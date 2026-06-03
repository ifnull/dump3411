# HTTP feed: `remoteid.json`

`dump3411` can serve its current detections as a JSON document over HTTP so a consumer on the LAN (e.g. the `adsb-enrich` ADS-B → Home Assistant service) can poll them. This is **opt-in** and additive — it does not change the default detect-and-print-to-journald behavior.

> **This is not `aircraft.json`.** Drones are not manned aircraft and this feed is not meant for dump1090-family tools. It borrows dump1090's *envelope idioms* (`now`, `seen`, a polled array) only because the intended consumer already understands them. The object schema is purpose-built for ASTM F3411 Remote ID — including operator location, UAS ID type, and native AGL, which dump1090 has no fields for.

**The canonical contract lives in the consumer repo** (`adsb-enrich/FEED.md`). If this file and that one ever disagree, that one wins. This file restates the contract so the detector repo is self-contained, and adds the producer-side obligations.

---

## Running the feed

```bash
sudo python3 dump3411.py --wifi-iface wlan1 --serve 0.0.0.0:8754
```

Both radios run as threads inside **one** process and share a **single** in-memory detection cache; one HTTP server serves the JSON document from that cache. The per-radio scripts (`ble_feeder.py`, `wifi_feeder.py`) remain as standalone testing tools and do not serve the feed.

Then from the consumer:

```
GET http://<dump3411-host>:8754/data/remoteid.json
```

Use stdlib `http.server` — no async, no extra deps. dump3411 is designed to run on modest hardware (a Pi Zero W is the reference target), so **the HTTP handler must only serialize a pre-built in-memory snapshot — never decode or do work per request.**

---

## Document shape

```jsonc
{
  "schema_version": 1,          // bump only on breaking changes
  "now": 1717200000.0,          // epoch seconds at snapshot time (producer clock)
  "messages": 4213,             // total RID messages decoded since start (monotonic)
  "drones": [ /* see below; may be empty */ ]
}
```

### Detection object

| Field | Type | Unit | Req | Source (decoder) |
|---|---|---|:--:|---|
| `id` | string | — | ✓ | Basic ID `uas_id` — the identity key |
| `id_type` | string | — | ✓ | `serial` \| `caa_reg` \| `utm_uuid` \| `session` \| `unknown` (from `id_type`) |
| `ua_type` | string | — |  | Basic ID `ua_type` |
| `lat` / `lon` | number | deg |  | Location msg `latitude`/`longitude` |
| `alt_geom_ft` | number | ft |  | Location `altitude_geo` (m → ft) |
| `agl_ft` | number | ft |  | Location `height_agl` (m → ft) |
| `gs` | number | kt |  | Location `ground_speed` (m/s → kt) |
| `track` | number | deg |  | Location `heading` |
| `geom_rate` | number | ft/min |  | Location `vertical_speed` (m/s → ft/min) |
| `rssi` | number | dBm |  | transport RSSI of last message |
| `message_count` | number | — |  | running count of decoded RID messages for this `id` since first seen (per-drone; distinct from the envelope `messages`) |
| `seen` | number | s | ✓ | `now - last_seen[id]` |
| `seen_pos` | number | s |  | `now - last_pos_seen[id]` |
| `rid_source` | string | — |  | `ble` \| `wifi_beacon` \| `wifi_nan` |
| `operator` | object | — |  | from System / Operator-ID msgs (see below) |

### `operator` sub-object (present once seen)

| Field | Type | Unit | Source |
|---|---|---|---|
| `lat` / `lon` | number | deg | System msg `operator_lat`/`operator_lon` |
| `id` | string | — | Operator-ID msg `operator_id` |
| `alt_takeoff_ft` | number | ft | System `alt_takeoff_geo` (m → ft) |
| `seen` | number | s | `now - last_operator_seen[id]` — staleness of this whole block |

A detection with no `lat`/`lon` is valid (Basic ID heard before GPS lock). Keep it — the identity is known; position is just unavailable.

---

## Producer obligations (the part that's on this repo)

1. **Single in-memory cache, keyed by `uas_id`.** One process, one `dict`, one `threading.Lock`. Each entry holds the latest decoded fields plus three monotonic timestamps: `last_seen`, `last_pos_seen`, `last_operator_seen`. Both radios run as threads inside this process and write into the same cache.
2. **Convert to consumer units at write time.** RID broadcasts SI (m, m/s); the feed emits **ft, kt, ft/min**. The producer owns the conversion so the consumer has a single unit path. The decoders in `ble_feeder.py` / `wifi_feeder.py` currently emit SI — do the conversion in the cache layer, not in the decoder, so the journald output stays untouched.
3. **Multi-transport precedence: most-recent message wins.** If the same `uas_id` is heard on more than one transport, the latest message updates `rid_source`, `rssi`, and any time-varying field (position, velocity, heading, altitude, operator block). Identity fields (`id_type`, `ua_type`) are write-once from the first Basic ID and not overwritten. `message_count` increments on every decoded message regardless of transport.
4. **`seen`, `seen_pos`, `operator.seen` computed at serialize time** from the cached monotonic timestamps relative to `now`.
5. **Drop stale ids** from the cache after a producer-side timeout (default: 60 s with no messages on any transport) so the feed reflects current airspace.
6. **Snapshot-only HTTP handler.** Under the cache lock, copy the live state into a plain dict; release the lock; then serialize and return. Never decode, convert, or compute under request.
7. **Additive.** Adding the feed must not change the existing detector behavior — journald detection logging stays exactly as-is.

---

## Versioning

`schema_version` is one integer. Adding optional fields is backward-compatible and does **not** bump it (consumers ignore unknown fields). Removing/repurposing a field or changing units/semantics bumps the major and is coordinated with `adsb-enrich`. Keep this table and the canonical one in sync.

| Version | Change |
|---|---|
| 1 | Initial contract (draft). |
