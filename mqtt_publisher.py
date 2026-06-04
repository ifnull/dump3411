#!/usr/bin/env python3
"""
dump3411 / mqtt_publisher.py

Publishes detections to an MQTT broker so consumers (Home Assistant
automations, Node-RED, custom scripts, …) can react without polling the
JSON feed. Wires in via Tracker callbacks; no work happens on the radio
threads beyond ``dict[uas_id] = row`` (state) or ``queue.put_nowait``
(events).

Topic layout (under ``--mqtt-topic-prefix``, default ``dump3411``):

  <prefix>/online                  retained, QoS 1
                                   "online" on connect; LWT publishes
                                   "offline" if the process dies.

  <prefix>/status                  retained, QoS 1, every ~5 s
                                   mirrors GET /status (uptime, per-source
                                   counters, drones_active, cpu_temp_c).

  <prefix>/drones/<uas_id>         retained, QoS 1, debounced
                                   per-drone state, same shape as one row of
                                   drones[] in /data/remoteid.json (imperial,
                                   matching FEED.md).
                                   Empty (zero-byte) retained payload is
                                   published when a drone TTL-evicts, so
                                   subscribers see the removal.

  <prefix>/events/detection        QoS 0, not retained
                                   one publish per decoded message:
                                   { uas_id, rid_source, rssi, t }.

Per-drone state is **latest-wins**: every change overwrites a small
``_latest[uas_id]`` dict, and the publisher thread drains that dict no
more than once per ``DEBOUNCE_S`` per drone. That way a Location that
arrives 100 ms after a Basic ID still reaches the retained topic — it
just lands at the end of the debounce window instead of immediately.
The first publish for a freshly-seen drone goes out without waiting.

Units mirror FEED.md (imperial). Optional dependency: paho-mqtt — if a
broker is configured without it installed, dump3411 errors out with a
clear install hint at startup.
"""

import json
import logging
import os
import queue
import threading
import time
from typing import Optional, Tuple

log = logging.getLogger("dump3411.mqtt")


# Lazy-ish import so this module can be imported even without paho-mqtt; the
# error only fires when someone tries to construct an MqttPublisher.
try:
    import paho.mqtt.client as mqtt          # type: ignore[import-not-found]
    HAVE_PAHO = True
except ImportError:                          # pragma: no cover - import-time branch
    mqtt = None                              # type: ignore[assignment]
    HAVE_PAHO = False


def _make_client(client_id: str):
    """Build a paho.mqtt.client.Client compatible with both 1.x and 2.x.

    paho 2.x added CallbackAPIVersion and made it required. Targeting v1
    callback signatures keeps our code identical across both library versions.
    """
    if hasattr(mqtt, "CallbackAPIVersion"):
        return mqtt.Client(                                                       # type: ignore[attr-defined]
            mqtt.CallbackAPIVersion.VERSION1,
            client_id=client_id, clean_session=True,
        )
    return mqtt.Client(client_id=client_id, clean_session=True)


def _safe_topic_segment(s: str) -> str:
    """Sanitise a string for use as an MQTT topic level.

    UAS IDs *should* be ASCII alphanumeric (ANSI/CTA-2063-A), but spoofers
    and malformed transmitters can emit arbitrary bytes — including '/',
    '+', '#', and NUL, all of which break or are reserved by MQTT.
    """
    out = [c if (c.isalnum() or c in "-._") else "_" for c in s]
    return "".join(out) or "unknown"


