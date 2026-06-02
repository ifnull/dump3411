#!/usr/bin/env python3
"""
drone-aware-zero / feed_server.py

Tiny stdlib HTTP server that serves the tracker's snapshot at
``/data/remoteid.json``. See FEED.md for the wire contract.

Constraints from FEED.md "Producer obligations":
  * **Snapshot-only.** No decoding, conversion, or computation under request;
    everything is already done inside ``Tracker.snapshot()``. The handler
    grabs the snapshot dict, serialises, and returns.
  * **Stdlib only** — ``http.server`` + ``json``. No async, no extra deps.

The server is threaded so multiple LAN consumers can poll concurrently
without serialising on one request. The handler also responds to ``HEAD``
and emits no per-request access log (otherwise the journal would gain one
line per consumer poll).
"""

import http.server
import json
import logging
from typing import Tuple

from tracker import Tracker

log = logging.getLogger("droneaware.feed")


# -- Request handler -----------------------------------------------------------

class _Handler(http.server.BaseHTTPRequestHandler):
    """Per-request handler.  ``tracker`` is bound at subclass-creation time
    in :func:`make_server` so this class can be plain BaseHTTPRequestHandler."""

    tracker: Tracker        # filled in by make_server()
    server_version = "drone-aware-zero/1"
    sys_version    = ""     # suppress the default "Python/3.x" Server suffix

    def do_GET(self) -> None:
        if self.path != "/data/remoteid.json":
            self.send_error(404, "Not Found")
            return
        try:
            body = self._encode_snapshot()
        except Exception:
            log.exception("snapshot failed")
            self.send_error(500, "Internal Server Error")
            return
        self._send_headers(len(body))
        self.wfile.write(body)

    def do_HEAD(self) -> None:
        if self.path != "/data/remoteid.json":
            self.send_error(404, "Not Found")
            return
        try:
            body = self._encode_snapshot()
        except Exception:
            log.exception("snapshot failed")
            self.send_error(500, "Internal Server Error")
            return
        self._send_headers(len(body))

    def _encode_snapshot(self) -> bytes:
        # Tracker.snapshot() acquires the cache lock briefly, copies state into
        # a plain dict, releases. JSON serialisation happens lock-free here.
        return json.dumps(
            self.tracker.snapshot(), separators=(",", ":")
        ).encode("utf-8")

    def _send_headers(self, body_len: int) -> None:
        self.send_response(200)
        self.send_header("Content-Type",                "application/json")
        self.send_header("Content-Length",              str(body_len))
        self.send_header("Cache-Control",               "no-store")
        # LAN tool, public-airspace payload — let browser consumers poll too.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

    def log_message(self, *_args) -> None:
        """Silence the default per-request stderr access log."""
        pass


# -- Server constructors -------------------------------------------------------

def make_server(addr: Tuple[str, int], tracker: Tracker) -> http.server.ThreadingHTTPServer:
    """Build a ThreadingHTTPServer bound to ``addr`` serving ``tracker``.

    Returns the server instance so the caller can ``serve_forever()`` it on
    any thread and ``shutdown()`` it cleanly (used by the standalone test
    below).
    """
    handler_cls = type("Handler", (_Handler,), {"tracker": tracker})
    return http.server.ThreadingHTTPServer(addr, handler_cls)


def serve(addr: Tuple[str, int], tracker: Tracker) -> None:
    """Build a server and block in ``serve_forever``.

    This is the entry point ``droneaware.py`` uses on its main thread.
    Returns when ``KeyboardInterrupt`` is raised (SIGINT) or when something
    else calls ``server.shutdown()``.
    """
    server = make_server(addr, tracker)
    host, port = addr
    log.info(f"feed listening on http://{host}:{port}/data/remoteid.json")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# -- Standalone smoke test -----------------------------------------------------

if __name__ == "__main__":
    import threading
    import urllib.error
    import urllib.request

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    t = Tracker(ttl_seconds=60.0)
    t.update_basic_id(
        mac="aa:bb:cc:dd:ee:01", uas_id="158190SK3X2YB7",
        id_type_raw=1, ua_type_raw=2,
        rssi=-62.0, rid_source="ble",
    )
    t.update_location(
        mac="aa:bb:cc:dd:ee:01",
        lat=40.7128, lon=-74.0060,
        alt_geo_m=125.5, height_agl_m=115.0,
        gs_mps=8.2, heading_deg=271.0, vspeed_mps=-3.25,
        rssi=-60.0, rid_source="ble",
    )

    server = make_server(("127.0.0.1", 0), t)        # ephemeral port
    host, port = server.server_address
    threading.Thread(target=server.serve_forever, daemon=True).start()

    try:
        # 1) Good path.
        url = f"http://{host}:{port}/data/remoteid.json"
        with urllib.request.urlopen(url, timeout=2) as r:
            body    = r.read()
            doc     = json.loads(body)
            headers = {k: r.headers[k] for k in
                       ("Content-Type", "Cache-Control",
                        "Access-Control-Allow-Origin", "Server")}
        print(f"GET /data/remoteid.json -> {r.status}")
        for k, v in headers.items():
            print(f"  {k}: {v}")
        print(f"  body: {len(body)} bytes, drones={len(doc['drones'])}, "
              f"schema_v={doc['schema_version']}, messages={doc['messages']}")
        assert doc["schema_version"] == 1
        assert doc["drones"][0]["id"]          == "158190SK3X2YB7"
        assert doc["drones"][0]["lat"]         == 40.7128
        assert doc["drones"][0]["alt_geom_ft"] == round(125.5 * 3.28084, 1)
        assert headers["Content-Type"]                == "application/json"
        assert headers["Cache-Control"]               == "no-store"
        assert headers["Access-Control-Allow-Origin"] == "*"

        # 2) HEAD same path — headers only, empty body.
        req = urllib.request.Request(url, method="HEAD")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200
            assert r.read() == b""
            print(f"HEAD /data/remoteid.json -> {r.status} (no body, ok)")

        # 3) Wrong path -> 404.
        try:
            urllib.request.urlopen(
                f"http://{host}:{port}/data/aircraft.json", timeout=2
            )
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as e:
            assert e.code == 404
            print(f"GET /data/aircraft.json -> {e.code} (correct)")

        print("OK")
    finally:
        server.shutdown()
        server.server_close()
