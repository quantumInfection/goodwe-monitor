#!/usr/bin/env python3
"""Monitor GoodWe grid draw and alert when it crosses a threshold.

Reads either the local inverter (UDP discovery, falling back to INVERTER_IP)
or the SEMS cloud portal, on a loop. When the house pulls more than
GRID_DRAW_THRESHOLD_WATTS from the grid it raises a macOS notification and/or
POSTs to WEBHOOK_URL, then sleeps DEBOUNCE_TIME before it can fire again.

    python monitor.py --watch    live terminal dashboard
    python monitor.py            one log line per poll
    python monitor.py --once     single reading, then exit
"""

from __future__ import annotations

import argparse
import asyncio
import ipaddress
import logging
from logging.handlers import RotatingFileHandler
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import goodwe
import requests
from dotenv import load_dotenv
from goodwe import Inverter, InverterError

import sems

# Absolute path: launchd starts the agent with an unrelated working directory,
# so the default relative .env lookup would silently find nothing.
load_dotenv(Path(__file__).resolve().parent / ".env")

log = logging.getLogger("goodwe-monitor")


class ConfigurationError(Exception):
    """Something in .env makes the monitor unrunnable. Never retried."""


DISCOVERY_PORT = 48899
DISCOVERY_PAYLOAD = b"WIFIKIT-214028-READ"

# Tried in order when GRID_POWER_SENSOR is not set. Names vary by inverter
# family: meter_active_power_total on ET/EH, meter_active_power on DT/MS,
# pgrid on ES/EM. meter_p is kept first because some firmware exposes it.
GRID_SENSOR_CANDIDATES = (
    "meter_p",
    "meter_active_power_total",
    "active_power",
    "meter_active_power",
    "pgrid",
)


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _env_str(name: str, default: str = "") -> str:
    return (os.getenv(name) or "").strip() or default


def _env_num(name: str, default: float, cast=float) -> Any:
    """Read a numeric env var, falling back to the default if it is unusable."""
    raw = _env_str(name)
    if not raw:
        return cast(default)
    try:
        return cast(raw)
    except (TypeError, ValueError):
        log.warning("%s=%r is not a number; using default %s", name, raw, default)
        return cast(default)