class MqttPublisher:
    """Bridges Tracker callbacks to an MQTT broker.

    Snapshot-only: the publisher does no decoding or unit conversion; the
    Tracker hands it the same imperial row dict that goes into the JSON
    feed. The publisher only does topic routing and rate-limiting.
    """

    DEBOUNCE_S       = 1.0     # at most one per-drone state publish per second
    STATUS_INTERVAL  = 5.0     # status snapshot cadence
    PUB_TICK_S       = 0.1     # how often the pub thread checks pending state
    EVENT_QUEUE_MAX  = 2000    # drop-oldest cap; radio threads never block

    def __init__(self, broker: str, topic_prefix: str = "dump3411",
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 client_id: Optional[str] = None,
                 tracker=None):
        if not HAVE_PAHO:
            raise RuntimeError(
                "paho-mqtt is not installed. Install with one of:\n"
                "  sudo apt install python3-paho-mqtt   (Debian/Ubuntu)\n"
                "  pip install paho-mqtt"
            )

        self._host, self._port = self._parse_broker(broker)
        self._prefix = topic_prefix.rstrip("/")
        self._tracker = tracker

        self._t_online = f"{self._prefix}/online"
        self._t_status = f"{self._prefix}/status"
        self._t_drones = f"{self._prefix}/drones"
        self._t_event  = f"{self._prefix}/events/detection"

        client_id = client_id or f"dump3411-{os.uname().nodename}"
        self._client = _make_client(client_id)
        if username:
            self._client.username_pw_set(username, password or "")
        self._client.will_set(self._t_online, payload="offline", qos=1, retain=True)
        self._client.on_connect = self._on_connect

        # Latest pending state per drone (latest-wins; debounce-publish).
        self._latest: dict[str, dict] = {}
        self._latest_lock = threading.Lock()
        # Per-drone monotonic timestamp of the most recent publish. `None`
        # for "never published" so the first sighting publishes immediately
        # rather than waiting out the debounce window.
        self._last_pub: dict[str, Optional[float]] = {}
        # Event stream (detection events, expire events) — FIFO, drained
        # by the publisher thread.
        self._events: "queue.Queue[Tuple[str, str, Optional[dict]]]" = \
            queue.Queue(maxsize=self.EVENT_QUEUE_MAX)
        self._stop = threading.Event()
        self._pub_thread: Optional[threading.Thread]    = None
        self._status_thread: Optional[threading.Thread] = None

    # -- broker parsing ----------------------------------------------------

    @staticmethod
    def _parse_broker(s: str) -> Tuple[str, int]:
        """Accept 'host', 'host:port', 'mqtt://host', or 'mqtt://host:port'."""
        if "://" in s:
            s = s.split("://", 1)[1]
        if ":" in s:
            host, port = s.rsplit(":", 1)
            return host, int(port)
        return s, 1883

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        log.info("connecting to MQTT broker %s:%d", self._host, self._port)
        try:
            self._client.connect(self._host, self._port, keepalive=30)
        except Exception:
            log.exception("initial MQTT connect failed; paho will retry in the background")

        self._client.loop_start()
        self._pub_thread = threading.Thread(
            target=self._pub_loop, daemon=True, name="mqtt-pub",
        )
        self._status_thread = threading.Thread(
            target=self._status_loop, daemon=True, name="mqtt-status",
        )
        self._pub_thread.start()
        self._status_thread.start()

    def stop(self) -> None:
        log.info("shutting down MQTT publisher")
        self._stop.set()
        try:
            # One final pass so any debounced state goes out before we drop.
            self._flush_pending(force=True)
            self._client.publish(self._t_online, payload="offline",
                                 qos=1, retain=True)
            self._client.loop_stop()
            self._client.disconnect()
        except Exception:
            pass

    # -- Tracker callbacks (called from radio threads under the cache lock —
    #    must be fast and non-blocking) -----------------------------------

    def on_drone_change(self, uas_id: str, row: dict) -> None:
        # Latest state wins; publisher thread will pick it up on its next tick
        # (subject to per-drone debounce).
        with self._latest_lock:
            self._latest[uas_id] = row
        # A detection event (separate topic, no debounce) fires per message.
        try:
            self._events.put_nowait(("event", uas_id, row))
        except queue.Full:
            log.warning("MQTT event queue full, dropping detection for %s", uas_id)

    def on_drone_expire(self, uas_id: str) -> None:
        try:
            self._events.put_nowait(("expire", uas_id, None))
        except queue.Full:
            pass

    # -- background loops --------------------------------------------------

    def _pub_loop(self) -> None:
        """Publish any eligible pending state, drain any queued events,
        and sleep briefly. Runs until ``stop()`` is called."""
        while not self._stop.is_set():
            self._flush_pending(force=False)
            self._drain_events(budget_s=0.05)
            time.sleep(self.PUB_TICK_S)

    def _flush_pending(self, *, force: bool) -> None:
        """Publish any pending per-drone state whose debounce window has
        elapsed. When ``force`` is True, flush everything (called from stop)."""
        now = time.monotonic()
        ready: list[Tuple[str, dict]] = []
        with self._latest_lock:
            for uas_id, row in list(self._latest.items()):
                last = self._last_pub.get(uas_id)
                if force or last is None or (now - last) >= self.DEBOUNCE_S:
                    ready.append((uas_id, row))
                    self._last_pub[uas_id] = now
                    del self._latest[uas_id]
        for uas_id, row in ready:
            self._publish_state(uas_id, row)

    def _drain_events(self, *, budget_s: float) -> None:
        """Drain as many queued events as we can within ``budget_s`` seconds."""
        deadline = time.monotonic() + budget_s
        while time.monotonic() < deadline:
            try:
                kind, uas_id, payload = self._events.get_nowait()
            except queue.Empty:
                return
            try:
                self._handle_event(kind, uas_id, payload)
            except Exception:
                log.exception("event publish failed (%s, %s)", kind, uas_id)

    def _handle_event(self, kind: str, uas_id: str,
                      payload: Optional[dict]) -> None:
        if kind == "event":
            event = {
                "uas_id":     uas_id,
                "rid_source": (payload or {}).get("rid_source"),
                "rssi":       (payload or {}).get("rssi"),
                "t":          round(time.time(), 3),
            }
            self._client.publish(
                self._t_event,
                payload=json.dumps(event, separators=(",", ":")),
                qos=0, retain=False,
            )
        elif kind == "expire":
            seg = _safe_topic_segment(uas_id)
            self._client.publish(
                f"{self._t_drones}/{seg}",
                payload=b"",
                qos=1, retain=True,
            )
            # Clear our bookkeeping so a same-id drone reappearing later
            # publishes immediately rather than waiting out the debounce.
            with self._latest_lock:
                self._latest.pop(uas_id, None)
            self._last_pub.pop(uas_id, None)

    def _publish_state(self, uas_id: str, row: dict) -> None:
        seg = _safe_topic_segment(uas_id)
        self._client.publish(
            f"{self._t_drones}/{seg}",
            payload=json.dumps(row, separators=(",", ":")),
            qos=1, retain=True,
        )

    def _status_loop(self) -> None:
        """Periodic /status snapshot publish for HA-style monitoring."""
        while not self._stop.wait(self.STATUS_INTERVAL):
            if self._tracker is None:
                continue
            try:
                doc = self._tracker.health()
                self._client.publish(
                    self._t_status,
                    payload=json.dumps(doc, separators=(",", ":")),
                    qos=1, retain=True,
                )
            except Exception:
                log.exception("status publish failed")

    # -- connect callback --------------------------------------------------

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc == 0:
            log.info("MQTT connected to %s:%d", self._host, self._port)
            client.publish(self._t_online, payload="online", qos=1, retain=True)
        else:
            log.warning("MQTT connect returned rc=%d", rc)
