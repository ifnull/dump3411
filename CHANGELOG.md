# Changelog

All notable changes to dump3411 are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]


## [1.0.0] — 2026-06-21

First tagged release.

### Detection

- ASTM F3411 Remote ID decoder across all three broadcast transports: Bluetooth LE, Wi-Fi Beacon (vendor-specific IE inside 802.11 management frames), and Wi-Fi NAN public action frames.
- OpenDroneID message types decoded: Basic ID (0x0), Location/Vector (0x1), Self-ID (0x3), System (0x4), Operator-ID (0x5), and Message Pack (0xF).
- Free-text Self-ID "purpose of flight" string surfaces in the journal, JSON feed (`self_id` + `self_id_seen`), MQTT per-drone state, and dashboard as a **Description** column.
- Defensive NAN logging: frames matching `_is_nan_action` but failing ODID-SDA extraction emit a warning with a hex prefix, so off-spec transmitters surface diagnostic data immediately rather than being silently dropped.

### Feed and dashboard

- HTTP server (`--serve HOST:PORT`) providing:
  - `GET /data/remoteid.json` — current tracker snapshot. Wire format locked by FEED.md (imperial units).
  - `GET /status` — operational health (uptime, last beacon, CPU temp, per-source counters, `history_enabled` + `history` stats when enabled).
  - `GET /` — self-contained status dashboard. Service-health pill, top-tile counters, per-transport message rates, live drone table with Google Maps links for both drone and operator coordinates, and a per-browser ft·kt·°F ↔ m·m/s·°C unit toggle (persists in `localStorage`).
- Dashboard **Recent detections** section — drones from the history DB over a configurable lookback (default 7 days, `--history-recent-days` / `HISTORY_RECENT_DAYS`), polled on a 30 s cadence. Each UAS-ID hyperlinks to `/map`; a `● live` badge marks anything also currently in the live tracker. Renders whenever history is enabled, including on IDLE ticks with no live drones.

### Persistence

- Opt-in SQLite detection history (`--history-db PATH` / `HISTORY_DB=`). Disabled by default for SD-card safety. Per-drone debounced writes (default 1 s), age + size rotation (defaults 30 d / 100 MB).
- `GET /history.json?uas_id=…&since=…&until=…` returns the full track and operator location for one drone.
- `GET /history/recent.json?since=…&limit=…` lists recently-seen drones (defaults: configured `HISTORY_RECENT_DAYS` window — 7 days out of the box — and 50 most recent). Response carries `window_seconds` + `window_label` so clients can render the lookback without hard-coding it.
- `GET /map?uas_id=…` — self-contained Leaflet page with the operator marker (blue) and the drone polyline (red, click any point for per-message detail). The one page that requires internet to render (OSM tiles + Leaflet CDN); the rest of dump3411 stays fully offline.

### Publishing

- Optional MQTT publisher (`--mqtt-broker` / `MQTT_BROKER` and friends). Compatible with `paho-mqtt` 1.x and 2.x.
  - `<prefix>/drones/<uas_id>` — retained per-drone state, latest-wins, 1 Hz debounced. Empty payload on TTL eviction so subscribers see the removal.
  - `<prefix>/events/detection` — one publish per decoded message: `{uas_id, rid_source, rssi, t}`.
  - `<prefix>/status` — retained `GET /status` JSON, refreshed every ~5 s.
  - `<prefix>/online` — `"online"` on connect; LWT publishes `"offline"` on disconnect.

### Packaging and deployment

- `pyproject.toml` declaring deps (`bleak` required, `paho-mqtt` as the `mqtt` extra), Python ≥ 3.10, and a `dump3411` console script.
- `install.sh` — idempotent root bootstrap. Installs apt deps, auto-detects the USB monitor-mode Wi-Fi adapter (prompts on multiple, manual fallback), sed-rewrites the systemd unit's `ExecStart` path and `--wifi-iface` argument, then `daemon-reload` + `enable --now`.
- `dump3411.service` with `EnvironmentFile=-/etc/dump3411.env` so MQTT and history config live outside the unit file.
- `journald-dump3411.conf` drop-in for persistent journal entries capped at 50 MB.

### Documentation

- README — Quickstart leads with `sudo ./install.sh`; manual install path kept underneath. Sections for hardware, dashboard, JSON feed, MQTT publisher, persistent history, logs, and standalone single-radio mode.
- FEED.md — JSON wire contract. Imperial units. [`ha-airspace`](https://github.com/ifnull/ha-airspace) named as the canonical Home Assistant consumer.
- TESTING.md — how to put a Remote ID transmitter on the air to validate the receive path.
- TODO.md — Done / Parked ideas / Considered and declined sections. Records reasoning for declining DJI DroneID via SDR and FAA RID lookup so those decisions don't get re-litigated.

### Changed

- Project renamed from `drone-aware-zero` to `dump3411` mid-development. The old name implied the Raspberry Pi Zero W reference platform; the new name follows the `dump1090` / `dump978` naming convention for a spec-decoding receiver (ASTM F3411).

### Fixed

- Wi-Fi Beacon: strip the OpenDroneID send-counter byte before handing the payload to the decoder.
- Decoder: Location/Vector and System message parsers aligned to ASTM F3411 (scaling factors, signed-field handling, east/west direction byte).


[Unreleased]: https://github.com/ifnull/dump3411/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ifnull/dump3411/releases/tag/v1.0.0
