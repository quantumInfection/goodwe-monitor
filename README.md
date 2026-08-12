# goodwe-monitor

Watch a GoodWe solar inverter and get a desktop notification when your house
starts pulling power from the grid.

Reads either the **inverter directly on your LAN** or the **SEMS cloud portal**
(for dongles that refuse local connections). Runs in the terminal or as a macOS
login agent.

```
GoodWe monitor  ·  sems  ·  station a1b2c3d4
──────────────────────────────────────────────────────────────
  Grid     ▼      305 W  importing    threshold 100 W
  Solar             210 W
  Load            1,660 W
  Battery             0 W
──────────────────────────────────────────────────────────────
  ▁▂▅█▆▃▁
  last 7 readings   min 120 W   max 1,450 W
──────────────────────────────────────────────────────────────
  OVER THRESHOLD   polls 7   errors 0   last alert 42s ago   next poll 214s
```

## Install

```bash
git clone https://github.com/quantumInfection/goodwe-monitor.git
cd goodwe-monitor
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`, then:

```bash
./venv/bin/python monitor.py --watch
```

## Choosing a data source

**`DATA_SOURCE=local`** — polls the inverter over your LAN. Fast, private, no
account needed. Find it with:

```bash
./venv/bin/python find_inverter.py            # one-shot scan
./venv/bin/python find_inverter.py --watch    # re-scan, flag IP changes
```

