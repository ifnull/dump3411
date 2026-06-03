#!/usr/bin/env python3
"""
dump3411 / tracker.py

In-memory per-drone state aggregator that produces the snapshot served by the
``/data/remoteid.json`` HTTP feed. See FEED.md for the wire contract — this
module is the producer side of that contract.

Design rules (mirror of FEED.md "Producer obligations"):

  * **SI internally, consumer units on output.** Decoders pass metres,
    metres-per-second and degrees; ``snapshot()`` converts to ft, kt and
    ft/min so the journald output paths stay untouched.
  * **Identity is write-once.** ``id_type`` is set when the first Basic ID
    creates the entry. ``ua_type`` is set the first time a non-None value
    arrives and not overwritten afterwards. Everything else is most-recent
    wins (multi-transport: BLE and WiFi merge into one entry).
  * **Per-drone ``message_count``** increments on every decoded RID message
    regardless of transport. The envelope-level ``messages`` counter is the
    total across all drones.
  * **Three monotonic timestamps per drone:** ``last_seen`` (anything),
    ``last_pos_seen`` (Location/Vector), ``last_operator_seen``
    (System or Operator-ID). The ``seen`` fields in the snapshot are computed
    from these at serialize time.
  * **One ``threading.Lock`` guards the cache.** All public methods take it;
    private helpers assume it is held. The snapshot copies live state into a
    plain dict under the lock, then releases before JSON serialisation.

Only Basic ID carries the UAS ID on the wire — Location, System and
Operator-ID do not. We use a transmitter-MAC ↔ uas_id map (short-lived,
swept with the drone TTL) to associate non-identity messages with the right
drone. A Location received before any Basic ID for its MAC is dropped on the
floor; this is rare in practice because BLE rotates Basic IDs frequently and
WiFi Message Packs carry Basic ID alongside Location in the same beacon.

When the WiFi feeder unpacks a Message Pack, it must call
``update_basic_id`` for any Basic ID sub-message **before** the others in
that pack, so the MAC mapping is in place when ``update_location`` /
``update_system`` / ``update_operator_id`` look it up.
"""

import dataclasses
import threading
import time
from typing import Optional


# -- Unit conversions ----------------------------------------------------------

M_TO_FT     = 3.28084            # metres → feet
MPS_TO_KT   = 1.943844           # m/s    → knots
MPS_TO_FTPM = 196.8503937        # m/s    → feet/minute  (m/s · 60 · 3.28084)


# -- RID enum → wire strings ---------------------------------------------------
# Keep aligned with FEED.md "Detection object" table.

_ID_TYPE_STRINGS = {
    0: "unknown",
    1: "serial",        # ANSI/CTA-2063-A
    2: "caa_reg",       # CAA-assigned
    3: "utm_uuid",      # UTM-assigned
    4: "session",       # specific session ID (privacy mode)
}

_UA_TYPE_STRINGS = {
    0:   "none",
    1:   "aeroplane",
    2:   "multirotor",         # ASTM: "Helicopter (or Multirotor)"
    3:   "gyroplane",
    4:   "hybrid",
    5:   "ornithopter",
    6:   "glider",
    7:   "kite",
    8:   "free_balloon",
    9:   "captive_balloon",
    10:  "airship",
    11:  "parachute",
    12:  "rocket",
    13:  "tethered",
    14:  "ground_obstacle",
    255: "other",
}

SCHEMA_VERSION = 1


def _read_cpu_temp() -> Optional[float]:
    """Best-effort CPU temperature (°C). Returns None on non-Pi/Linux hosts."""
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


# -- Per-drone state -----------------------------------------------------------

@dataclasses.dataclass
class DroneState:
    """One drone's aggregated state. SI-native; ``snapshot()`` converts."""

    uas_id: str
    id_type: str                                   # mapped feed string
    ua_type: Optional[str] = None                  # mapped feed string

    # Position / velocity — most-recent wins.
    lat: Optional[float]          = None
    lon: Optional[float]          = None
    alt_geo_m: Optional[float]    = None
    height_agl_m: Optional[float] = None
    gs_mps: Optional[float]       = None
    heading_deg: Optional[float]  = None
    vspeed_mps: Optional[float]   = None

    # Last-message metadata — most-recent wins.
    rssi_dbm: Optional[float] = None
    rid_source: Optional[str] = None

    # Operator / System block — most-recent wins.
    op_lat: Optional[float]           = None
    op_lon: Optional[float]           = None
    op_alt_takeoff_m: Optional[float] = None
    operator_id: Optional[str]        = None

    # Counters and timestamps (monotonic, intervals only).
    message_count: int                  = 0
    last_seen: float                    = 0.0
    last_pos_seen: Optional[float]      = None
    last_operator_seen: Optional[float] = None


