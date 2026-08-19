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

### A sleeping Mac does not monitor anything

This is the failure mode most likely to catch you out, because it looks
identical to "working fine, nothing to report": **no alerts, no errors, and a
process that still says `RUNNING`.**

A login agent only runs while the machine is awake. On a laptop that sleeps
whenever the lid closes, coverage collapses. Measured over one week on the
development machine, against an expected 720 polls/day at a 120s interval:

```
2026-08-13   157/720   21.8%
2026-08-15     8/720    1.1%
2026-08-17   435/720   60.4%
2026-08-19     3/720    0.4%
```

Three readings in a day is not monitoring. Grid draw does not wait for you to
open your laptop.

The agent therefore runs under `caffeinate -s`, which holds a "do not sleep"
assertion for exactly as long as the monitor lives. The `-s` flag applies
**only on AC power**, so battery life is untouched, and the assertion is
released the moment the service stops.

Confirm it is actually in force:

```bash
pmset -g assertions | grep -A1 PreventSystemSleep
# caffeinate asserting on behalf of '.../venv/bin/python' (pid NNN)
```

Caveats worth knowing:

- **Closing the lid still sleeps the machine.** `-s` prevents *idle* sleep, not
  clamshell sleep. Leave the lid open, or attach an external display.
- On battery the assertion does nothing by design, so coverage drops again.
- To let the Mac sleep normally, remove `/usr/bin/caffeinate` and `-s` from
  `ProgramArguments` in the plist and re-run `./service.sh install`.

If you want genuine 24/7 coverage, run it on something that is always on. It
reads the SEMS cloud rather than your LAN, so a Raspberry Pi or a small VPS
works — point `WEBHOOK_URL` at a push service to get alerts on your phone.

To check the monitor is alive rather than merely installed, look at how fresh
the log is — `./service.sh status` prints the last few readings with
timestamps. Stale timestamps are the tell.

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

Uses `osascript`, which ships with macOS and needs no install or permission
step. Selectable with `NOTIFIER`:

| `NOTIFIER` | Behaviour |
| --- | --- |
| `osascript` | **Default.** Built in, works out of the box. |
| `terminal-notifier` | Groups banners via `-group` instead of stacking one per alert — but see the warning below. |
| `auto` | `terminal-notifier` when installed, else `osascript`. |

> **`terminal-notifier` fails silently until you authorise it.** macOS discards
> its notifications entirely if it has not been granted permission, and it does
> not appear under System Settings → Notifications until it has registered
> there. It still exits 0, so the log reports success and nothing is shown.
>
> This was not theoretical: installing it and preferring it automatically
> replaced a working `osascript` setup with one that delivered nothing for a
> week, while logging `Notification shown` at every alert. Hence the default,
> and hence `auto` is not it.

**Neither backend can tell you whether a banner was actually seen.** Both exit 0
regardless. If you are debugging silence, test the backends against each other
rather than trusting exit codes:

```bash
python -c "import monitor; monitor._notify_macos('A','osascript','see me?','Glass','osascript')"
python -c "import monitor; monitor._notify_macos('B','terminal-notifier','see me?','Glass','terminal-notifier')"
```

If neither appears, grant permission under **System Settings → Notifications**
(Script Editor for `osascript`), and check Focus/Do Not Disturb is off.

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

## Quiet hours and snoozing

Solar only produces for part of the day. Overnight your house draws from the
grid no matter what, so alerting on it is noise you cannot act on. Three ways
to mute, all of which keep polling and keep the dashboard live — they only
suppress the alert:

**Scheduled window** — in `.env`:

```ini
ACTIVE_HOURS=05:00-19:15   # blank = always; may wrap midnight, e.g. 22:00-06:00
ACTIVE_DAYS=mon-fri        # blank = every day; also mon,wed,sat
```

Set these from your **own** production curve, not a guess. Check when your
panels actually start and stop:

```bash
./venv/bin/python sems.py --curve          # today
./venv/bin/python sems.py --curve 3        # last 3 complete days
```

A window that starts too late mutes real morning draw; one that ends too late
alerts every evening after the sun is gone, when there is nothing to act on.

**Solar-based** — mute while the sun is effectively down, which self-adjusts
across seasons instead of needing the clock edited twice a year:

```ini
MIN_PV_WATTS=200           # mute while solar output is below this; 0 disables
```

**Ad-hoc snooze** — takes effect immediately, no restart needed, and works on
an already-running login agent:

```bash
./service.sh snooze 2h     # also 30m, 1h30m, or a bare number of minutes
./service.sh unsnooze
./service.sh status        # shows whether alerts are muted, and why
```

