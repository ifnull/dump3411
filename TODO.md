# TODO

Things that aren't blocking the project shipping, but are worth doing next. Nothing here is committed — adopt or drop items as priorities shift.

## Done

- **MQTT publisher** — implemented in `mqtt_publisher.py`. Publishes per-drone retained state (debounced, latest-wins), a `/status` snapshot, detection events, and an `online`/`offline` LWT. Configured via `--mqtt-broker` / `MQTT_BROKER` env. See README for topic layout.
- **Wi-Fi NAN decoding** — `wifi_feeder.py` now walks the NAN attributes inside a Public Action frame, finds the Service Descriptor Attribute matching the ODID Service ID, strips the send-counter byte out of the Service Info, and hands the ODID payload to the existing decoder. Sub-messages are routed into the tracker with `rid_source="wifi_nan"`; the dashboard's "By transport" table includes `wifi_nan` again.

## Parked ideas

### Persistent detection history
Append every decoded detection to a rolling SQLite log (size-capped). Lets the dashboard show "what flew over today/yesterday/this week" and survives reboots in a structured form (vs the journal's text-only history).

### Wire [`ha-airspace`](https://github.com/ifnull/ha-airspace) end-to-end
The FEED.md contract is in place; actually plumb the Pi → `ha-airspace` → Home Assistant path with live data and confirm the round-trip.

### Validate against a spec-compliant transmitter
Flash an ESP32-S3 with `ArduPilot/ArduRemoteID`. Closes the trust gap the spoofer's encoding quirks (`gs` 3×, `track` mod 180°) leave open. ~$10–15 of hardware.

### Extract shared decoder
`ble_feeder.py` and `wifi_feeder.py` each carry their own copy of `parse_basic_id` / `parse_location` / `parse_system_msg` / `parse_operator_id` / `decode_rid_message`. Refactor to a shared `odid_decoder.py` so the next spec fix is a one-file change.

## Considered and declined

Items we evaluated and decided against, with the reasoning recorded so we don't re-litigate the same call later. Each entry should also note what would change the decision.

### DJI DroneID via Software-Defined Radio  *(2026-06-05)*

Adding a sidecar receiver for DJI's proprietary OcuSync DroneID broadcast — the signal Aeroscope decodes. Reference implementation: [RUB-SysSec/DroneSecurity](https://github.com/RUB-SysSec/DroneSecurity); paper: Schiller et al., *Drone Security and the Mysterious Case of DJI's DroneID*, [NDSS '23](https://www.ndss-symposium.org/wp-content/uploads/2023/02/ndss2023_f217_paper.pdf).

**Why declined:**

1. **RTL-SDR can't do this.** DroneID is a 15.36 MHz OFDM signal at 2.4 GHz / 5.7 GHz. RTL-SDR's ~2.4 MHz instantaneous bandwidth and E4000-tuner 2.2 GHz ceiling disqualify it twice over. Would need a HackRF / PlutoSDR / LimeSDR / B205-mini — ~$250 minimum, ~$1500 for the paper's setup.
2. **Pi Zero W can't decode it.** OFDM demod + descrambling + turbo decode at 50 MHz sample rate needs NUC-class CPU, not a single-core ARMv6.
3. **Range is ~10 m** per the paper's own results — practically worse than the BLE/Wi-Fi RID coverage dump3411 already gets.
4. **DJI-only.** Doesn't help with Skydio, Autel, Parrot, Yuneec, ArduPilot-based airframes, etc.
5. **Shrinking target.** Post-Sept 2023 DJI drones broadcast ASTM F3411 RID natively (already decoded here). Pre-RID DJIs are the long tail and the FAA is gradually retrofitting them via broadcast modules.
6. **Cheaper alternative for the airframe we actually own.** A ~$200 Dronetag Beacon clipped to the Phantom 4 Pro satisfies FAA Part 89 and gives dump3411 a native F3411 signal at full BLE/Wi-Fi range — no SDR pipeline.
7. **Authors' own caveat.** Their README explicitly: *"not optimized … not meant for productive, reliable localization"* — it's an academic artifact.

**What would change this call:**

- A specific local DJI-detection use case appears (e.g. a recurring drone in the airspace whose owner isn't using a broadcast module).
- A lightweight SDR-based DroneID receiver lands that runs on Pi-class hardware (FPGA-accelerated frontend, etc.).
- A wideband SDR is already in the rack for unrelated reasons, making the marginal cost near-zero.

Until any of those is true, stay focused on the open-standard F3411 path — which is the regulatory direction the airspace is moving in regardless.

### FAA RID lookup (UAS serial → make / model)  *(2026-06-11)*

Enrich detected drones with manufacturer + model from the FAA Remote ID registry. Reference implementation: [jlrjr/faa-rid-lookup](https://github.com/jlrjr/faa-rid-lookup) — bundles a local SQLite cache built from the FAA's public API (~3,900 exact serials + ~250 ranges) with optional online fallback.

**Why declined here:**

1. **dump3411 is a thin producer.** It pulls RID off the wire, decodes per ASTM F3411, serves a snapshot per FEED.md. External-registry enrichment is a different concern with its own operational tail (DB freshness, FAA API rate limits, online-fallback policy).
2. **The dashboard is "is the receiver working?", not the rich drone view.** A bare UAS ID is sufficient for verifying detection. The make/model belongs where drone data is actually consumed.
3. **FEED.md stays clean.** Producing `id` + `id_type` keeps the wire contract on "what came over the air"; baking enrichment fields into the producer would force the schema (and every consumer) to evolve every time a new enrichment source shows up.
4. **Resolution: `ha-airspace` handles it.** That project already is the enrichment layer for ADS-B; RID lookup is exact symmetry — same DB-update cadence, same Home Assistant audience.

**What would change this call:**

- A lightweight static map of CTA-2063-A manufacturer codes (first 4 chars → vendor name from ICAO's MFR registry, ~50 entries) might land in dump3411 later if a "manufacturer hint" in the dashboard turns out to be useful. That's a ~30-line const, no DB, no network, no schema change — not a registry lookup, just a vendor-name annotation. Skipping it for now.
