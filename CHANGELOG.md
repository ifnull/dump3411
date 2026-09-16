# Changelog

All notable changes to dump3411 are recorded here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).


## [Unreleased]

### Added

- `--channel CH` (on both `dump3411.py` and standalone `wifi_feeder.py`) pins the Wi-Fi radio to a single fixed 2.4 GHz channel (1-11) instead of hopping — full dwell on a transmitter whose channel you already know, at the cost of blind spots on the other ten. `--channel-dwell` is ignored in this mode.

- `ble5` as a new `rid_source` value for Bluetooth 5 long-range / extended advertising (a Message Pack per advertisement) — Bluetooth 4 legacy advertisements keep the existing `ble`, so consumers are unaffected. BLE journal lines log as `[BLE]` / `[BLE5]`; `/status` gains a `ble5` per-source counter alongside `ble`; the dashboard Transport column, history DB and MQTT `events/detection` payload carry it through the existing `rid_source` field — no new field, no `schema_version` bump. Classification is from the wire format — Bluetooth 5 RID advertisements carry a Message Pack, which cannot fit a legacy ADV PDU's 31-byte cap — because BlueZ does not expose the PHY of received advertisements. Whether BT5 is received at all depends on adapter + BlueZ support for coded-PHY scanning; the `ble5` counter staying at zero while `ble` climbs means it isn't.

- Per-drone transport history in the feed and dashboard. Drone rows gain an additive `rid_sources` array listing **every** transport the drone has been heard on since it entered the tracker, most-recent-first (e.g. `["wifi_beacon", "ble5", "ble"]`) — previously only the most recent transport was visible. The dashboard Source column renders the full list with the current transport at full intensity and previously-heard transports dimmed. No `schema_version` bump; `rid_source` keeps its most-recent-wins semantics.

### Fixed

- BLE detection no longer stops for the life of the process after a transient BlueZ error. The BLE scanner runs as the whole BLE thread, so an exception escaping it ended Bluetooth reception silently while Wi-Fi kept serving — the dashboard showed empty airspace rather than a dead radio, and systemd's `Restart=on-failure` never fired because the process itself stayed healthy. Observed in the field as `[org.bluez.Error.NotReady] Resource Not Ready` a couple of seconds into startup: `wifi_feeder.set_monitor_mode()` runs `rfkill unblock all`, which also touches the Bluetooth device, and BlueZ reports the controller not ready while it powers back up. The scanner now reconnects with capped exponential backoff (1 s doubling to 60 s), which also covers adapter resets and bluetoothd restarts.
- Bluetooth 5 extended-advertising Remote ID (a Message Pack per advertisement, e.g. from ArduRemoteID / Dronetag transmitters) was previously dropped at the service-data length check and logged as "Unrecognised service data". `extract_rid_payload` now accepts the pack format and decodes all sub-messages.
- System message operator location now carries `operator_location_type` (`takeoff` \| `live_gnss` \| `fixed`, decoded from byte 1 bits 0-1). Some transmitters (reported: Potensic RID-916, over BLE) toggle this across messages, which made the `operator` block's coordinates look like they randomly jumped between the drone's own position and the operator's — they were actually alternating between the drone's takeoff point and a live operator fix, both reported under the same field. Surfaced as `operator.location_type` in the JSON feed (additive, no `schema_version` bump) and as a `(takeoff)` annotation next to the Operator column on the live dashboard table. Not yet persisted to the history DB / `/map` view — see TODO.md.


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