**`DATA_SOURCE=sems`** — polls GoodWe's SEMS portal with your portal login.
Use this if your dongle only speaks DTLS (see [Local access](#local-access-may-not-work)).
Slower and cloud-dependent, but works when local does not.

```bash
./venv/bin/python sems.py --probe          # check credentials and readings
./venv/bin/python sems.py --probe --dump   # raw JSON
```

## Running

```bash
./venv/bin/python monitor.py --watch    # live dashboard
./venv/bin/python monitor.py            # one log line per poll
./venv/bin/python monitor.py --once     # single reading, then exit
```

Piped or run as a service, the dashboard degrades to one timestamped line per
poll so logs stay readable.

## Start at login (macOS)

```bash
./service.sh install     # start at login, and start now
./service.sh status      # running? plus recent log lines
./service.sh logs        # follow the log
./service.sh restart     # reload after editing .env
./service.sh uninstall   # stop and remove
```

Installs a launchd agent at `~/Library/LaunchAgents/`. Background runs log to
`logs/monitor.log`, rotated at 1 MB with 3 backups.

`KeepAlive` uses `SuccessfulExit=false`, so a crash restarts but a deliberate
exit (such as a config error exiting 1) does not become a restart loop;
`ThrottleInterval` bounds retries to one per 30s. `.env` is loaded by absolute
path, because launchd starts the agent with an unrelated working directory and
a relative lookup would silently find nothing.

## Alerts

Crossing the threshold raises a native macOS notification:

```
Grid draw 1,450 W
Over the 100 W threshold
Solar 210 W  Load 1,660 W
```

Uses `osascript`, which ships with macOS — no extra dependency. If
`terminal-notifier` is installed it is preferred, because its `-group` flag
replaces the previous banner instead of stacking a new one per alert.

If notifications never appear, grant permission to the process that posts them:
**System Settings → Notifications**, then allow **Script Editor** (osascript) or
**terminal-notifier**.

Set `WEBHOOK_URL` to also POST JSON:

```json
{
  "event": "grid_draw_exceeded",
  "watts": 250.0,
  "threshold_watts": 100.0,
  "source": "sems",
  "source_detail": "station a1b2c3d4",
  "timestamp": "2026-01-01T12:00:00+00:00"
}
```

Debounce applies to every channel, so a sustained overdraw alerts once per
`DEBOUNCE_TIME` rather than every poll.

## Configuration

All settings live in `.env`; see [`.env.example`](.env.example) for the full
list with comments. The ones that matter most:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_SOURCE` | `local` | `local` or `sems` |
| `GRID_DRAW_THRESHOLD_WATTS` | `100` | Alert above this many watts drawn |
| `POLL_INTERVAL` | `10` | Seconds between reads (use 120+ for SEMS) |
| `DEBOUNCE_TIME` | `300` | Quiet period after an alert |
| `NOTIFY_MACOS` | on (macOS) | Native notification |
| `WEBHOOK_URL` | blank | Optional POST target |

---

## Things this project learned the hard way

Most of the value here is in the quirks. They cost real debugging time, and
none of them are documented upstream.

### `meter_p` does not exist

The commonly cited sensor name is not in `goodwe` 0.4.10. The real name varies
by inverter family, so the code auto-detects it and logs which it picked:

| Family | Sensor |
| --- | --- |
| ET/EH/BT/BH | `meter_active_power_total` |
| DT/MS/D-NS/XS | `meter_active_power` |
| ES/EM/BP | `pgrid` |

### Grid power is export-positive

Drawing 250 W from the grid reads as `-250`; exporting 800 W of solar reads as
`+800`. The library's own ES sensor is literally named *"On-grid Export Power"*,
and house consumption is derived as `pv + battery - pgrid`.

Comparing the raw value against a threshold therefore fires on **solar export**
— the exact opposite of grid draw. The code normalises this so "grid draw" is
positive.

### SEMS `gridStatus` is not a direction flag

It looks like one. It is not. Sampled live it read `1` in **both** directions:

| | pv | load | grid | gridStatus | pmeter | energy balance |
| --- | --- | --- | --- | --- | --- | --- |
| importing | 1692 | 1737 | 45 | 1 | −45 | `1692 + 45 = 1737` |
| exporting | 2023 | 2007 | 16 | 1 | +16 | `2023 − 16 = 2007` |

Trusting it reports export as import, firing false alerts on sunny days.
Direction is therefore taken from, in order:

1. **`pmeter`** — signed, and does track direction (export-positive).
2. **Energy balance** `load − pv − battery`, if `pmeter` is absent.
3. `grid` + `gridStatus`, last resort only, with a warning.

The battery term in step 2 is unverified — it was developed on a system without
storage, so every sample had `battery 0`. Re-check with `--probe` if you have a
battery.

### SEMS endpoint quirks

- Login returns an `api` field redirecting to a **regional host**
  (`hk.`, `eu.`, `au.`…). Ignore it and every later call fails.
- The `PowerStation/*` **listing** endpoints can answer `"ver is not fund"`
  (code 100000) regardless of the `version` sent in the Token header —
  including the obvious `GetPowerStationIdByOwner`. Station discovery falls
  back to `v2/HistoryData/QueryPowerStationByHistory`.
- `GetMonitorDetailByPowerstationId` works fine *once you have a station id*;
  only discovery is affected.
- Re-authenticate only on the auth codes. Retrying `"ver is not fund"` just
  doubles your request rate.

### Local access may not work

Newer DTLS dongles answer discovery but refuse the data protocol. A dongle
replying `dongle@sn,dtls_port:8899,<serial>` with every TCP port closed cannot
be read by `goodwe` 0.4.10, which has no DTLS support. Options:

- Enable **Modbus TCP** in the SolarGo app, then set `INVERTER_PORT=502`.
- Use `DATA_SOURCE=sems`.
- Wire up **RS485** directly, bypassing the dongle.

### Discovery returns bytes, not an address

`goodwe.search_inverters()` answers a raw byte string, and on newer dongles the
body contains **no IP at all**. This project reads the address from the UDP
**source** instead, which works either way.

It also broadcasts to the interface subnet address (`192.168.x.255`) before
`255.255.255.255`, because macOS often will not route the latter — a common
reason discovery "fails" on a perfectly healthy LAN.

## Requirements

Python 3.9+ (tested on 3.9 and 3.13). macOS is needed for notifications and the
login agent; the monitoring itself is cross-platform — set `NOTIFY_MACOS=false`
and use `WEBHOOK_URL` elsewhere.

## License

MIT — see [LICENSE](LICENSE).
