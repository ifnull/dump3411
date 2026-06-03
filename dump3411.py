#!/usr/bin/env python3
"""
dump3411 / dump3411.py

Single-process orchestrator. Runs both radios (BLE + WiFi) as daemon threads
feeding one shared in-memory ``Tracker``, plus a stdlib HTTP server that
serves the tracker snapshot at ``/data/remoteid.json``. See FEED.md.

Usage:
    sudo python3 dump3411.py --wifi-iface wlan1 --serve 0.0.0.0:8754

The ``--serve`` flag is optional. Without it the process detects-and-journals
exactly like the standalone feeders but maintains the in-memory cache too —
useful if a future tool reads the tracker over some other interface.

The standalone ``ble_feeder.py`` and ``wifi_feeder.py`` scripts remain
runnable on their own for debugging a single radio in isolation.
"""

import argparse
import asyncio
import logging
import signal
import sys
import threading
import time
from typing import Tuple

import feed_server
from ble_feeder import BLEFeeder
from tracker import Tracker
from wifi_feeder import WiFiFeeder

# Logging is configured (root) by the feeder modules at import time; reuse it.
log = logging.getLogger("dump3411.main")


# -- CLI -----------------------------------------------------------------------

def _parse_addr(s: str) -> Tuple[str, int]:
    """Parse ``--serve`` argument: ``HOST:PORT``, ``:PORT``, or just ``PORT``."""
    if ":" in s:
        host, port_str = s.rsplit(":", 1)
    else:
        host, port_str = "", s
    return (host or "0.0.0.0"), int(port_str)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="dump3411 unified Remote ID detector + JSON feed (offline)",
    )
    p.add_argument(
        "--ble-adapter", default="hci0",
        help="HCI adapter for BLE scan (default: hci0)",
    )
    p.add_argument(
        "--wifi-iface", default="wlan1",
        help="USB monitor-mode interface for WiFi capture (default: wlan1)",
    )
    p.add_argument(
        "--channel-dwell", type=float, default=0.2,
        help="Seconds per channel before WiFi hopper moves on (default: 0.2)",
    )
    p.add_argument(
        "--serve", default=None, metavar="HOST:PORT",
        help="Serve /data/remoteid.json on HOST:PORT (e.g. 0.0.0.0:8754). "
             "Omit for detection-only mode.",
    )
    p.add_argument(
        "--ttl", type=float, default=60.0,
        help="Seconds of no-messages before a drone is dropped from the feed "
             "(default: 60)",
    )
    p.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every decoded message, not just Basic ID / Location",
    )
    return p.parse_args()


# -- Main ----------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    tracker = Tracker(ttl_seconds=args.ttl)
    ble  = BLEFeeder(adapter=args.ble_adapter, verbose=args.verbose,
                     tracker=tracker)
    wifi = WiFiFeeder(iface=args.wifi_iface, verbose=args.verbose,
                      channel_dwell=args.channel_dwell, tracker=tracker)

    # Daemon threads: radios produce, sweeper expires stale entries.
    threading.Thread(target=lambda: asyncio.run(ble.run()),
                     daemon=True, name="ble").start()
    threading.Thread(target=wifi.run,
                     daemon=True, name="wifi").start()
    threading.Thread(target=tracker.sweep_loop,
                     daemon=True, name="sweeper").start()

    # Shutdown coordination — main thread either blocks in serve_forever or on
    # a flag; signal handlers nudge it.
    shutdown = threading.Event()
    server   = None
    if args.serve:
        host, port = _parse_addr(args.serve)
        server = feed_server.make_server((host, port), tracker)
        log.info(f"feed listening on http://{host}:{port}/data/remoteid.json")
    else:
        log.info("detection-only mode (no --serve)")

    def _handle_term(signum, _frame):
        log.info(f"signal {signum} received — shutting down")
        shutdown.set()
        if server is not None:
            # server.shutdown() must run off the serving thread.
            threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_term)
    signal.signal(signal.SIGINT,  _handle_term)

    try:
        if server is not None:
            server.serve_forever()
        else:
            while not shutdown.wait(1.0):
                pass
    finally:
        wifi.stop()
        tracker.stop()
        # Wifi cleanup (restore_managed_mode) runs in its thread's finally.
        # Give it a moment before the process exits and kills daemon threads.
        time.sleep(1.5)
        if server is not None:
            server.server_close()
        log.info("dump3411 exiting")


if __name__ == "__main__":
    main()
