#!/usr/bin/env bash
# DroneAware — offline runner (manual / testing).
#
# Runs both detectors in one terminal. For boot-time autostart use the
# systemd services instead — see README.md.
#
# Usage:
#   sudo ./run-offline.sh [wifi-interface]      # default wifi-interface: wlan1
set -u

WIFI_IFACE="${1:-wlan1}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root (raw sockets, monitor mode, Bluetooth)." >&2
    echo "Usage: sudo $0 [wifi-interface]   (default: wlan1)" >&2
    exit 1
fi

echo "DroneAware — BLE (hci0) + WiFi (${WIFI_IFACE}).  Ctrl-C stops both."

trap 'kill $(jobs -p) 2>/dev/null' EXIT

python3 "$DIR/ble_feeder.py"  2>&1 | sed -u 's/^/BLE  | /' &
python3 "$DIR/wifi_feeder.py" --iface "$WIFI_IFACE" 2>&1 | sed -u 's/^/WIFI | /' &
wait
