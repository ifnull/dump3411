#!/usr/bin/env python3
"""
dump3411 / history.py

Optional persistent detection log backed by SQLite. Hooks the same Tracker
``on_change`` callback MQTT uses; each fire stores a row of the imperial
snapshot dict (lat / lon / alt / gs / track / operator location / self_id /
…) so a flight path can be reconstructed after the in-memory tracker has
TTL-evicted the drone.

**Opt-in.** Disabled unless ``--history-db`` (or ``HISTORY_DB``) is
configured. Pi SD cards aren't infinite, so we don't write by default.

Design:

  - WAL-mode SQLite. One write connection guarded by a Lock; HTTP readers
    open per-request connections (WAL lets readers not block writes).
  - Per-drone debounce (default 1 s) so a 5 Hz broadcaster is one row/sec.
    Latest-wins: the *next* write after the window closes uses whatever
    row state the tracker currently has, which mirrors the snapshot the
    JSON feed and MQTT consumers also see.
  - Cleanup thread (default every 60 s) prunes rows older than the
    retention window and then trims to the size cap. Whichever cuts first.
  - No expire hook. When the tracker TTL-evicts a drone, history rows
    persist — that's the entire point.

Schema (v1):

    detections(
      id            INTEGER PRIMARY KEY AUTOINCREMENT,
      ts            REAL    NOT NULL,    -- epoch seconds, wall clock
      uas_id        TEXT    NOT NULL,
      id_type       TEXT,
      ua_type       TEXT,
      lat           REAL,
      lon           REAL,
      alt_geom_ft   REAL,
      agl_ft        REAL,
      gs            REAL,    -- knots
      track         REAL,    -- degrees
      geom_rate     REAL,    -- ft/min
      rssi          REAL,
      rid_source    TEXT,
      self_id       TEXT,
      op_lat        REAL,
      op_lon        REAL,
      operator_id   TEXT
    )

    idx_uas_ts ON (uas_id, ts)
    idx_ts     ON (ts)

PRAGMA user_version = 1.
"""

import logging
import os
import sqlite3
import threading
import time
from typing import Optional

log = logging.getLogger("dump3411.history")

SCHEMA_VERSION = 1

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS detections (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  ts           REAL    NOT NULL,
  uas_id       TEXT    NOT NULL,
  id_type      TEXT,
  ua_type      TEXT,
  lat          REAL,
  lon          REAL,
  alt_geom_ft  REAL,
  agl_ft       REAL,
  gs           REAL,
  track        REAL,
  geom_rate    REAL,
  rssi         REAL,
  rid_source   TEXT,
  self_id      TEXT,
  op_lat       REAL,
  op_lon       REAL,
  operator_id  TEXT
);
CREATE INDEX IF NOT EXISTS idx_uas_ts ON detections(uas_id, ts);
CREATE INDEX IF NOT EXISTS idx_ts     ON detections(ts);
"""

_INSERT_SQL = """
INSERT INTO detections
  (ts, uas_id, id_type, ua_type, lat, lon, alt_geom_ft, agl_ft, gs, track,
   geom_rate, rssi, rid_source, self_id, op_lat, op_lon, operator_id)
