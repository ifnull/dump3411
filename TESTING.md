# Testing dump3411 without a real drone

Three options for putting Remote ID on the air so you can verify dump3411's
detect → decode → feed/dashboard path. **Use a separate machine to transmit**
— don't try to transmit and receive on the same host.

Listed in roughly increasing fidelity. The first is what dump3411 was
developed and continuously verified against; the latter two are
spec-compliant and worth using when you need every decoded field to
round-trip exactly.

## 1. cyber-defence-campus / droneRemoteIDSpoofer

→ **[github.com/cyber-defence-campus/droneRemoteIDSpoofer](https://github.com/cyber-defence-campus/droneRemoteIDSpoofer)**

> ⚠️ Several repositories go by similar names. dump3411 was specifically
> tested with the **cyber-defence-campus** one linked above. Others may
> work but aren't what we verified against.

A Python spoofer that broadcasts Wi-Fi Beacon and (optionally) BLE Remote
ID. Runs on Linux and needs:

- Python 3.10+ and scapy (`pip install scapy`)
- A USB Wi-Fi adapter that supports monitor mode (for the Wi-Fi side)
- A Bluetooth adapter (only if you want BLE spoofing too)

### Quick run — Wi-Fi only

On the transmit machine, with `wlan1` as the monitor-mode adapter:

```bash
git clone https://github.com/cyber-defence-campus/droneRemoteIDSpoofer
cd droneRemoteIDSpoofer

/usr/bin/python3 -m venv .venv
.venv/bin/pip install scapy

sudo ./interface-monitor.sh wlan1        # put the iface in monitor mode

sudo .venv/bin/python3 spoof_drones.py -i wlan1
```

On the dump3411 side the journal should start logging `[WiFi-Beacon]`
lines within a few seconds, the dashboard's `wifi_beacon` counter should
climb, and `Spoofed_Serial_NNNNN` rows should appear in the drones table.

### Adding BLE

The spoofer's BLE backend talks to the Bluetooth controller via raw HCI,
which requires bluetoothd to be out of the way and the controller in
legacy-advertising mode:

```bash
sudo systemctl stop bluetooth
sudo modprobe -r btusb && sudo modprobe btusb       # reset to a clean LE state

sudo .venv/bin/python3 spoof_drones.py -i wlan1 -t both --ble-adapter hci0
```

When you're done, `sudo systemctl start bluetooth` restores normal Bluetooth.

### Spoofer-side quirks that show up in the feed

dump3411's decoder is spec-compliant. This spoofer isn't strictly
spec-compliant in two places, which will surface as:

- **`gs` reads ~3× the encoded speed.** The spoofer always sets the
  speed-multiplier status bit (×0.75) but encodes the speed value with the
  ×0.25 base, so a spec decoder reads three times the intended value.
- **`track` is rendered modulo 180°.** The spoofer places the
  direction-segment bit at status-byte bit 1; the spec puts it at bit 4. Any
  heading ≥ 180° appears as `heading − 180`.

Neither affects the spoofer's usefulness for confirming the receive
pipeline — both fields *exist*, they just read offset. Use one of the
spec-compliant transmitters below if you need correct values.

## 2. opendroneid / transmitter-linux

→ **[github.com/opendroneid/transmitter-linux](https://github.com/opendroneid/transmitter-linux)**

The official OpenDroneID Linux transmitter. Written in C, supports BLE +
Wi-Fi, fully spec-compliant. The right choice when you want every decoded
field to round-trip exactly.

## 3. ArduPilot / ArduRemoteID on an ESP32-S3

→ **[github.com/ArduPilot/ArduRemoteID](https://github.com/ArduPilot/ArduRemoteID)**

Pre-built firmware for the ESP32-S3. Flash with the Espressif flash tool,
plug in power, and it's a standalone Remote ID beacon you can leave on the
bench. ~$10–15 of hardware, fully spec-compliant.
