#!/usr/bin/env bash
# DroneAware — offline runner (manual / testing).
#
# Runs droneaware.py with both detectors plus the local JSON feed on :8754.
# For boot-time autostart use the systemd unit instead — see README.md.
#
# Usage:
#   sudo ./run-offline.sh [wifi-interface]      # default: wlan1
set -u

WIFI_IFACE="${1:-wlan1}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Must run as root (raw sockets, monitor mode, Bluetooth)." >&2
    echo "Usage: sudo $0 [wifi-interface]   (default: wlan1)" >&2
    exit 1
fi

echo "DroneAware — BLE (hci0) + WiFi (${WIFI_IFACE}) + feed on :8754.  Ctrl-C to stop."
exec python3 "$DIR/droneaware.py" --wifi-iface "$WIFI_IFACE" --serve 0.0.0.0:8754