VALUES
  (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


class HistoryWriter:
    """SQLite-backed persistent detection log.

    Construct with the destination path; call :meth:`start` to launch the
    cleanup thread; pass :meth:`on_drone_change` into ``Tracker.set_callbacks``
    or chain it alongside other consumers in the orchestrator.
    """

    CLEANUP_INTERVAL_S = 60.0

    def __init__(self, db_path: str,
                 max_mb: float = 100.0,
                 retention_days: float = 30.0,
                 debounce_s: float = 1.0):
        self._path = db_path
        self._max_bytes = int(max_mb * 1024 * 1024)
        self._retention_s = float(retention_days) * 86400.0
        self._debounce_s = float(debounce_s)

        # One write connection guarded by a lock; readers use per-request conns.
        os.makedirs(os.path.dirname(os.path.abspath(db_path)) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False,
                                     isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe, faster on SD
        self._conn.executescript(_SCHEMA_SQL)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

        self._write_lock = threading.Lock()
        self._last_write: dict[str, float] = {}   # uas_id -> monotonic
        self._stop = threading.Event()
        self._cleanup_thread: Optional[threading.Thread] = None

        log.info("history: %s (max=%.0f MB, retention=%.1f d, debounce=%.1f s)",
                 db_path, max_mb, retention_days, debounce_s)

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        self._cleanup_thread = threading.Thread(
            target=self._cleanup_loop, daemon=True, name="history-cleanup",
        )
        self._cleanup_thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            with self._write_lock:
                self._conn.close()
        except Exception:
            pass

    # -- Tracker callback ----------------------------------------------------

    def on_drone_change(self, uas_id: str, row: dict) -> None:
        """Called from radio threads via Tracker.on_change. Must be fast.

        Latest-wins per drone with a configurable debounce so SD-card writes
        stay bounded (default 1 s = max one row per drone per second).
        """
        now_mono = time.monotonic()
        last = self._last_write.get(uas_id, 0.0)
        if (now_mono - last) < self._debounce_s:
            return
        self._last_write[uas_id] = now_mono

        op = row.get("operator") or {}
        params = (
            time.time(),
            uas_id,
            row.get("id_type"),
            row.get("ua_type"),
            row.get("lat"),
            row.get("lon"),
            row.get("alt_geom_ft"),
            row.get("agl_ft"),
            row.get("gs"),
            row.get("track"),
            row.get("geom_rate"),
            row.get("rssi"),
            row.get("rid_source"),
            row.get("self_id"),
            op.get("lat"),
            op.get("lon"),
            op.get("id"),
        )
        try:
            with self._write_lock:
                self._conn.execute(_INSERT_SQL, params)
        except Exception:
            log.exception("history insert failed for %s", uas_id)

    # -- Cleanup -------------------------------------------------------------

    def _cleanup_loop(self) -> None:
        while not self._stop.wait(self.CLEANUP_INTERVAL_S):
            try:
                self._prune_age()
                self._prune_size()
            except Exception:
                log.exception("history cleanup failed")

    def _prune_age(self) -> None:
        cutoff = time.time() - self._retention_s
        with self._write_lock:
            cur = self._conn.execute(
                "DELETE FROM detections WHERE ts < ?", (cutoff,))
            removed = cur.rowcount
        if removed:
            log.info("history: pruned %d rows older than %.1f d",
                     removed, self._retention_s / 86400.0)

    def _prune_size(self) -> None:
        try:
            size = os.path.getsize(self._path)
        except OSError:
            return
        if size <= self._max_bytes:
            return
        # Over the size cap. Delete the oldest 10% of rows; VACUUM is heavy on
        # SD card, so let SQLite's freelist reuse pages and we'll check again
        # on the next tick.
        with self._write_lock:
            (total,) = self._conn.execute(
                "SELECT COUNT(*) FROM detections").fetchone()
            to_remove = max(1, total // 10)
            self._conn.execute(
                "DELETE FROM detections WHERE id IN "
                "(SELECT id FROM detections ORDER BY ts ASC LIMIT ?)",
                (to_remove,))
        log.info("history: over size cap (%.1f MB > %.1f MB); pruned %d oldest rows",
                 size / 1048576, self._max_bytes / 1048576, to_remove)

    # -- Read API (called from HTTP handler threads) ------------------------

    def _read_conn(self) -> sqlite3.Connection:
        """Open a fresh read-only connection. WAL lets these run without
        blocking the writer."""
        c = sqlite3.connect(f"file:{self._path}?mode=ro", uri=True,
                            check_same_thread=False)
        c.row_factory = sqlite3.Row
        return c

    def query_track(self, uas_id: str,
                    since: Optional[float] = None,
                    until: Optional[float] = None,
                    limit: int = 10000) -> list[dict]:
        """Return ordered position points for one drone within an optional
        time window. Filters out rows with no lat/lon so the result is a
        clean track suitable for plotting."""
        q = ("SELECT ts, lat, lon, alt_geom_ft, agl_ft, gs, track, geom_rate, "
             "rssi, rid_source FROM detections "
             "WHERE uas_id = ? AND lat IS NOT NULL AND lon IS NOT NULL")
        params: list = [uas_id]
        if since is not None:
            q += " AND ts >= ?"; params.append(since)
        if until is not None:
            q += " AND ts <= ?"; params.append(until)
        q += " ORDER BY ts ASC LIMIT ?"
        params.append(limit)
        c = self._read_conn()
        try:
            rows = c.execute(q, params).fetchall()
            return [dict(r) for r in rows]
        finally:
            c.close()

    def query_operator(self, uas_id: str) -> Optional[dict]:
        """Return the most recent non-null operator position seen for this
        drone, or None if we never saw one."""
        c = self._read_conn()
        try:
            row = c.execute(
                "SELECT ts, op_lat, op_lon, operator_id FROM detections "
                "WHERE uas_id = ? AND op_lat IS NOT NULL AND op_lon IS NOT NULL "
                "ORDER BY ts DESC LIMIT 1",
                (uas_id,),
            ).fetchone()
            if row is None:
                return None
            return {"ts": row["ts"], "lat": row["op_lat"], "lon": row["op_lon"],
                    "id": row["operator_id"]}
        finally:
            c.close()

    def query_drone_meta(self, uas_id: str) -> Optional[dict]:
        """Return latest id_type / ua_type / self_id seen for this drone."""
        c = self._read_conn()
        try:
            row = c.execute(
                "SELECT id_type, ua_type, self_id FROM detections "
                "WHERE uas_id = ? ORDER BY ts DESC LIMIT 1",
                (uas_id,),
            ).fetchone()
            return dict(row) if row else None
        finally:
            c.close()

    def stats(self) -> dict:
        """For /status: rows, distinct drones, file size."""
        try:
            size_bytes = os.path.getsize(self._path)
        except OSError:
            size_bytes = 0
        c = self._read_conn()
        try:
            (rows,) = c.execute("SELECT COUNT(*) FROM detections").fetchone()
            (drones,) = c.execute(
                "SELECT COUNT(DISTINCT uas_id) FROM detections").fetchone()
            row = c.execute(
                "SELECT MIN(ts), MAX(ts) FROM detections").fetchone()
            earliest, latest = row[0], row[1]
        finally:
            c.close()
        return {
            "rows":           rows,
            "drones":         drones,
            "size_bytes":     size_bytes,
            "earliest_ts":    earliest,
            "latest_ts":      latest,
        }
