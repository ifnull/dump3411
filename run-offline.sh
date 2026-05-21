#!/usr/bin/env bash
# DroneAware — fully offline runner.
#
# Detects drones via BLE (the Pi's built-in Bluetooth) and WiFi (a USB
# monitor-mode adapter) and prints decoded detections to this terminal.
# Nothing is sent to droneaware.io — no token, no uplink, no heartbeat.
# Detections are also broadcast on UDP 255.255.255.255:9999 for any device
# on your LAN, and buffered in RAM at /run/droneaware/detections.jsonl.
#
# Usage:
#   sudo ./run-offline.sh [wifi-interface]      # default wifi-interface: wlan1
#
# Find your USB WiFi adapter's interface name with:  ip link
set -u

WIFI_IFACE="${1:-wlan1}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root (raw sockets, monitor mode, Bluetooth)." >&2
    echo "Usage: sudo $0 [wifi-interface]   (default: wlan1)" >&2
    exit 1
fi

echo "=================================================="
echo " DroneAware — OFFLINE mode (no data leaves this Pi)"
echo "   BLE  : built-in Bluetooth (hci0)"
echo "   WiFi : ${WIFI_IFACE} (monitor mode)"
echo " Ctrl-C stops both feeders."
echo "=================================================="

# Kill any leftover child processes when this script exits.
trap 'kill $(jobs -p) 2>/dev/null' EXIT

python3 "$DIR/ble_feeder.py"  --offline 2>&1 | sed -u 's/^/BLE  | /' &
python3 "$DIR/wifi_feeder.py" --offline --iface "$WIFI_IFACE" 2>&1 | sed -u 's/^/WIFI | /' &
wait