# -- Tracker -------------------------------------------------------------------

class Tracker:
    """Thread-safe per-drone aggregator.

    Decoders call ``update_*`` with SI-native values whenever an RID message
    is decoded. ``snapshot()`` returns the full ``/data/remoteid.json``
    document with consumer-unit conversions applied.

    A short-lived MAC → uas_id map associates non-Basic-ID messages
    (Location, System, Operator-ID) with the right drone, because only
    Basic ID carries the UAS ID on the wire. See FEED.md.
    """

    def __init__(self, ttl_seconds: float = 60.0):
        self._lock = threading.Lock()
        self._drones: dict[str, DroneState] = {}    # uas_id -> state
        self._mac_to_uas: dict[str, str] = {}       # mac    -> uas_id
        self._ttl = ttl_seconds
        self._messages_total = 0
        self._stop = threading.Event()
        # Per-source rolling stats for the /status dashboard endpoint.
        self._by_source: dict[str, dict] = {}       # rid_source -> {messages, last_seen}
        self._boot_mono = time.monotonic()

    # -- updates ---------------------------------------------------------------

    def update_basic_id(self, *, mac: str, uas_id: str,
                        id_type_raw: int, ua_type_raw: int,
                        rssi: Optional[float], rid_source: str) -> None:
        """Decoded a Basic ID (msg type 0x0) — creates the entry if new."""
        now = time.monotonic()
        id_type = _ID_TYPE_STRINGS.get(id_type_raw, "unknown")
        ua_type = _UA_TYPE_STRINGS.get(ua_type_raw)             # None if unknown
        with self._lock:
            self._messages_total += 1
            self._bump_source(rid_source, now)
            state = self._drones.get(uas_id)
            if state is None:
                state = DroneState(uas_id=uas_id, id_type=id_type, ua_type=ua_type)
                self._drones[uas_id] = state
            elif state.ua_type is None and ua_type is not None:
                # Identity is write-once — only fill in ua_type if previously unknown.
                state.ua_type = ua_type
            state.message_count += 1
            state.last_seen      = now
            state.rssi_dbm       = rssi
            state.rid_source     = rid_source
            self._mac_to_uas[mac] = uas_id

    def update_location(self, *, mac: str,
                        lat: float, lon: float,
                        alt_geo_m: Optional[float], height_agl_m: Optional[float],
                        gs_mps: Optional[float], heading_deg: Optional[float],
                        vspeed_mps: Optional[float],
                        rssi: Optional[float], rid_source: str) -> None:
        """Decoded a Location/Vector (0x1). Dropped if MAC isn't mapped yet."""
        now = time.monotonic()
        with self._lock:
            state = self._drone_for_mac(mac)
            if state is None:
                return     # heard position before identity for this MAC; drop
            self._messages_total += 1
            self._bump_source(rid_source, now)
            state.lat            = lat
            state.lon            = lon
            state.alt_geo_m      = alt_geo_m
            state.height_agl_m   = height_agl_m
            state.gs_mps         = gs_mps
            state.heading_deg    = heading_deg
            state.vspeed_mps     = vspeed_mps
            state.rssi_dbm       = rssi
            state.rid_source     = rid_source
            state.message_count += 1
            state.last_seen      = now
            state.last_pos_seen  = now

    def update_system(self, *, mac: str,
                      op_lat: Optional[float], op_lon: Optional[float],
                      alt_takeoff_m: Optional[float],
                      rssi: Optional[float], rid_source: str) -> None:
        """Decoded a System message (0x4) — operator location + takeoff alt.

        Fields are partial-updated: only overwrite when the decoder produced a
        usable value, so a System message whose operator coords got filtered
        by the parser (out-of-range / sentinel) does **not** wipe an earlier
        valid operator block.
        """
        now = time.monotonic()
        with self._lock:
            state = self._drone_for_mac(mac)
            if state is None:
                return
            self._messages_total       += 1
            self._bump_source(rid_source, now)
            if op_lat is not None:        state.op_lat           = op_lat
            if op_lon is not None:        state.op_lon           = op_lon
            if alt_takeoff_m is not None: state.op_alt_takeoff_m = alt_takeoff_m
            state.rssi_dbm              = rssi
            state.rid_source            = rid_source
            state.message_count        += 1
            state.last_seen             = now
            state.last_operator_seen    = now

    def update_operator_id(self, *, mac: str, operator_id: str,
                           rssi: Optional[float], rid_source: str) -> None:
        """Decoded an Operator ID message (0x5).

        Empty operator_id is ignored — many transmitters (and the spoofer)
        emit a blank string before the operator field is configured.
        """
        now = time.monotonic()
        with self._lock:
            state = self._drone_for_mac(mac)
            if state is None:
                return
            self._messages_total       += 1
            self._bump_source(rid_source, now)
            if operator_id:             state.operator_id = operator_id
            state.rssi_dbm              = rssi
            state.rid_source            = rid_source
            state.message_count        += 1
            state.last_seen             = now
            state.last_operator_seen    = now

    # -- helpers (lock must be held) ------------------------------------------

    def _drone_for_mac(self, mac: str) -> Optional[DroneState]:
        uas_id = self._mac_to_uas.get(mac)
        if uas_id is None:
            return None
        return self._drones.get(uas_id)

    def _bump_source(self, rid_source: str, now_mono: float) -> None:
        """Track per-transport message counters for the /status dashboard."""
        src = self._by_source.setdefault(rid_source, {"messages": 0, "last_seen": 0.0})
        src["messages"]  += 1
        src["last_seen"]  = now_mono

    # -- snapshot --------------------------------------------------------------

    def snapshot(self) -> dict:
        """Build the ``/data/remoteid.json`` document.

        Returns a plain dict ready for ``json.dumps``. SI → imperial conversion
        and the ``seen`` / ``seen_pos`` / ``operator.seen`` deltas are computed
        here from the current monotonic time so the HTTP handler does zero
        work per-request beyond serialisation.
        """
        now_mono = time.monotonic()
        now_wall = time.time()
        with self._lock:
            messages_total = self._messages_total
            drones = [self._row_for(s, now_mono) for s in self._drones.values()]
        return {
            "schema_version": SCHEMA_VERSION,
            "now":            round(now_wall, 1),
            "messages":       messages_total,
            "drones":         drones,
        }

    @staticmethod
    def _row_for(s: DroneState, now_mono: float) -> dict:
        row: dict = {
            "id":            s.uas_id,
            "id_type":       s.id_type,
            "message_count": s.message_count,
            "seen":          round(now_mono - s.last_seen, 1),
        }
        if s.ua_type is not None:      row["ua_type"]     = s.ua_type
        if s.lat is not None:          row["lat"]         = round(s.lat, 7)
        if s.lon is not None:          row["lon"]         = round(s.lon, 7)
        if s.alt_geo_m is not None:    row["alt_geom_ft"] = round(s.alt_geo_m    * M_TO_FT,     1)
        if s.height_agl_m is not None: row["agl_ft"]      = round(s.height_agl_m * M_TO_FT,     1)
        if s.gs_mps is not None:       row["gs"]          = round(s.gs_mps       * MPS_TO_KT,   1)
        if s.heading_deg is not None:  row["track"]       = round(s.heading_deg,                1)
        if s.vspeed_mps is not None:   row["geom_rate"]   = round(s.vspeed_mps   * MPS_TO_FTPM, 0)
        if s.rssi_dbm is not None:     row["rssi"]        = s.rssi_dbm
        if s.last_pos_seen is not None:
            row["seen_pos"] = round(now_mono - s.last_pos_seen, 1)
        if s.rid_source is not None:   row["rid_source"]  = s.rid_source

        operator: dict = {}
        if s.op_lat is not None:           operator["lat"]            = round(s.op_lat, 7)
        if s.op_lon is not None:           operator["lon"]            = round(s.op_lon, 7)
        if s.operator_id is not None:      operator["id"]             = s.operator_id
        if s.op_alt_takeoff_m is not None:
            operator["alt_takeoff_ft"] = round(s.op_alt_takeoff_m * M_TO_FT, 1)
        if s.last_operator_seen is not None:
            operator["seen"] = round(now_mono - s.last_operator_seen, 1)
        if operator:
            row["operator"] = operator
        return row

    # -- Health (for /status dashboard endpoint) -------------------------------

    def health(self) -> dict:
        """Operational snapshot for the status dashboard.

        Lock-cheap: copies a few scalars + the small ``by_source`` dict and
        releases. The HTTP handler must do nothing else under request.
        """
        now_mono = time.monotonic()
        with self._lock:
            sources = {
                src: {
                    "messages":   info["messages"],
                    "last_seen_s": round(now_mono - info["last_seen"], 1),
                }
                for src, info in self._by_source.items()
            }
            messages_total = self._messages_total
            drones_active  = len(self._drones)
            uptime_s       = round(now_mono - self._boot_mono, 1)
            last_any       = max(
                (info["last_seen"] for info in self._by_source.values()),
                default=None,
            )
        return {
            "schema_version": SCHEMA_VERSION,
            "uptime_s":       uptime_s,
            "messages_total": messages_total,
            "drones_active":  drones_active,
            "last_seen_s":    round(now_mono - last_any, 1) if last_any is not None else None,
            "by_source":      sources,
            "cpu_temp_c":     _read_cpu_temp(),
        }

    # -- TTL sweep -------------------------------------------------------------

    def sweep_loop(self, interval: float = 1.0) -> None:
        """Daemon thread body: drop entries older than ``ttl_seconds``.

        Returns when ``stop()`` is called.
        """
        while not self._stop.wait(interval):
            self._sweep_once()

    def _sweep_once(self) -> None:
        cutoff = time.monotonic() - self._ttl
        with self._lock:
            stale = [uas for uas, s in self._drones.items() if s.last_seen < cutoff]
            for uas in stale:
                del self._drones[uas]
            # Drop MAC mappings that now point at nothing.
            dead_macs = [m for m, uas in self._mac_to_uas.items() if uas not in self._drones]
            for m in dead_macs:
                del self._mac_to_uas[m]

    def stop(self) -> None:
        self._stop.set()