A snooze outranks the scheduled window. Muted periods do **not** consume the
debounce, so the first actionable reading after a quiet period alerts straight
away rather than waiting out a timer.

`ACTIVE_HOURS` and `MIN_PV_WATTS` are read at startup — run
`./service.sh restart` after editing them. Snoozing is a state file, so it
applies instantly.

## If you add a battery

Short answer: yes, it stays useful — but **the quiet-hours advice above
inverts, and leaving it as-is would mute the most valuable alert you have.**

Without storage, overnight grid draw is unavoidable. There is no sun and no
reserve, so an alert tells you nothing you can act on — hence `ACTIVE_HOURS`
and `MIN_PV_WATTS`.

With storage, overnight grid draw means **the battery is not covering the
load**. It is flat, faulted, throttled by a BMS alarm, or reserving charge
because of a misconfigured backup floor. That is the single most diagnostic
signal the system produces, and a daylight-only window hides it completely.

So when you commission a battery:

```ini
ACTIVE_HOURS=          # blank — night draw is now the interesting case
MIN_PV_WATTS=0         # low PV no longer means "nothing to act on"
```

The threshold alone becomes the useful signal, around the clock.

### Re-verify the sign first

The energy-balance fallback computes `grid = load − pv − battery`, which
assumes **positive battery = discharging** (supplying the house). That matches
the library's own local formula, `house_consumption = pv + pbattery1 −
active_power`.

It has never been exercised against real storage. Every sample this was built
on had `battery 0`, so the term always vanished. With a battery it becomes
load-bearing, and if SEMS reports charging as positive instead, grid draw will
be wrong by twice the battery power.

Check it on the first sunny afternoon while the battery is charging:

```bash
./venv/bin/python sems.py --probe
```

The reported grid figure should agree with the SEMS app. It is only the
fallback path that is at risk — `pmeter` is preferred and is independent of
the battery term.

Also treat `betteryStatus` with the same suspicion as `gridStatus`, which
[turns out not to track direction](#sems-gridstatus-is-not-a-direction-flag).
Do not assume the charge/discharge flag is trustworthy without checking it
against the energy balance.

### Battery presence is not `battery_count`

A hybrid inverter with an empty bay still reports `battery_count: 1` and a
populated `more_batterys` entry, with `vbattery`, `soh`, and power all zero.
Detection therefore looks for physical evidence — voltage, state of health, or
actual power flow — rather than trusting the count.

This is why state of charge appears only when a battery is really fitted, and
why it is shown **even at 0%**: on a system with storage, a flat battery is
precisely the reading you need to see.

### Alerts worth adding

A plain grid-draw threshold no longer captures everything once storage exists.
The data for these is already collected (`stats["battery"]` and
`stats["soc"]`); the rules are not implemented:

| Condition | Why it matters |
| --- | --- |
| SOC below a reserve floor | You are about to lose backup capability |
| Grid import while SOC is healthy | Battery should be covering this — fault or bad config |
| Battery charging *from the grid* | Costs money unless you are on a cheap tariff |
| Exporting while the battery is not full | Charge priority is misconfigured |

One caveat in the other direction: if you deliberately charge from the grid on
a cheap night tariff, that is a large, intentional import which the plain
threshold will flag. Either schedule `ACTIVE_HOURS` around the tariff window,
or `./service.sh snooze` for its duration.

## Configuration

All settings live in `.env`; see [`.env.example`](.env.example) for the full
list with comments. The ones that matter most:

| Variable | Default | Meaning |
| --- | --- | --- |
| `DATA_SOURCE` | `local` | `local` or `sems` |
| `GRID_DRAW_THRESHOLD_WATTS` | `100` | Alert above this many watts drawn |
| `POLL_INTERVAL` | `10` | Seconds between reads (use 120+ for SEMS) |
| `DEBOUNCE_TIME` | `300` | Quiet period after an alert |
| `ACTIVE_HOURS` | blank | Only alert in this window, e.g. `05:00-19:15` |
| `ACTIVE_DAYS` | blank | Only alert on these days, e.g. `mon-fri` |
| `MIN_PV_WATTS` | `0` | Mute while solar is below this |
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
storage, so every sample had `battery 0`. See
[If you add a battery](#if-you-add-a-battery) before relying on it.

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
- **SEMS sessions are single-use per account.** Two instances polling the same
  login invalidate each other's token and re-authenticate on *every* poll —
  measured at 1 login per 3 polls with one instance, versus 4 with two. Do not
  leave `--watch` running in a terminal while the login agent is also running;
  use `./service.sh logs` to watch the agent instead.

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
