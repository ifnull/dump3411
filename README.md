# dump3411

A drone Remote ID detector for the **Raspberry Pi Zero W**. Detects nearby
drones over BLE and WiFi, prints them to the journal, and optionally serves
a JSON feed for LAN consumers. Fully offline — no account, no token, no
data leaves the Pi.

Stripped-down fork of the DroneAware node feeders: the network uplink,
enrollment, heartbeat, GPS, and on-disk buffers have been removed.

## Hardware

- **Raspberry Pi Zero W 1.1** — built-in Bluetooth handles BLE detection.
- **USB WiFi adapter with monitor mode** — Alfa AWUS036NEH (RT3070) confirmed.
- **micro-USB OTG adapter** to attach the WiFi adapter, ideally through a
  **powered USB hub** (the adapter's current draw can brown out the Pi).

The Pi's built-in WiFi cannot do monitor mode — that is what the USB adapter
is for. BLE uses the built-in Bluetooth and needs no extra hardware.

## OS

**Raspberry Pi OS Bookworm, 32-bit Lite.** The code needs **Python 3.10+**
(Bookworm ships 3.11). The Pi Zero W 1 is ARMv6 and cannot run 64-bit.

## Install

```bash
sudo apt update
sudo apt install -y python3-bleak iw rfkill bluez
```

`bluez` is usually already present. The HTTP feed uses stdlib `http.server`,
no extra deps.

## Run

Find the USB adapter's interface name (built-in WiFi is `wlan0`; the USB
adapter is usually `wlan1`):

```bash
ip link
```

### Manually (one terminal, for testing)

```bash
sudo ./run-offline.sh wlan1
```

This launches `dump3411.py` with both radios and the JSON feed on port
8754. Ctrl-C stops it and returns the WiFi adapter to managed mode.

### As a service (starts on boot)

The service file assumes the repo lives at `/home/pi/dump3411` and
the USB adapter is `wlan1`. Edit `dump3411.service` if either differs.

```bash
sudo cp dump3411.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now dump3411
```

Other commands:

```bash
sudo systemctl status dump3411       # check state
sudo systemctl restart dump3411      # restart
sudo systemctl disable --now dump3411 # stop and remove from boot
```

## Status dashboard

Browser to **`http://drone-detector.local:8754/`** for a live status page —
service health pill, uptime, per-transport message counters, CPU temp, and a
table of currently tracked drones. Stdlib HTML embedded in `feed_server.py`;
polls `/status` and `/data/remoteid.json` every 1.5 s. No build step, no
external assets. Useful for "is the detector alive?" without ssh + journalctl.

## Local JSON feed

When `--serve HOST:PORT` is passed (the default `run-offline.sh` and the
service both pass `--serve 0.0.0.0:8754`), `dump3411.py` exposes:

```
GET http://<pi>:8754/data/remoteid.json
```

A snapshot of currently-tracked drones, ~1 Hz polling cadence, intended for
consumers like `adsb-enrich` to fold into Home Assistant. See **FEED.md** for
the wire contract.

Quick check from another LAN device:

```bash
curl -s http://drone-detector.local:8754/data/remoteid.json | jq .
```

The feed is additive — it does not change the journal logging behavior. To
run detection-only (no feed) drop `--serve` from the command line.

## Logs

The detector writes no log file of its own. Under systemd its output goes
to the journal. By default that journal is **volatile (RAM only)** and is
wiped on reboot — so detections wouldn't survive a power cycle.

To keep detection history across reboots, install the journald drop-in. It
makes the journal persistent and caps it at 50 MB so it can never fill the
SD card:

```bash
sudo mkdir -p /etc/systemd/journald.conf.d
sudo cp journald-dump3411.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald
```

Then view detections:

```bash
journalctl -u dump3411 -f                              # live tail
journalctl -u dump3411 --since "today"                 # by date
journalctl -u dump3411 --since "2026-05-21 09:00"      # from a time
journalctl --disk-usage                                  # how much it's using
```

## What you'll see

Detections print one line each:

```
[BLE] MAC=...  RSSI=-62dBm  Type=Basic ID  UAS-ID=1581F...
[WiFi-Beacon] MAC=...  RSSI=-71dBm  Type=Location/Vector  lat=40.71 lon=-74.00
```

A `[Heartbeat]` line every 60 seconds confirms each detector is alive and
shows the running detection count. Add `--verbose` (or edit the service
file) to log every decoded message type, not just Basic ID and Location.

## Standalone single-radio testing

The per-radio scripts still run on their own — handy when debugging one
radio in isolation:

```bash
sudo python3 ble_feeder.py  --adapter hci0
sudo python3 wifi_feeder.py --iface wlan1
```

These do not serve the feed (only `dump3411.py` does).

## Testing without a real drone

Stand up an OpenDroneID transmitter on a separate machine so the detector
has something to decode:

- **`opendroneid/transmitter-linux`** — the official C transmitter, runs on
  any Linux box with a Bluetooth adapter and/or a monitor-mode WiFi card.
- **`ArduPilot/ArduRemoteID`** — flashes an ESP32-S3 as a standalone Remote
  ID beacon (pre-built binaries available; ~$10 of hardware).

Don't run the transmitter on the same Pi as the detector.

## Notes

- Run as root — raw sockets, monitor mode, and Bluetooth all require it.
- Remote ID is only broadcast by drones registered after Sept 2023. Seeing
  zero detections usually just means nothing compliant is flying nearby.
- WiFi capture parses every 802.11 management frame in Python. On the
  Zero's single 1 GHz core this is heavy and may drop packets under busy
  2.4 GHz — fine for detection, not lossless.

## Credit & license

Derived from the DroneAware Node feeders. See `LICENSE`.
