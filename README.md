# drone-aware-zero

A drone Remote ID detector for the **Raspberry Pi Zero W**. Detects nearby
drones over BLE and WiFi and prints them to the terminal. Fully offline — no
account, no token, no data leaves the Pi.

Stripped-down fork of the DroneAware node feeders: the network uplink,
enrollment, heartbeat, GPS, and on-disk buffers have been removed. Detection
and decoding only.

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

`bluez` is usually already present. Installing via apt avoids Bookworm's
"externally-managed environment" pip error.

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

Ctrl-C stops both and returns the WiFi adapter to normal mode.

### As a service (starts on boot)

The service files assume the repo lives at `/home/pi/drone-aware-zero` and the
USB adapter is `wlan1`. Edit the paths/interface in the `.service` files first
if yours differ.

```bash
sudo cp droneaware-ble.service droneaware-wifi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now droneaware-ble droneaware-wifi
```

Watch detections live:

```bash
journalctl -u droneaware-ble -u droneaware-wifi -f
```

Other commands:

```bash
sudo systemctl status droneaware-wifi      # check state
sudo systemctl restart droneaware-wifi     # restart
sudo systemctl disable --now droneaware-*  # stop and remove from boot
```

## What you'll see

Detections print one line each:

```
[BLE] MAC=...  RSSI=-62dBm  Type=Basic ID  UAS-ID=1581F...
[WiFi-Beacon] MAC=...  RSSI=-71dBm  Type=Location/Vector  lat=40.71 lon=-74.00
```

A `[Heartbeat]` line every 60 seconds confirms each detector is alive and
shows the running detection count. Add `--verbose` (or edit the service file)
to log every decoded message type, not just Basic ID and Location.

## Testing without a real drone

Install the **OpenDroneID transmitter** app on an Android phone (broadcasts
genuine ASTM F3411 Remote ID over BLE and WiFi) and start a transmit — both
detectors should pick it up within a few seconds.

## Notes

- Run as root — raw sockets, monitor mode, and Bluetooth all require it.
- Remote ID is only broadcast by drones registered after Sept 2023. Seeing
  zero detections usually just means nothing compliant is flying nearby.
- WiFi capture parses every 802.11 management frame in Python. On the Zero's
  single 1 GHz core this is heavy and may drop packets under busy 2.4 GHz —
  fine for detection, not lossless.

## Credit & license

Derived from the DroneAware Node feeders. See `LICENSE`.