def _env_bool(name: str, default: bool) -> bool:
    raw = _env_str(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


class Config:
    def __init__(self) -> None:
        self.source = _env_str("DATA_SOURCE", "local").lower()
        self.fallback_ip = _env_str("INVERTER_IP")
        self.webhook_url = _env_str("WEBHOOK_URL")
        self.threshold = _env_num("GRID_DRAW_THRESHOLD_WATTS", 100.0)
        self.poll_interval = _env_num("POLL_INTERVAL", 10.0)
        self.debounce = _env_num("DEBOUNCE_TIME", 300.0)

        # SEMS cloud
        self.sems_account = _env_str("SEMS_ACCOUNT")
        self.sems_password = _env_str("SEMS_PASSWORD")
        self.sems_base_url = _env_str("SEMS_BASE_URL", sems.DEFAULT_BASE_URL)
        self.sems_station_id = _env_str("SEMS_STATION_ID")
        self.sems_import_status = _env_num("SEMS_IMPORT_STATUS", 1, int)

        # Alerting
        self.notify_macos = _env_bool("NOTIFY_MACOS", sys.platform == "darwin")
        self.notify_sound = _env_str("NOTIFY_SOUND", "Ping")

        self.sensor = _env_str("GRID_POWER_SENSOR")
        self.invert_sign = _env_bool("INVERT_GRID_SIGN", True)
        self.port = _env_num("INVERTER_PORT", 8899, int)
        self.timeout = _env_num("INVERTER_TIMEOUT", 1, int)
        self.retries = _env_num("INVERTER_RETRIES", 3, int)
        self.webhook_timeout = _env_num("WEBHOOK_TIMEOUT", 10.0)
        self.max_read_failures = _env_num("MAX_READ_FAILURES", 5, int)
        self.log_level = _env_str("LOG_LEVEL", "INFO").upper()
        self.log_file = _env_str("LOG_FILE")
        self.log_max_bytes = _env_num("LOG_MAX_BYTES", 1_000_000, int)
        self.log_backups = _env_num("LOG_BACKUPS", 3, int)

    def validate(self) -> list[str]:
        problems = []
        if self.source not in ("local", "sems"):
            problems.append(f"DATA_SOURCE must be 'local' or 'sems', not {self.source!r}")
        # An empty WEBHOOK_URL is allowed: alerts are printed instead of POSTed.
        if self.webhook_url and not self.webhook_url.startswith(("http://", "https://")):
            problems.append("WEBHOOK_URL must start with http:// or https://")
        if self.poll_interval <= 0:
            problems.append("POLL_INTERVAL must be greater than 0.")
        if self.debounce < 0:
            problems.append("DEBOUNCE_TIME cannot be negative.")
        if self.source == "sems":
            if not self.sems_account or not self.sems_password:
                problems.append(
                    "DATA_SOURCE=sems needs SEMS_ACCOUNT and SEMS_PASSWORD."
                )
            if self.poll_interval < 60:
                # Not fatal, but SEMS only refreshes every few minutes and will
                # rate-limit a tight loop.
                log.warning(
                    "POLL_INTERVAL=%.0fs is aggressive for SEMS, which updates "
                    "roughly every 5 minutes. Consider 120-300.",
                    self.poll_interval,
                )
        return problems


# --------------------------------------------------------------------------
# Discovery and connection
# --------------------------------------------------------------------------

def _parse_discovery_response(raw: bytes) -> str | None:
    """Pull the IPv4 address out of a discovery response body, if present.

    Older WiFi kits answer b'192.168.1.14,AABBCCDDEEFF,SolarWiFi'. Newer DTLS
    dongles answer b'dongle@sn,dtls_port:8899,<serial>', which has no address
    at all -- hence None, and the caller falls back to _broadcast_discover().
    """
    text = raw.decode("ascii", errors="ignore")
    for token in text.replace("\x00", "").split(","):
        token = token.strip()
        try:
            ipaddress.IPv4Address(token)
        except ValueError:
            continue
        return token
    return None


def _broadcast_addresses() -> list[str]:
    """Broadcast targets to try, most specific first."""
    targets: list[str] = []
    try:
        out = subprocess.run(
            ["ifconfig"], capture_output=True, text=True, timeout=5
        ).stdout
        targets += re.findall(r"broadcast (\d+\.\d+\.\d+\.\d+)", out)
    except (OSError, subprocess.SubprocessError):
        pass
    # 255.255.255.255 last: macOS frequently declines to route it, which is the
    # usual reason goodwe.search_inverters() finds nothing on a working LAN.
    targets.append("255.255.255.255")
    return list(dict.fromkeys(targets))


def _broadcast_probe(timeout: float) -> list[tuple[str, bytes]]:
    """Send the discovery command and collect (sender_ip, payload) replies.

    Takes the address from the UDP source rather than the body, so it still
    works with dongles whose reply omits the IP.
    """
    found: list[tuple[str, bytes]] = []
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.settimeout(timeout)
    try:
        for target in _broadcast_addresses():
            try:
                sock.sendto(DISCOVERY_PAYLOAD, (target, DISCOVERY_PORT))
            except OSError as err:
                log.debug("Broadcast to %s failed: %s", target, err)
                continue
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    data, addr = sock.recvfrom(1024)
                except socket.timeout:
                    break
                except OSError:
                    break
                if not any(ip == addr[0] for ip, _ in found):
                    found.append((addr[0], data))
            if found:
                break
    finally:
        sock.close()
    return found


async def resolve_ip(config: Config) -> str:
    """Auto-discover the inverter, falling back to INVERTER_IP from .env."""
    try:
        response = await goodwe.search_inverters()
        log.debug("Discovery response: %r", response)
        ip = _parse_discovery_response(response or b"")
        if ip:
            log.info("Discovered inverter at %s", ip)
            return ip
        log.debug("Discovery reply carried no IP: %r", response)
    except (InverterError, OSError, asyncio.TimeoutError) as err:
        log.debug("goodwe.search_inverters() found nothing: %s", err)

    # Second pass: our own broadcast, reading the IP off the UDP source.
    try:
        replies = await asyncio.to_thread(_broadcast_probe, 3.0)
        if replies:
            ip, payload = replies[0]
            log.info("Discovered inverter at %s (reply: %r)", ip, payload)
            if len(replies) > 1:
                log.warning(
                    "Several devices answered; using %s. Others: %s",
                    ip,
                    ", ".join(other for other, _ in replies[1:]),
                )
            return ip
    except OSError as err:
        log.debug("Broadcast discovery failed: %s", err)

    if not config.fallback_ip:
        raise ConfigurationError(
            "Discovery failed and no INVERTER_IP fallback is set in .env"
        )
    log.info("Discovery found nothing; falling back to INVERTER_IP=%s",
             config.fallback_ip)
    return config.fallback_ip


async def connect_inverter(config: Config) -> tuple[Inverter, str]:
    ip = await resolve_ip(config)
    inverter = await goodwe.connect(
        host=ip,
        port=config.port,
        timeout=config.timeout,
        retries=config.retries,
    )
    log.info(
        "Connected to %s %s (S/N %s) at %s",
        type(inverter).__name__,
        getattr(inverter, "model_name", "unknown"),
        getattr(inverter, "serial_number", "unknown"),
        ip,
    )
    return inverter, ip


# --------------------------------------------------------------------------
# Reading grid power
# --------------------------------------------------------------------------

def resolve_sensor(data: dict[str, Any], config: Config) -> str | None:
    """Pick the runtime-data key holding grid power."""
    if config.sensor:
        if config.sensor in data:
            return config.sensor
        log.error(
            "GRID_POWER_SENSOR=%r is not in this inverter's runtime data. "
            "Available grid-ish sensors: %s",
            config.sensor,
            ", ".join(sorted(k for k in data if "power" in k or "grid" in k)) or "none",
        )
        return None

    for candidate in GRID_SENSOR_CANDIDATES:
        if isinstance(data.get(candidate), (int, float)):
            return candidate
    return None


def grid_draw_watts(raw_value: float, config: Config) -> float:
    """Normalise a raw sensor reading to watts drawn FROM the grid.

    GoodWe reports grid power export-positive, so importing shows up as a
    negative number. Flipping the sign makes "drawing 250 W from the grid"
    read as +250, which is what the threshold comparison expects.
    """
    return -float(raw_value) if config.invert_sign else float(raw_value)


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------
# Both answer read() in the same units: watts drawn FROM the grid, positive,
# so everything downstream (threshold, webhook, debounce) is source-agnostic.

class LocalSource:
    """Poll the inverter directly over the LAN."""

    name = "local"
    # Transient faults worth retrying rather than crashing on.
    transient = (InverterError, OSError, asyncio.TimeoutError)

    def __init__(self, config: Config) -> None:
        self.config = config
        self._inverter: Inverter | None = None
        self._sensor: str | None = None
        self.detail = ""
        self.stats: dict[str, Any] = {}

    async def connect(self) -> None:
        self._inverter, ip = await connect_inverter(self.config)
        self._sensor = None
        self.detail = ip

    async def read(self) -> float | None:
        assert self._inverter is not None
        data = await self._inverter.read_runtime_data()

        if self._sensor is None:
            self._sensor = resolve_sensor(data, self.config)
            if self._sensor is None:
                log.error(
                    "No grid power sensor found. Set GRID_POWER_SENSOR in .env "
                    "to one of: %s",
                    ", ".join(sorted(data)),
                )
                return None
            log.info("Using grid power sensor: %s", self._sensor)

        raw = data.get(self._sensor)
        if not isinstance(raw, (int, float)):
            log.warning("Sensor %s returned %r; skipping", self._sensor, raw)
            return None

        self.stats = {}
        for key, label in (("ppv", "pv"), ("house_consumption", "load"),
                           ("pbattery1", "battery"), ("battery_soc", "soc")):
            value = data.get(key)
            if isinstance(value, (int, float)):
                self.stats[label] = value
        return grid_draw_watts(raw, self.config)


class SemsSource:
    """Poll the SEMS cloud portal. Used when the dongle refuses local access."""

    name = "sems"
    transient = (sems.SemsError, requests.RequestException, OSError)

    def __init__(self, config: Config) -> None:
        self.config = config
        self._client: sems.SemsClient | None = None
        self.detail = ""
        self.stats: dict[str, Any] = {}

    async def connect(self) -> None:
        try:
            client = sems.SemsClient(
                account=self.config.sems_account,
                password=self.config.sems_password,
                base_url=self.config.sems_base_url,
                station_id=self.config.sems_station_id,
            )
            await asyncio.to_thread(client.login)
            station = await asyncio.to_thread(client.resolve_station_id)
        except sems.SemsAuthError as err:
            # Bad credentials will never fix themselves; do not spin on them.
            raise ConfigurationError(f"SEMS login failed: {err}") from err
        self._client = client
        self.detail = f"station {station}"

    async def read(self) -> float | None:
        assert self._client is not None
        try:
            watts, self.stats = await asyncio.to_thread(
                self._client.read, self.config.sems_import_status
            )
            return watts
        except sems.SemsAuthError as err:
            raise ConfigurationError(f"SEMS rejected the credentials: {err}") from err


def build_source(config: Config) -> LocalSource | SemsSource:
    return SemsSource(config) if config.source == "sems" else LocalSource(config)


# --------------------------------------------------------------------------
# Webhook
# --------------------------------------------------------------------------

def _applescript_str(text: str) -> str:
    """Escape a Python string for embedding in an AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _notify_macos(title: str, subtitle: str, message: str, sound: str) -> str:
    """Show a macOS notification. Returns "" on success, else an error string.

    Prefers terminal-notifier when installed, because -group replaces the
    previous alert instead of stacking a new banner every debounce period.
    Falls back to osascript, which is always present on macOS.
    """
    if shutil.which("terminal-notifier"):
        cmd = [
            "terminal-notifier",
            "-title", title,
            "-subtitle", subtitle,
            "-message", message,
            "-group", "goodwe-monitor",
        ]
        if sound:
            cmd += ["-sound", sound]
    else:
        script = (
            f'display notification "{_applescript_str(message)}" '
            f'with title "{_applescript_str(title)}" '
            f'subtitle "{_applescript_str(subtitle)}"'
        )
        if sound:
            script += f' sound name "{_applescript_str(sound)}"'
        cmd = ["osascript", "-e", script]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as err:
        return str(err)
    if result.returncode != 0:
        return (result.stderr or "").strip() or f"exit {result.returncode}"
    return ""


def _post_webhook(url: str, payload: dict[str, Any], timeout: float) -> None:
    response = requests.post(url, json=payload, timeout=timeout)
    response.raise_for_status()


async def fire_alert(
    config: Config,
    watts: float,
    source: str,
    detail: str,
    stats: dict[str, Any] | None = None,
) -> bool:
    """Notify every configured channel. True if all of them succeeded."""
    stats = stats or {}
    log.warning(
        "%s",
        f"ALERT: grid draw {watts:,.0f} W is over the "
        f"{config.threshold:,.0f} W threshold",
    )

    ok = True
    if config.notify_macos:
        context = "  ".join(
            f"{label} {stats[key]:,.0f} W"
            for label, key in (("Solar", "pv"), ("Load", "load"))
            if isinstance(stats.get(key), (int, float))
        )
        error = await asyncio.to_thread(
            _notify_macos,
            f"Grid draw {watts:,.0f} W",
            f"Over the {config.threshold:,.0f} W threshold",
            context or f"via {source}",
            config.notify_sound,
        )
        if error:
            ok = False
            log.error(
                "macOS notification failed: %s. If this is a permissions "
                "problem, allow notifications for your terminal app in "
                "System Settings > Notifications.", error,
            )
        else:
            log.info("Notification shown.")

    if config.webhook_url:
        ok = await _fire_webhook(config, watts, source, detail) and ok

    if not config.notify_macos and not config.webhook_url:
        log.warning("No alert channel configured; nothing was sent.")
    return ok


async def _fire_webhook(config: Config, watts: float, source: str, detail: str) -> bool:
    """POST the current wattage. Returns True if the endpoint accepted it."""
    payload = {
        "event": "grid_draw_exceeded",
        "watts": round(watts, 1),
        "threshold_watts": config.threshold,
        "source": source,
        "source_detail": detail,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    try:
        # requests is blocking, so keep it off the event loop.
        await asyncio.to_thread(
            _post_webhook, config.webhook_url, payload, config.webhook_timeout
        )
        log.info("Webhook fired: %.1f W (threshold %.1f W)", watts, config.threshold)
        return True
    except requests.RequestException as err:
        log.error("Webhook POST failed: %s", err)
        return False


# --------------------------------------------------------------------------
# Terminal display
# --------------------------------------------------------------------------

SPARK = "▁▂▃▄▅▆▇█"


class Screen:
    """Live terminal readout. Redraws in place on a tty, logs lines otherwise."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.tty = sys.stdout.isatty()
        self.history: deque[tuple[float, float]] = deque(maxlen=180)
        self.watts: float | None = None
        self.stats: dict[str, Any] = {}
        self.source = ""
        self.detail = ""
        self.status = "starting"
        self.polls = 0
        self.errors = 0
        self.last_alert: float | None = None
        self.last_read: float | None = None
        self.next_poll: float | None = None
        self.note = ""

    # -- helpers ----------------------------------------------------------

    def _c(self, text: str, code: str) -> str:
        return f"\033[{code}m{text}\033[0m" if self.tty else text

    def _spark(self, width: int = 48) -> str:
        if not self.history:
            return ""
        values = [w for _, w in list(self.history)[-width:]]
        low, high = min(values), max(values)
        span = high - low
        if span < 1e-9:
            return SPARK[3] * len(values)
        return "".join(
            SPARK[min(len(SPARK) - 1, int((v - low) / span * (len(SPARK) - 1)))]
            for v in values
        )

    @staticmethod
    def _ago(when: float | None) -> str:
        if when is None:
            return "never"
        delta = int(time.time() - when)
        if delta < 60:
            return f"{delta}s ago"
        if delta < 3600:
            return f"{delta // 60}m {delta % 60}s ago"
        return f"{delta // 3600}h {(delta % 3600) // 60}m ago"

    # -- rendering --------------------------------------------------------

    def render(self) -> str:
        rule = "─" * 62
        title = f"GoodWe monitor  ·  {self.source}"
        if self.detail:
            title += f"  ·  {self.detail}"
        lines = [
            self._c(title[:62], "1"),
            self._c(rule, "2;37"),
        ]

        if self.watts is None:
            lines.append("  waiting for first reading…")
        else:
            over = self.watts > self.config.threshold
            if self.watts >= 0:
                arrow, word, colour = "▼", "importing", ("31;1" if over else "33")
            else:
                arrow, word, colour = "▲", "exporting", "32"
            reading = f"{abs(self.watts):,.0f} W"
            lines.append(
                f"  {'Grid':<9}{self._c(f'{arrow} {reading:>10}', colour)}"
                f"  {word:<10}   threshold {self.config.threshold:,.0f} W"
            )
            for label, key in (("Solar", "pv"), ("Load", "load"),
                               ("Battery", "battery")):
                value = self.stats.get(key)
                if isinstance(value, (int, float)):
                    extra = ""
                    if key == "battery" and self.stats.get("soc"):
                        extra = f"   ({self.stats['soc']})"
                    lines.append(f"  {label:<9}  {value:>10,.0f} W{extra}")

        spark = self._spark()
        if spark:
            lines += [self._c(rule, "2;37"), f"  {spark}"]
            window = list(self.history)
            lows = min(w for _, w in window)
            highs = max(w for _, w in window)
            lines.append(
                self._c(f"  last {len(window)} readings   "
                        f"min {lows:,.0f} W   max {highs:,.0f} W", "2;37")
            )

        lines.append(self._c(rule, "2;37"))
        state = self.status
        if self.watts is not None and self.watts > self.config.threshold:
            state = self._c("OVER THRESHOLD", "31;1")
        elif self.status == "ok":
            state = self._c("ok", "32")
        countdown = ""
        if self.next_poll:
            remaining = max(0, int(self.next_poll - time.time()))
            countdown = f"   next poll {remaining}s"
        lines.append(
            f"  {state}   polls {self.polls}   errors {self.errors}"
            f"   last alert {self._ago(self.last_alert)}{countdown}"
        )
        if self.note:
            lines.append(self._c(f"  {self.note}", "33"))
        lines.append(self._c("  Ctrl-C to stop", "2;37"))
        return "\n".join(lines)

    def draw(self) -> None:
        if self.tty:
            # Home the cursor and clear, rather than scrolling the buffer.
            sys.stdout.write("\033[H\033[J" + self.render() + "\n")
            sys.stdout.flush()

    def line(self) -> None:
        """One-line summary, for non-tty output (piped, nohup, systemd)."""
        if self.watts is None:
            return
        bits = [f"grid {self.watts:+,.0f} W"]
        for label, key in (("pv", "pv"), ("load", "load")):
            value = self.stats.get(key)
            if isinstance(value, (int, float)):
                bits.append(f"{label} {value:,.0f} W")
        flag = "  OVER" if self.watts > self.config.threshold else ""
        print(f"{datetime.now():%H:%M:%S}  {'  '.join(bits)}{flag}", flush=True)

    def update(self, watts: float, source: LocalSource | SemsSource) -> None:
        self.watts = watts
        self.stats = source.stats
        self.source = source.name
        self.detail = source.detail
        self.polls += 1
        self.last_read = time.time()
        self.history.append((self.last_read, watts))
        self.status = "ok"


# --------------------------------------------------------------------------
# Main loop
# --------------------------------------------------------------------------

async def monitor(
    config: Config, stop: asyncio.Event, screen: Screen | None = None
) -> None:
    source = build_source(config)
    connected = False
    failures = 0

    while not stop.is_set():
        try:
            if not connected:
                if screen:
                    screen.status = "connecting"
                    screen.draw()
                await source.connect()
                connected = True
                failures = 0

            watts = await source.read()
            if watts is None:
                await _wait(stop, config.poll_interval, screen)
                continue

            failures = 0
            if screen:
                screen.update(watts, source)
                screen.line()
                screen.draw()
            else:
                # Plain mode still reports every poll -- a monitor that prints
                # nothing for hours is indistinguishable from a hung one.
                extras = "".join(
                    f"  {label} {source.stats[key]:,.0f} W"
                    for label, key in (("pv", "pv"), ("load", "load"))
                    if isinstance(source.stats.get(key), (int, float))
                )
                # Pre-format: %-style logging has no thousands separator.
                log.info(
                    "%s",
                    f"grid {watts:+,.0f} W "
                    f"({'importing' if watts >= 0 else 'exporting'})"
                    f"{extras}   threshold {config.threshold:,.0f} W"
                    f"{'  OVER' if watts > config.threshold else ''}",
                )

            if watts > config.threshold:
                await fire_alert(
                    config, watts, source.name, source.detail, source.stats
                )
                if screen:
                    screen.last_alert = time.time()
                    screen.note = (
                        f"alert at {datetime.now():%H:%M:%S} — "
                        f"debouncing {config.debounce:.0f}s"
                    )
                # Debounce whether or not the POST succeeded, so a failing
                # endpoint is not hammered every poll either.
                log.info("Debouncing for %.0f s", config.debounce)
                await _wait(stop, config.debounce, screen)
                if screen:
                    screen.note = ""
                continue

            await _wait(stop, config.poll_interval, screen)

        except (asyncio.CancelledError, ConfigurationError):
            raise
        except source.transient as err:
            # UDP drops and cloud hiccups are routine; a failed read must not
            # kill the loop.
            failures += 1
            log.warning(
                "Read failed (%d/%d): %s", failures, config.max_read_failures, err
            )
            if screen:
                screen.errors += 1
                screen.status = f"read failed ({failures}/{config.max_read_failures})"
                screen.note = str(err)[:58]
                screen.draw()
            if failures >= config.max_read_failures:
                log.error("Too many failures; reconnecting.")
                connected = False
                failures = 0
            await _wait(stop, config.poll_interval, screen)
        except Exception:  # noqa: BLE001 - last resort, keep the loop alive
            log.exception("Unexpected error; continuing.")
            if screen:
                screen.errors += 1
            await _wait(stop, config.poll_interval, screen)


async def _wait(
    stop: asyncio.Event, seconds: float, screen: Screen | None = None
) -> None:
    """Sleep, but wake immediately on shutdown.

    With a live screen, tick once a second so the countdown stays honest.
    """
    if screen is None or not screen.tty:
        try:
            await asyncio.wait_for(stop.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            pass
        return

    screen.next_poll = time.time() + seconds
    try:
        while not stop.is_set():
            remaining = screen.next_poll - time.time()
            if remaining <= 0:
                return
            screen.draw()
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(1.0, remaining))
            except asyncio.TimeoutError:
                continue
    finally:
        screen.next_poll = None


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Monitor GoodWe grid draw; alert over a threshold."
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="live terminal readout instead of scrolling logs",
    )
    parser.add_argument(
        "--once", action="store_true", help="take one reading, print it, exit",
    )
    args = parser.parse_args()

    config = Config()
    handlers: list[logging.Handler] = []
    if config.log_file:
        # Rotate: as a login agent this runs for weeks at a time.
        path = Path(config.log_file).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(
            RotatingFileHandler(
                path, maxBytes=config.log_max_bytes, backupCount=config.log_backups
            )
        )
    if not config.log_file or sys.stdout.isatty():
        handlers.append(logging.StreamHandler())

    logging.basicConfig(
        # In --watch the screen owns the terminal, so keep logging quiet and
        # let warnings/errors surface in the status line instead.
        level=logging.WARNING if args.watch
        else getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=handlers,
    )

    if args.once:
        return await _read_once(config)

    problems = config.validate()
    if problems:
        for problem in problems:
            log.error("Config error: %s", problem)
        log.error("Fix .env and try again (see .env.example).")
        return 1

    if not config.webhook_url:
        log.info("No WEBHOOK_URL set — alerts will be printed, not sent.")
    log.info(
        "Monitoring via %s: threshold %.0f W, poll %.0fs, debounce %.0fs",
        config.source,
        config.threshold,
        config.poll_interval,
        config.debounce,
    )

    screen = Screen(config) if args.watch else None
    if screen and screen.tty:
        sys.stdout.write("\033[?25l")  # hide cursor while the view is live
        screen.draw()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await monitor(config, stop, screen)
    except ConfigurationError as err:
        log.error("%s", err)
        return 1
    finally:
        if screen and screen.tty:
            sys.stdout.write("\033[?25h")  # restore cursor
            sys.stdout.flush()

    log.info("Shutting down.")
    return 0


async def _read_once(config: Config) -> int:
    """Single reading for scripts and cron."""
    source = build_source(config)
    try:
        await source.connect()
        watts = await source.read()
    except ConfigurationError as err:
        log.error("%s", err)
        return 1
    except Exception as err:  # noqa: BLE001 - report and exit non-zero
        log.error("Read failed: %s", err)
        return 1
    if watts is None:
        log.error("No reading available.")
        return 1

    direction = "importing" if watts >= 0 else "exporting"
    print(f"grid {watts:+,.0f} W ({direction})", end="")
    for label, key in (("pv", "pv"), ("load", "load"), ("battery", "battery")):
        value = source.stats.get(key)
        if isinstance(value, (int, float)):
            print(f"  {label} {value:,.0f} W", end="")
    print(f"  threshold {config.threshold:,.0f} W"
          f"  {'OVER' if watts > config.threshold else 'ok'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
