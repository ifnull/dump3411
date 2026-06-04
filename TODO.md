# TODO

Things that aren't blocking the project shipping, but are worth doing next. Nothing here is committed — adopt or drop items as priorities shift.

## MQTT publisher

Publish detections to an MQTT broker so a consumer (Home Assistant, Node-RED, Telegraf, anything that speaks MQTT) can react to drones in range without polling the JSON feed.

**Shape (sketch)**:

- New optional dependency: `paho-mqtt` (apt-installable as `python3-paho-mqtt`; should also fall back gracefully if missing so the rest of the project still runs).
- New CLI flags on `dump3411.py`:
  - `--mqtt-broker HOST:PORT` (e.g. `mqtt.lan:1883`); omit to disable
  - `--mqtt-topic-prefix PREFIX` (default `dump3411`)
  - `--mqtt-user USER` / `--mqtt-password PASS` (optional)
  - `--mqtt-tls` (optional)
- New module `mqtt_publisher.py` that subscribes to the tracker's update events. Cleanest design: have the `Tracker` expose an event-bus hook (`on_drone_seen`, `on_drone_expired`) so the MQTT publisher is a pure consumer and adding more sinks later is trivial.

**Suggested topic layout** (Home-Assistant-friendly):

```
dump3411/status                              retained, ~1 Hz, mirrors /status
dump3411/drones/<uas_id>/state               retained, latest per-drone snapshot
dump3411/drones/<uas_id>/seen                retained, last_seen epoch
dump3411/events/detection                    one publish per decoded message
dump3411/events/expired                      one publish when a drone TTL-evicts
```

`retained` for the per-drone state means a fresh HA restart immediately sees the current airspace.

**Open questions to settle before coding**:

- One snapshot publish per N seconds vs publish-on-change? (Probably per-change with a debounce — drones broadcast multiple times per second.)
- QoS 0 (fire-and-forget) is right for events; QoS 1 (at-least-once) might be right for the retained state.
- Should we mirror the FEED.md schema (imperial units) or use SI on MQTT? FEED.md is locked imperial for `ha-airspace`; MQTT consumers tend to expect SI. Worth a separate decision.

## Other parked ideas

### Persistent detection history
Append every decoded detection to a rolling SQLite log (size-capped). Lets the dashboard show "what flew over today/yesterday/this week" and survives reboots in a structured form (vs the journal's text-only history).

### Wire [`ha-airspace`](https://github.com/ifnull/ha-airspace) end-to-end
The FEED.md contract is in place; actually plumb the Pi → `ha-airspace` → Home Assistant path with live data and confirm the round-trip.

### Validate against a spec-compliant transmitter
Flash an ESP32-S3 with `ArduPilot/ArduRemoteID`. Closes the trust gap the spoofer's encoding quirks (`gs` 3×, `track` mod 180°) leave open. ~$10–15 of hardware.

### Wi-Fi NAN decoding
Today `wifi_feeder.py` counts NAN frames but doesn't decode the ODID payload from the action-frame body. The `wifi_nan` source is already wired through the tracker and dashboard for the day this lands. Coverage win is small — most drones use BLE + Beacon, not NAN.

### Extract shared decoder
`ble_feeder.py` and `wifi_feeder.py` each carry their own copy of `parse_basic_id` / `parse_location` / `parse_system_msg` / `parse_operator_id` / `decode_rid_message`. Refactor to a shared `odid_decoder.py` so the next spec fix is a one-file change.
