# DroneAware Node — Offline Setup (Pi Zero W)

Run the drone detector with **no connection to droneaware.io**. Detections
print to your terminal only. Nothing is enrolled, uploaded, or shared.

This bypasses the official `install.sh` (which enrolls your node with the
third-party service). Instead you run the Python feeders directly with the
added `--offline` flag.

## Hardware

- Raspberry Pi Zero W 1.1 — built-in Bluetooth 4.1 handles BLE detection.
- USB WiFi adapter with monitor mode — **Alfa AWUS036NEH (RT3070)** works.
- micro-USB OTG adapter to connect the WiFi adapter to the Pi Zero.
- A solid 5V power supply (2.5A+). The AWUS036NEH draws real current; a
  powered USB hub avoids brownouts.

## OS

Use **Raspberry Pi OS Bookworm, 32-bit Lite** (the Pi Zero W 1 is ARMv6 and
cannot run 64-bit). The feeders require **Python 3.10+** — Bookworm ships 3.11.
Older Bullseye (Python 3.9) will not run this code.

## Install dependencies

```bash
sudo apt update
sudo apt install -y python3-bleak python3-requests python3-serial iw rfkill
```

Using apt (not pip) avoids Bookworm's "externally-managed environment" error.

## Run

Find your USB WiFi adapter's interface name (the built-in WiFi is `wlan0`;
the USB adapter is usually `wlan1`):

```bash
ip link
```

Then start both detectors:

```bash
sudo ./run-offline.sh wlan1
```

Decoded detections print live, prefixed `BLE |` or `WIFI |`. Press Ctrl-C to
stop — the WiFi adapter is returned to normal mode on exit.

To run just one detector:

```bash
sudo python3 ble_feeder.py  --offline
sudo python3 wifi_feeder.py --offline --iface wlan1
```

Add `--verbose` to either for every raw packet.

## Where detections go

- **Terminal** — decoded Basic ID and Location/Vector lines.
- **UDP broadcast** `255.255.255.255:9999` — listen from any LAN device with
  `nc -luk 9999`.
- **RAM buffer** `/run/droneaware/detections.jsonl` — last ~60 min, JSON lines,
  never written to the SD card. Tail it with `tail -f`.

## What `--offline` changes

- No enrollment token is required (the stock feeders exit without one).
- The HTTP forwarder is disabled — no batches are POSTed anywhere.
- The 60-second heartbeat to `api.droneaware.io` is skipped.

With `--offline`, no network traffic leaves the Pi.

## Notes & limits

- The Pi Zero's **built-in WiFi cannot do monitor mode** — that is why the USB
  adapter is required. BLE uses the built-in Bluetooth and needs no extra hardware.
- Most consumer drones (DJI) broadcast Remote ID over **BLE**, so the BLE
  feeder alone catches a lot.
- WiFi capture parses every 802.11 management frame in Python. On the Zero's
  single 1 GHz core this is heavy and may drop packets under a busy 2.4 GHz
  band — fine for detection, not lossless.
- Remote ID is only broadcast by drones registered after Sept 2023; seeing
  zero detections often just means nothing compliant is flying nearby.
