#!/usr/bin/env python3
"""Minimal GoodWe SEMS portal client.

Used when the local dongle cannot be polled (DTLS-only hardware). Logs in,
finds the power station, and reads grid power.

Run it directly to check credentials and inspect the raw values:

    ./venv/bin/python sems.py --probe
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

import requests

log = logging.getLogger("goodwe-monitor.sems")

# SEMS wants a JSON blob in the Token header. This is the unauthenticated seed;
# after CrossLogin the response's data object replaces it.
SEED_TOKEN = json.dumps({"version": "v2.1.0", "client": "ios", "language": "en"})

DEFAULT_BASE_URL = "https://www.semsportal.com"

# Codes meaning "your token is stale, log in again". Anything else is a real
# failure that a re-login will not cure.
AUTH_EXPIRED_CODES = (100001, 100002)


class SemsError(Exception):
    """SEMS refused a request or answered something unusable."""


class SemsAuthError(SemsError):
    """Credentials rejected. Retrying will not help."""


def parse_power(text: Any) -> float | None:
    """Turn SEMS power strings into watts. '0.62(kW)' -> 620.0, '-30(W)' -> -30.0."""
    if isinstance(text, (int, float)):
        return float(text)
    if not isinstance(text, str):
        return None
    match = re.search(r"(-?\d+(?:\.\d+)?)\s*\(?\s*(k?W)", text, re.IGNORECASE)
    if not match:
        return None
    value = float(match.group(1))
    return value * 1000.0 if match.group(2).lower() == "kw" else value


class SemsClient:
    def __init__(
        self,
        account: str,
        password: str,
        base_url: str = DEFAULT_BASE_URL,
        station_id: str = "",
        timeout: float = 20.0,
    ) -> None:
        if not account or not password:
            raise SemsAuthError("SEMS_ACCOUNT and SEMS_PASSWORD must be set in .env")
        self.account = account
        self.password = password
        self.station_id = station_id
        self.timeout = timeout
        self._api_base = base_url.rstrip("/") + "/api/"
        self._token = SEED_TOKEN
        self._session = requests.Session()
        self._warned_meter_sign = False

    # -- plumbing ---------------------------------------------------------

    def _raw_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        url = self._api_base + path
        response = self._session.post(
            url,
            headers={"Token": self._token, "Content-Type": "application/json"},
            json=body,
            timeout=self.timeout,
        )
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as err:
            raise SemsError(f"{path}: response was not JSON") from err

    def login(self) -> None:
        payload = self._raw_post("v2/Common/CrossLogin", {
            "account": self.account,
            "pwd": self.password,
        })
        data = payload.get("data")
        if payload.get("hasError") or not data:
            raise SemsAuthError(
                f"login rejected: {payload.get('msg') or payload.get('code')}"
            )
        self._token = json.dumps(data)
        # SEMS redirects accounts to a regional host (eu/au/...). Honour it,
        # otherwise every later call 500s or returns empty data.
        api = payload.get("api")
        if isinstance(api, str) and api.startswith("http"):
            if api.rstrip("/") != self._api_base.rstrip("/"):
                log.info("SEMS redirected to regional endpoint %s", api)
            self._api_base = api if api.endswith("/") else api + "/"
        log.info("Logged in to SEMS as %s", self.account)

    def post(self, path: str, body: dict[str, Any]) -> Any:
        """POST, re-authenticating once if the session has expired.

        Only retries on the auth codes: other failures (e.g. 100000 "ver is not
        fund", which means the endpoint is unavailable) will not be fixed by
        logging in again, and retrying them just doubles the request rate.
        """
        if self._token is SEED_TOKEN:
            self.login()
        payload = self._raw_post(path, body)
        if payload.get("code") in AUTH_EXPIRED_CODES:
            log.debug("SEMS session expired (%s); re-authenticating",
                      payload.get("msg") or payload.get("code"))
            self.login()
            payload = self._raw_post(path, body)
        if payload.get("hasError"):
            raise SemsError(f"{path}: {payload.get('msg') or payload.get('code')}")
        return payload.get("data")

    # -- API calls --------------------------------------------------------

    def resolve_station_id(self) -> str:
        """Find the plant uuid.

        QueryPowerStationByHistory is used rather than the more obvious
        GetPowerStationIdByOwner, because the PowerStation/* listing endpoints
        answer "ver is not fund" (code 100000) on at least the hk region --
        regardless of the version sent in the Token header.
        """
        if self.station_id:
            return self.station_id

        station = ""
        try:
            data = self.post("v2/HistoryData/QueryPowerStationByHistory", {})
            stations = (data or {}).get("list") or []
            if stations:
                station = str(stations[0].get("id") or "")
                if len(stations) > 1:
                    log.warning(
                        "Account has %d plants; using %r. Set SEMS_STATION_ID "
                        "to pick another: %s",
                        len(stations),
                        stations[0].get("pw_name"),
                        ", ".join(
                            f"{s.get('pw_name')}={s.get('id')}" for s in stations
                        ),
                    )
                else:
                    log.info("Plant %r (%s)", stations[0].get("pw_name"), station)
        except SemsError as err:
            log.debug("QueryPowerStationByHistory failed: %s", err)

        if not station:
            # Older/other regions may still offer the classic endpoint.
            try:
                data = self.post("v2/PowerStation/GetPowerStationIdByOwner", {})
                if isinstance(data, str):
                    station = data.strip()
                elif isinstance(data, list) and data:
                    first = data[0]
                    station = first if isinstance(first, str) else str(
                        first.get("powerstation_id") or first.get("id") or ""
                    )
            except SemsError as err:
                log.debug("GetPowerStationIdByOwner failed: %s", err)

        if not station:
            raise SemsError(
                "Could not determine the power station id. Set SEMS_STATION_ID "
                "in .env (the uuid in the SEMS portal plant URL)."
            )
        self.station_id = station
        return station

    def monitor_detail(self) -> dict[str, Any]:
        data = self.post(
            "v2/PowerStation/GetMonitorDetailByPowerstationId",
            {"powerStationId": self.resolve_station_id()},
        )
        if not isinstance(data, dict):
            raise SemsError("GetMonitorDetailByPowerstationId returned no detail")
        return data

    # -- grid power -------------------------------------------------------

    def grid_draw_watts(self, detail: dict[str, Any], import_status: int = 1) -> float:
        """Watts being drawn FROM the grid. Negative means exporting.

        The order here matters, and is not the obvious one.

        powerflow.gridStatus LOOKS like a direction flag but is not: sampled
        live it read 1 while importing 45 W (pv 1692 + grid 45 = load 1737)
        AND while exporting 16 W (pv 2023 - grid 16 = load 2007). Trusting it
        reports export as import, which would fire alerts on sunny days.

        The inverter's pmeter is signed and does track direction: -45 while
        importing, +16 while exporting. That is export-positive, the same
        convention the local protocol uses, so it is the primary source.
        """
        # 1. Signed meter reading -- the only field observed to track direction.
        for inverter in detail.get("inverter") or []:
            if not isinstance(inverter, dict):
                continue
            full = inverter.get("invert_full")
            if not isinstance(full, dict):
                continue
            for key in ("pmeter", "meter_p", "pgrid"):
                value = parse_power(full.get(key))
                if value is not None:
                    log.debug("grid from invert_full.%s=%s", key, value)
                    return -value

        # 2. Energy balance: whatever the load needs beyond what PV and the
        #    battery supply has to come from the grid. Physically grounded,
        #    so it beats the unreliable status flag.
        flow = detail.get("powerflow") if isinstance(detail.get("powerflow"), dict) else None
        if flow:
            load = parse_power(flow.get("load"))
            pv = parse_power(flow.get("pv"))
            if load is not None and pv is not None:
                # Battery sign is unverified (this system has none); assumed
                # positive = discharging, i.e. supplying the house.
                battery = parse_power(flow.get("bettery")) or 0.0
                draw = load - pv - battery
                log.debug("grid from balance: load %s - pv %s - batt %s = %s",
                          load, pv, battery, draw)
                return draw

        # 3. Last resort: magnitude plus gridStatus. Known unreliable -- see
        #    the docstring -- but better than no reading at all.
        if flow:
            magnitude = parse_power(flow.get("grid"))
            if magnitude is not None:
                if not self._warned_meter_sign:
                    log.warning(
                        "Falling back to powerflow.grid + gridStatus, which "
                        "has been observed NOT to track direction. Treat the "
                        "sign with suspicion."
                    )
                    self._warned_meter_sign = True
                try:
                    status = int(flow.get("gridStatus"))
                except (TypeError, ValueError):
                    return abs(magnitude)
                return abs(magnitude) if status == import_status else -abs(magnitude)

        raise SemsError(
            "No grid power field found in the SEMS response. "
            "Run `python sems.py --probe --dump` and check the JSON."
        )

    def read_grid_draw_watts(self, import_status: int = 1) -> float:
        return self.grid_draw_watts(self.monitor_detail(), import_status)

    @staticmethod
    def has_battery(detail: dict[str, Any]) -> bool:
        """Is real storage attached, as opposed to an empty hybrid slot?

        battery_count reads 1 on a hybrid inverter with nothing plugged in,
        so it cannot be trusted on its own. Physical evidence -- voltage,
        state of health, or actual power flow -- is what distinguishes a
        fitted battery from an empty bay.
        """
        for inverter in detail.get("inverter") or []:
            if not isinstance(inverter, dict):
                continue
            full = inverter.get("invert_full")
            if not isinstance(full, dict):
                continue
            for key in ("vbattery1", "total_pbattery", "soh"):
                value = full.get(key)
                if isinstance(value, (int, float)) and value != 0:
                    return True
            for pack in full.get("more_batterys") or []:
                if not isinstance(pack, dict):
                    continue
                if any(
                    isinstance(pack.get(k), (int, float)) and pack[k] != 0
                    for k in ("vbattery", "soh", "pbattery")
                ):
                    return True
        return False

    def flow_summary(self, detail: dict[str, Any]) -> dict[str, Any]:
        """The other powerflow figures, for display alongside grid draw."""
        flow = detail.get("powerflow")
        if not isinstance(flow, dict):
            return {}
        summary: dict[str, Any] = {}
        for source_key, label in (("pv", "pv"), ("load", "load"),
                                  ("bettery", "battery")):
            value = parse_power(flow.get(source_key))
            if value is not None:
                summary[label] = value

        # Report SOC only when a battery is actually fitted -- but then report
        # it even at 0%, because a flat battery is the reading that matters
        # most. Suppressing "0%" would hide exactly that.
        if self.has_battery(detail):
            soc = flow.get("socText") or flow.get("soc")
            if soc not in (None, ""):
                summary["soc"] = soc
        return summary

    def power_curve(self, date: str) -> list[tuple[str, float]]:
        """PV output through one day as [(HH:MM, watts)].

        `date` is YYYY-MM-DD. Today's curve is truncated at the current time,
        so use a past date to see a full sunrise-to-sunset span.
        """
        data = self.post(
            "v2/Charts/GetPlantPowerChart",
            {"id": self.resolve_station_id(), "date": date, "full_script": False},
        )
        lines = (data or {}).get("lines") or []
        pv = next((l for l in lines if l.get("key") == "PCurve_Power_PV"), None)
        if not pv:
            return []
        return [
            (point["x"], float(point["y"]))
            for point in pv.get("xy") or []
            if isinstance(point.get("y"), (int, float))
        ]

    def production_window(
        self, date: str, floor_watts: float = 100.0
    ) -> dict[str, Any]:
        """Summarise when the panels were actually producing on `date`."""
        curve = self.power_curve(date)
        producing = [(t, w) for t, w in curve if w > 0]
        above = [(t, w) for t, w in curve if w >= floor_watts]
        if not producing:
            return {"date": date, "any_output": False}
        peak_time, peak_watts = max(producing, key=lambda tw: tw[1])
        return {
            "date": date,
            "any_output": True,
            "first": producing[0][0],
            "last": producing[-1][0],
            "first_above": above[0][0] if above else None,
            "last_above": above[-1][0] if above else None,
            "peak_time": peak_time,
            "peak_watts": peak_watts,
            "floor_watts": floor_watts,
        }

    def read(self, import_status: int = 1) -> tuple[float, dict[str, Any]]:
        """Grid draw in watts plus the surrounding powerflow figures."""
        detail = self.monitor_detail()
        return (
            self.grid_draw_watts(detail, import_status),
            self.flow_summary(detail),
        )


def _show_curve(client: SemsClient, args: Any) -> int:
    """Print the production window per day, plus a suggested ACTIVE_HOURS."""
    import datetime

    try:
        days = int(args.curve)
    except (TypeError, ValueError):
        days = 0

    if days > 0:
        # Complete days only -- today's curve stops at the current time and
        # would make sunset look far earlier than it is.
        dates = [
            (datetime.date.today() - datetime.timedelta(days=n)).isoformat()
            for n in range(1, days + 1)
        ]
    else:
        dates = [datetime.date.today().isoformat()]
        print("Today's curve is cut off at the current time. "
              "Use --curve 3 for complete days.\n")

    print(f"{'date':12} {'first':>7} {'>=' + str(int(args.floor)) + 'W':>7} "
          f"{'peak':>18} {'last>=':>7} {'last':>7}")
    windows = []
    for date in dates:
        try:
            info = client.production_window(date, args.floor)
        except (SemsError, requests.RequestException) as err:
            print(f"{date:12}  unavailable ({err})")
            continue
        if not info["any_output"]:
            print(f"{date:12}  no output recorded")
            continue
        peak = f"{info['peak_time']} {info['peak_watts']:,.0f}W"
        print(f"{date:12} {info['first']:>7} {str(info['first_above']):>7} "
              f"{peak:>18} {str(info['last_above']):>7} {info['last']:>7}")
        windows.append(info)

    if windows and days > 0:
        earliest = min(w["first"] for w in windows)
        latest = max(w["last"] for w in windows)
        print(f"\nAcross {len(windows)} day(s), the panels produced between "
              f"{earliest} and {latest}.")
        print(f"Suggested:  ACTIVE_HOURS={earliest}-{latest}")
        print("\nThis is a snapshot of the current season -- daylight shifts by "
              "hours over the year.\nRe-run this occasionally, or set "
              "MIN_PV_WATTS to mute on actual output instead of the clock.")
    return 0


def _probe() -> int:
    import argparse
    import os

    from dotenv import load_dotenv

    load_dotenv()
    parser = argparse.ArgumentParser(description="Check SEMS credentials and data")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--dump", action="store_true", help="print the full JSON")
    parser.add_argument(
        "--curve", nargs="?", const="0", metavar="DAYS",
        help="show when the panels actually produce, to set ACTIVE_HOURS. "
             "DAYS = how many complete past days to include (default today).",
    )
    parser.add_argument(
        "--floor", type=float, default=100.0,
        help="watts counted as 'really producing' (default 100)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")
    try:
        client = SemsClient(
            account=os.getenv("SEMS_ACCOUNT", "").strip(),
            password=os.getenv("SEMS_PASSWORD", "").strip(),
            base_url=os.getenv("SEMS_BASE_URL", "").strip() or DEFAULT_BASE_URL,
            station_id=os.getenv("SEMS_STATION_ID", "").strip(),
        )
        detail = client.monitor_detail()
    except SemsAuthError as err:
        print(f"\nFAILED: {err}")
        return 1
    except (SemsError, requests.RequestException) as err:
        print(f"\nFAILED: {err}")
        return 1

    if args.curve is not None:
        return _show_curve(client, args)

    flow = detail.get("powerflow") or {}
    print("\n--- powerflow ---")
    for key in ("pv", "load", "grid", "bettery", "gridStatus", "loadStatus",
                "pvStatus", "betteryStatus"):
        if key in flow:
            print(f"  {key:14s} {flow[key]!r}")

    for inverter in detail.get("inverter") or []:
        full = inverter.get("invert_full") or {}
        print(f"\n--- inverter {inverter.get('sn', '?')} ---")
        for key in ("pmeter", "pgrid", "pac", "out_pac", "soc", "status"):
            if key in full:
                print(f"  {key:14s} {full[key]!r}")

    import_status = int(os.getenv("SEMS_IMPORT_STATUS", "1") or 1)
    watts = client.grid_draw_watts(detail, import_status)
    other = client.grid_draw_watts(detail, -import_status)

    print("\n--- interpretation ---")
    print(f"  SEMS_IMPORT_STATUS={import_status:<3} -> {watts:+9.1f} W  "
          f"({'IMPORTING from grid' if watts > 0 else 'exporting to grid'})")
    print(f"  SEMS_IMPORT_STATUS={-import_status:<3} -> {other:+9.1f} W  "
          f"({'IMPORTING from grid' if other > 0 else 'exporting to grid'})")
    print("\nVERIFY THIS BEFORE TRUSTING ALERTS.")
    print("Compare against the SEMS app right now: if the sun is up and you are")
    print("exporting, the correct setting is whichever line reads 'exporting'.")
    print("A wrong setting fires the webhook on solar export instead of grid draw.")
    print(f"\nWebhook would fire now: "
          f"{watts > float(os.getenv('GRID_DRAW_THRESHOLD_WATTS', '100') or 100)}")

    if "--dump" in sys.argv:
        print("\n--- raw JSON ---")
        print(json.dumps(detail, indent=2)[:6000])
    return 0


if __name__ == "__main__":
    raise SystemExit(_probe())
