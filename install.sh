#!/usr/bin/env bash
#
# dump3411 install helper.
#
# Detects your monitor-mode Wi-Fi interface, rewrites the systemd unit's
# ExecStart paths to point at this checkout, installs the apt dependencies,
# copies the unit into /etc/systemd/system, and enables + starts the service.
#
# Idempotent — re-run safely after a `git pull` to roll out config changes.
#
# Usage:
#   sudo ./install.sh

set -euo pipefail


# --- preflight ---------------------------------------------------------------

if [[ $EUID -ne 0 ]]; then
    echo "This script must run as root (apt + systemctl)." >&2
    echo "Try: sudo ./install.sh" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "systemctl not found — dump3411's unit needs systemd." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_SRC="$SCRIPT_DIR/dump3411.service"
SERVICE_DEST="/etc/systemd/system/dump3411.service"
SERVICE_TMP="$(mktemp)"
trap 'rm -f "$SERVICE_TMP"' EXIT

if [[ ! -f "$SERVICE_SRC" ]]; then
    echo "dump3411.service not found at $SERVICE_SRC" >&2
    echo "Run this from the dump3411 repo checkout." >&2
    exit 1
fi

echo "=== dump3411 install ==="
echo "Repo path : $SCRIPT_DIR"
echo


# --- 1) apt dependencies -----------------------------------------------------

echo "[1/5] Installing apt dependencies (python3-bleak, iw, rfkill, bluez)..."
DEBIAN_FRONTEND=noninteractive apt-get update -q >/dev/null
DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    python3-bleak iw rfkill bluez >/dev/null

echo "      Optional: python3-paho-mqtt for the MQTT publisher."
read -rp "      Install paho-mqtt now? [y/N]: " ans
if [[ "${ans,,}" == "y" ]]; then
    DEBIAN_FRONTEND=noninteractive apt-get install -yq python3-paho-mqtt >/dev/null
    INSTALLED_MQTT=1
else
    INSTALLED_MQTT=0
fi
echo


# --- 2) detect monitor-mode Wi-Fi interface ----------------------------------

echo "[2/5] Detecting USB monitor-mode Wi-Fi interface..."
# Prefer wlan*/wlx* interfaces other than wlan0 (which is usually the
# built-in adapter providing the management network).
mapfile -t IFACES < <(
    ip -o link show 2>/dev/null \
        | awk -F': ' '{print $2}' \
        | awk -F'@' '{print $1}' \
        | grep -E '^(wlan|wlx)' \
        | grep -v '^wlan0$' \
    || true
)

if [[ ${#IFACES[@]} -eq 0 ]]; then
    echo "      No candidate USB Wi-Fi interface found (looked for wlan*/wlx* except wlan0)."
    echo "      Make sure your monitor-mode adapter is plugged in and listed by 'ip link'."
    read -rp "      Specify interface manually (e.g. wlan1): " WIFI_IFACE
elif [[ ${#IFACES[@]} -eq 1 ]]; then
    WIFI_IFACE="${IFACES[0]}"
    echo "      Auto-detected: $WIFI_IFACE"
else
    echo "      Multiple candidates:"
    for i in "${!IFACES[@]}"; do
        echo "        $((i+1))) ${IFACES[i]}"
    done
    read -rp "      Pick one [1-${#IFACES[@]}]: " choice
    if ! [[ "$choice" =~ ^[0-9]+$ ]] || (( choice < 1 || choice > ${#IFACES[@]} )); then
        echo "      Invalid choice — abort." >&2
        exit 1
    fi
    WIFI_IFACE="${IFACES[$((choice - 1))]}"
fi

if [[ -z "$WIFI_IFACE" ]]; then
    echo "No Wi-Fi interface chosen — abort." >&2
    exit 1
fi
echo "      Using: $WIFI_IFACE"
echo


# --- 3) rewrite the unit -----------------------------------------------------

echo "[3/5] Generating systemd unit..."
# Substitute the ExecStart path and the --wifi-iface argument from the
# checked-in template. The pristine template lives in $SERVICE_SRC, so this is
# safe to re-run without ever modifying the repo copy.
sed \
    -e "s|/home/pi/dump3411/dump3411.py|$SCRIPT_DIR/dump3411.py|g" \
    -e "s| --wifi-iface wlan1 | --wifi-iface $WIFI_IFACE |g" \
    "$SERVICE_SRC" > "$SERVICE_TMP"
echo "      ExecStart -> $SCRIPT_DIR/dump3411.py"
echo "      Interface -> $WIFI_IFACE"
echo


# --- 4) install + reload -----------------------------------------------------

echo "[4/5] Installing unit and reloading systemd..."
install -m 644 "$SERVICE_TMP" "$SERVICE_DEST"
systemctl daemon-reload
echo


# --- 5) enable + start -------------------------------------------------------

echo "[5/5] Enabling and starting dump3411..."
systemctl enable --now dump3411 >/dev/null
sleep 2

echo
echo "=== status ==="
systemctl --no-pager status dump3411 | head -8 || true
echo

LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}' || echo "<host>")"
echo "Dashboard : http://${LAN_IP}:8754/"
echo "JSON feed : http://${LAN_IP}:8754/data/remoteid.json"
echo "Live tail : sudo journalctl -u dump3411 -f"
echo

if [[ "$INSTALLED_MQTT" == "1" ]]; then
    echo "MQTT is installed — to enable publishing, create /etc/dump3411.env with:"
    echo "    MQTT_BROKER=mqtt.lan:1883"
    echo "    MQTT_USER=dump3411"
    echo "    MQTT_PASSWORD=…"
    echo "Then: sudo systemctl restart dump3411"
    echo
fi

echo "To enable persistent detection history, add to /etc/dump3411.env:"
echo "    HISTORY_DB=/var/lib/dump3411/history.db"
echo "Then: sudo mkdir -p /var/lib/dump3411 && sudo systemctl restart dump3411"
echo
echo "Done."
