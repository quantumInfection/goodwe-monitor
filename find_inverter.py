#!/usr/bin/env python3
"""Find the inverter's current IP on the local network.

    ./venv/bin/python find_inverter.py           # one-shot scan
    ./venv/bin/python find_inverter.py --watch   # re-scan every 30s

Broadcasts the GoodWe discovery command on UDP 48899 and reports whoever
answers. The IP comes from the UDP source address, so this works even with
dongles whose reply body omits the address.
"""

from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from datetime import datetime

from monitor import (
    DISCOVERY_PAYLOAD,
    DISCOVERY_PORT,
    _broadcast_addresses,
    _broadcast_probe,
)


def probe_data_port(ip: str, timeout: float = 2.0) -> str:
    """Check whether the plain (non-DTLS) UDP data protocol answers on 8899."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        sock.sendto(bytes.fromhex("aa55c07f0102000241"), (ip, 8899))
        sock.recvfrom(1024)
        return "responds to plain UDP -- monitor.py can read it"
    except socket.timeout:
        return "no reply on 8899 (DTLS-only dongle? see README)"
    except OSError as err:
        return f"error: {err}"
    finally:
        sock.close()


def arp_lookup(ip: str) -> str:
    try:
        out = subprocess.run(
            ["arp", "-n", ip], capture_output=True, text=True, timeout=5
        ).stdout
        for part in out.split():
            if part.count(":") == 5:
                return part
    except (OSError, subprocess.SubprocessError):
        pass
    return "unknown"


def scan() -> list[tuple[str, bytes]]:
    print(f"Broadcasting on {', '.join(_broadcast_addresses())} "
          f"(UDP {DISCOVERY_PORT}, payload {DISCOVERY_PAYLOAD.decode()})")
    replies = _broadcast_probe(3.0)
    if not replies:
        print("  No inverter answered.")
        print("  Check: same network/VLAN as the dongle, no AP client isolation,")
        print("  and that the dongle is powered (it sleeps when the PV is dark).")
        return []
    for ip, payload in replies:
        print(f"  FOUND {ip}")
        print(f"    MAC     : {arp_lookup(ip)}")
        print(f"    reply   : {payload!r}")
        print(f"    port8899: {probe_data_port(ip)}")
    return replies


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--watch", action="store_true", help="re-scan on a loop")
    parser.add_argument("--interval", type=float, default=30.0)
    args = parser.parse_args()

    if not args.watch:
        return 0 if scan() else 1

    last: str | None = None
    print("Watching for IP changes. Ctrl-C to stop.\n")
    try:
        while True:
            print(datetime.now().strftime("%H:%M:%S"))
            replies = scan()
            current = replies[0][0] if replies else None
            if current and last and current != last:
                print(f"  !! IP CHANGED: {last} -> {current}")
                print(f"  !! update INVERTER_IP in .env")
            if current:
                last = current
            print()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print("Stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
