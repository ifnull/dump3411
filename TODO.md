# TODO

Things that aren't blocking the project shipping, but are worth doing next. Nothing here is committed — adopt or drop items as priorities shift.

## Done

- **MQTT publisher** — implemented in `mqtt_publisher.py`. Publishes per-drone retained state (debounced, latest-wins), a `/status` snapshot, detection events, and an `online`/`offline` LWT. Configured via `--mqtt-broker` / `MQTT_BROKER` env. See README for topic layout.

## Parked ideas

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
