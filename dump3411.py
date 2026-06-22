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
import os
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
    # MQTT publisher — optional. Each flag falls back to an env var so the
    # systemd unit can pull credentials from an EnvironmentFile without
    # exposing them via `systemctl cat`.
    p.add_argument(
        "--mqtt-broker", default=os.environ.get("MQTT_BROKER"),
        metavar="HOST[:PORT]",
        help="Publish detections to an MQTT broker (e.g. mqtt.lan:1883). "
             "Also read from $MQTT_BROKER. Requires paho-mqtt.",
    )
    p.add_argument(
        "--mqtt-topic-prefix",
        default=os.environ.get("MQTT_TOPIC_PREFIX", "dump3411"),
        help="MQTT topic prefix (default: dump3411). "
             "Also read from $MQTT_TOPIC_PREFIX.",
    )
    p.add_argument(
        "--mqtt-user", default=os.environ.get("MQTT_USER"),
        help="MQTT username. Also read from $MQTT_USER.",
    )
    p.add_argument(
        "--mqtt-password", default=os.environ.get("MQTT_PASSWORD"),
        help="MQTT password. Also read from $MQTT_PASSWORD.",
    )
    # Persistent history — optional. Disabled by default for SD-card safety.
    p.add_argument(
        "--history-db", default=os.environ.get("HISTORY_DB"),
        metavar="PATH",
        help="Persist per-message detections to a SQLite log at PATH. "
             "Also read from $HISTORY_DB. Disabled when unset.",
    )
    p.add_argument(
        "--history-max-mb", type=float,
        default=float(os.environ.get("HISTORY_MAX_MB", "100")),
        help="Cap the history DB at N megabytes (default: 100). "
             "Also read from $HISTORY_MAX_MB.",
    )
    p.add_argument(
        "--history-retention-days", type=float,
        default=float(os.environ.get("HISTORY_RETENTION_DAYS", "30")),
        help="Drop history rows older than N days (default: 30). "
             "Also read from $HISTORY_RETENTION_DAYS.",
    )
    p.add_argument(
        "--history-debounce-s", type=float,
        default=float(os.environ.get("HISTORY_DEBOUNCE_S", "1.0")),
        help="Min seconds between per-drone history writes (default: 1.0). "
             "Also read from $HISTORY_DEBOUNCE_S.",
    )
    p.add_argument(
        "--history-recent-days", type=float,
        default=float(os.environ.get("HISTORY_RECENT_DAYS", "7")),
        help="Lookback window for the dashboard's Recent detections table "
             "and /history/recent.json's default (default: 7 days). "
             "Also read from $HISTORY_RECENT_DAYS.",
    )
    return p.parse_args()


# -- Main ----------------------------------------------------------------------

def main() -> None:
    args = _parse_args()

    tracker = Tracker(ttl_seconds=args.ttl)

    # MQTT publisher — only built if a broker is configured. Constructed before
    # the radios so the tracker callbacks are wired before any message arrives.
    publisher = None
    if args.mqtt_broker:
        try:
            from mqtt_publisher import MqttPublisher
        except ImportError as e:
            log.error("--mqtt-broker requires paho-mqtt: %s", e)
            sys.exit(2)
        try:
            publisher = MqttPublisher(
                broker=args.mqtt_broker,
                topic_prefix=args.mqtt_topic_prefix,
                username=args.mqtt_user,
                password=args.mqtt_password,
                tracker=tracker,
            )
        except RuntimeError as e:
            log.error("%s", e)
            sys.exit(2)

    # Persistent history writer — also optional.
    history = None
    if args.history_db:
        from history import HistoryWriter
        history = HistoryWriter(
            db_path=args.history_db,
            max_mb=args.history_max_mb,
            retention_days=args.history_retention_days,
            debounce_s=args.history_debounce_s,
            recent_days=args.history_recent_days,
        )

    # Fan tracker callbacks out to every configured sink. Each sink gets the
    # imperial row dict that goes into the JSON feed — same source of truth.
    on_change_sinks = []
    on_expire_sinks = []
    if publisher is not None:
        on_change_sinks.append(publisher.on_drone_change)
        on_expire_sinks.append(publisher.on_drone_expire)
    if history is not None:
        on_change_sinks.append(history.on_drone_change)
        # History intentionally has no expire hook — TTL eviction in the
        # tracker doesn't mean we should forget the flight on disk.

    def _fan_change(uas_id, row, _sinks=on_change_sinks):
        for cb in _sinks:
            try:
                cb(uas_id, row)
            except Exception:
                log.exception("on_change sink raised for %s", uas_id)

    def _fan_expire(uas_id, _sinks=on_expire_sinks):
        for cb in _sinks:
            try:
                cb(uas_id)
            except Exception:
                log.exception("on_expire sink raised for %s", uas_id)

    if on_change_sinks or on_expire_sinks:
        tracker.set_callbacks(
            on_change=_fan_change if on_change_sinks else None,
            on_expire=_fan_expire if on_expire_sinks else None,
        )

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
        server = feed_server.make_server((host, port), tracker, history=history)
        log.info(f"feed listening on http://{host}:{port}/data/remoteid.json")
        if history is not None:
            log.info(f"history at {args.history_db}; map view: "
                     f"http://{host}:{port}/map?uas_id=<UAS-ID>")
    else:
        log.info("detection-only mode (no --serve)")

    if publisher is not None:
        publisher.start()
    if history is not None:
        history.start()

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
        if publisher is not None:
            publisher.stop()
        if history is not None:
            history.stop()
        # Wifi cleanup (restore_managed_mode) runs in its thread's finally.
        # Give it a moment before the process exits and kills daemon threads.
        time.sleep(1.5)
        if server is not None:
            server.server_close()
        log.info("dump3411 exiting")


if __name__ == "__main__":
    main()