# -- Standalone smoke test -----------------------------------------------------

if __name__ == "__main__":
    import json

    t = Tracker(ttl_seconds=60.0)

    # A drone heard first via BLE: Basic ID → Location → System → Operator-ID.
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
    t.update_system(
        mac="aa:bb:cc:dd:ee:01",
        op_lat=40.6900, op_lon=-74.0100,
        alt_takeoff_m=15.0,
        rssi=-61.0, rid_source="ble",
    )
    t.update_operator_id(
        mac="aa:bb:cc:dd:ee:01", operator_id="FA3OPERATOR",
        rssi=-61.0, rid_source="ble",
    )

    # An orphan Location on a new MAC arrives before its Basic ID — must drop.
    t.update_location(
        mac="99:88:77:66:55:44",
        lat=40.7130, lon=-74.0061,
        alt_geo_m=126.0, height_agl_m=116.0,
        gs_mps=8.3, heading_deg=271.5, vspeed_mps=-3.0,
        rssi=-45.0, rid_source="wifi_beacon",
    )

    # Same drone now heard via WiFi (different MAC, MAC rotated or other radio).
    # Basic ID first, then Location; the Location should win for the position.
    t.update_basic_id(
        mac="99:88:77:66:55:44", uas_id="158190SK3X2YB7",
        id_type_raw=1, ua_type_raw=2,
        rssi=-45.0, rid_source="wifi_beacon",
    )
    t.update_location(
        mac="99:88:77:66:55:44",
        lat=40.7130, lon=-74.0061,
        alt_geo_m=126.0, height_agl_m=116.0,
        gs_mps=8.3, heading_deg=271.5, vspeed_mps=-3.0,
        rssi=-45.0, rid_source="wifi_beacon",
    )

    print(json.dumps(t.snapshot(), indent=2))
