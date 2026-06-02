#!/usr/bin/env python3
"""
DroneAware WiFi detector — offline Remote ID capture.

Puts a USB monitor-mode adapter (e.g. Alfa AWUS036NEH, RT3070) into monitor
mode, hops 2.4 GHz channels 1-11, decodes Remote ID from 802.11 beacon frames
(ASTM F3411), and prints detections to the terminal / systemd journal.
No network connection, no token, no data sharing.

Supports:
  - Wi-Fi Beacon transport (vendor IE, OUI FA:0B:BC, type 0x0D)  [F3411-19/22a]
  - Wi-Fi NAN transport detection (action frames, OUI 50:6F:9A)  [F3411-22a]

Uses raw AF_PACKET sockets (stdlib only — no scapy dependency).

Usage:
    sudo python3 wifi_feeder.py --iface wlan1 [--verbose]

Requirements:
    sudo apt install iw rfkill
"""

import argparse
import hashlib
import logging
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:                       # avoid hard import — feeder must run standalone
    from tracker import Tracker

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("droneaware.wifi")


# -- Constants -----------------------------------------------------------------

# Vendor-specific IE OUI for ASTM F3411 Wi-Fi Beacon transport
ASTM_OUI      = bytes([0xFA, 0x0B, 0xBC])
ASTM_OUI_TYPE = 0x0D  # Remote ID app code

# Wi-Fi Alliance NAN OUI (action frames)
NAN_OUI      = bytes([0x50, 0x6F, 0x9A])
NAN_OUI_TYPE = 0x13  # NAN

# ASTM F3411-22a Open Drone ID NAN Service ID
# First 6 bytes of SHA-256("org.opendroneid.remoteid")
# Consumer NAN (Apple AirDrop, Google Nearby Share, etc.) will never match this.
ODID_NAN_SERVICE_ID = hashlib.sha256(b"org.opendroneid.remoteid").digest()[:6]

# 2.4 GHz channels (RT3070 is 2.4 GHz only)
CHANNELS_24 = list(range(1, 12))  # 1-11 (US band)

MSG_TYPE = {
    0x0: "Basic ID",
    0x1: "Location/Vector",
    0x2: "Authentication",
    0x3: "Self ID",
    0x4: "System",
    0x5: "Operator ID",
    0xF: "Message Pack",
}

ID_TYPE = {
    0: "None",
    1: "Serial Number (ANSI/CTA-2063-A)",
    2: "CAA Assigned",
    3: "UTM Assigned",
    4: "Specific Session ID",
}

UA_TYPE = {
    0: "None",
    1: "Aeroplane",
    2: "Helicopter/Multirotor",
    3: "Gyroplane",
    4: "Hybrid Lift",
    5: "Ornithopter",
    6: "Glider",
    7: "Kite",
    8: "Free Balloon",
    9: "Captive Balloon",
    10: "Airship",
    11: "Free Fall/Parachute",
    12: "Rocket",
    13: "Tethered Powered Aircraft",
    14: "Ground Obstacle",
    255: "Other",
}


# -- Remote ID Decoder ---------------------------------------------------------

def parse_basic_id(data: bytes) -> dict:
    if len(data) < 25:
        return {}
    id_type = (data[1] >> 4) & 0x0F
    ua_type = data[1] & 0x0F
    uas_id  = data[2:22].rstrip(b'\x00').decode('ascii', errors='replace')
    return {
        "id_type":     ID_TYPE.get(id_type, f"Unknown({id_type})"),
        "ua_type":     UA_TYPE.get(ua_type, f"Unknown({ua_type})"),
        "id_type_raw": id_type,         # raw enum — consumed by Tracker.update_basic_id
        "ua_type_raw": ua_type,
        "uas_id":      uas_id,
    }


def parse_location(data: bytes) -> dict:
    if len(data) < 25:
        return {}
    speed_mult  = data[1] & 0x01
    height_type = (data[1] >> 2) & 0x01
    lat = struct.unpack_from('<i', data, 2)[0] * 1e-7
    lon = struct.unpack_from('<i', data, 6)[0] * 1e-7

    # Reject null/placeholder GPS values broadcast before lock (e.g. DJI firmware
    # transmits lat>90 or lon>180 as a sentinel until GPS acquires).
    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return {}

    alt_geodetic = struct.unpack_from('<H', data, 12)[0] * 0.5 - 1000.0
    height       = struct.unpack_from('<H', data, 14)[0] * 0.5 - 1000.0
    speed        = data[16] * (0.75 if speed_mult else 0.25)
    vspeed       = data[17] * 0.5 - 62.0
    heading      = struct.unpack_from('<H', data, 18)[0] * 0.01
    return {
        "latitude":       round(lat, 7),
        "longitude":      round(lon, 7),
        "altitude_geo":   round(alt_geodetic, 1),
        "height_agl":     round(height, 1),
        "ground_speed":   round(speed, 2),
        "vertical_speed": round(vspeed, 2),
        "heading":        round(heading, 1),
        "height_type":    "AGL" if height_type == 0 else "Above Takeoff",
    }


def parse_system_msg(data: bytes) -> dict:
    if len(data) < 16:
        return {}
    op_lat      = struct.unpack_from('<i', data, 4)[0] * 1e-7
    op_lon      = struct.unpack_from('<i', data, 8)[0] * 1e-7
    area_count  = data[12]
    area_radius = data[13] * 10
    alt_takeoff = struct.unpack_from('<H', data, 14)[0] * 0.5 - 1000.0
    return {
        "operator_lat":    round(op_lat, 7),
        "operator_lon":    round(op_lon, 7),
        "area_count":      area_count,
        "area_radius_m":   area_radius,
        "alt_takeoff_geo": round(alt_takeoff, 1),
    }


def parse_operator_id(data: bytes) -> dict:
    if len(data) < 22:
        return {}
    return {
        "operator_id_type": data[1],
        "operator_id":      data[2:22].rstrip(b'\x00').decode('ascii', errors='replace'),
    }


def parse_message_pack(data: bytes) -> list:
    if len(data) < 3:
        return []
    msg_size  = data[1]
    msg_count = data[2]
    messages  = []
    for i in range(msg_count):
        offset = 3 + i * msg_size
        if offset + msg_size > len(data):
            break
        messages.append(data[offset: offset + msg_size])
    return messages


def decode_rid_message(raw_bytes: bytes) -> dict | None:
    if len(raw_bytes) < 2:
        return None
    msg_type  = (raw_bytes[0] >> 4) & 0x0F
    type_name = MSG_TYPE.get(msg_type, f"Unknown(0x{msg_type:X})")
    result    = {"message_type": type_name, "raw_hex": raw_bytes.hex().upper()}
    if msg_type == 0x0:
        result.update(parse_basic_id(raw_bytes))
    elif msg_type == 0x1:
        result.update(parse_location(raw_bytes))
    elif msg_type == 0x4:
        result.update(parse_system_msg(raw_bytes))
    elif msg_type == 0x5:
        result.update(parse_operator_id(raw_bytes))
    elif msg_type == 0xF:
        sub_msgs = parse_message_pack(raw_bytes)
        result["messages"] = [m for m in (decode_rid_message(s) for s in sub_msgs) if m]
    return result


# -- Raw 802.11 Frame Parsers --------------------------------------------------
# Replaces scapy — uses stdlib socket + struct only.

def _parse_radiotap(data: bytes) -> tuple[int, int | None]:
    """
    Parse RadioTap header (IEEE 802.11-2020 Annex I).
    Returns (header_length, rssi_dbm_or_None).

    Fields are walked in present-bitmap order with natural alignment relative
    to the start of the header. Only fields needed to reach dBm Signal (bit 5)
    are decoded; the rest are skipped by size.
    """
    if len(data) < 8:
        return len(data), None

    rt_len  = struct.unpack_from('<H', data, 2)[0]
    present = struct.unpack_from('<I', data, 4)[0]

    rssi   = None
    offset = 8  # first field starts after the fixed 8-byte header

    # Bit 31 (EXT): chipsets like Atheros AR9271 chain additional present words
    # before field data begins. Read each word and check its own bit 31 — do not
    # re-check the first word, which never changes. `present` (first word) is
    # preserved for field parsing since standard bits 0–28 live only there.
    ext_word = present
    while ext_word & (1 << 31):
        if offset + 4 > len(data):
            return rt_len, None
        ext_word = struct.unpack_from('<I', data, offset)[0]
        offset += 4

    # Bit 0: TSFT — uint64, align 8
    if present & (1 << 0):
        offset = (offset + 7) & ~7
        offset += 8
    # Bit 1: Flags — uint8
    if present & (1 << 1):
        offset += 1
    # Bit 2: Rate — uint8
    if present & (1 << 2):
        offset += 1
    # Bit 3: Channel — uint16 freq + uint16 flags, align 2
    if present & (1 << 3):
        offset = (offset + 1) & ~1
        offset += 4
    # Bit 4: FHSS — uint8 hop_set + uint8 hop_pattern
    if present & (1 << 4):
        offset += 2
    # Bit 5: dBm Antenna Signal — int8
    if present & (1 << 5):
        if offset < len(data):
            rssi = struct.unpack_from('b', data, offset)[0]
        offset += 1

    return rt_len, rssi


def _mac_str(b: bytes) -> str:
    return ':'.join(f'{x:02x}' for x in b)


def _parse_dot11_mgmt(data: bytes) -> tuple[int, str, int] | None:
    """
    Parse an 802.11 management frame MAC header.

    Returns (subtype, addr2_mac_str, body_offset) or None if not a mgmt frame.
    addr2 is the transmitter (Source Address).
    Management frames have a fixed 24-byte MAC header.
    """
    if len(data) < 24:
        return None
    fc0 = data[0]
    frame_type    = (fc0 >> 2) & 0x3
    frame_subtype = (fc0 >> 4) & 0xF
    if frame_type != 0:          # 0 = management
        return None
    addr2 = _mac_str(data[10:16])
    return frame_subtype, addr2, 24


def _extract_beacon_rid(body: bytes) -> bytes | None:
    """
    Walk 802.11 beacon Information Elements looking for the vendor-specific
    ASTM F3411 Remote ID payload (OUI FA:0B:BC, type 0x0D).

    Beacon frame body layout (after 24-byte MAC header):
      Fixed parameters: 8 (timestamp) + 2 (beacon interval) + 2 (capability) = 12 bytes
      Then: IE chain — tag(1) + length(1) + value(length)

    Returns the ODID message (single message or Message Pack) or None.
    """
    offset = 12  # skip fixed parameters
    while offset + 2 <= len(body):
        tag_id  = body[offset]
        tag_len = body[offset + 1]
        end     = offset + 2 + tag_len
        if end > len(body):
            break
        if tag_id == 221:  # Vendor Specific IE
            info = body[offset + 2: end]
            if len(info) >= 5 and info[:3] == ASTM_OUI and info[3] == ASTM_OUI_TYPE:
                return info[4:]
        offset = end
    return None


def _is_nan_action(body: bytes) -> bool:
    """
    Detect Wi-Fi NAN action frames carrying ODID (ASTM F3411-22a).
    Requires the ODID NAN Service ID to be present in the frame body, which
    filters out consumer NAN traffic (Apple AirDrop/Handoff, Google Nearby
    Share) that uses the same OUI/type but a different Service ID.
    """
    if not (
        len(body) >= 6 and
        body[0] == 4 and           # Category: Public Action
        body[2:5] == NAN_OUI and
        body[5] == NAN_OUI_TYPE
    ):
        return False
    return ODID_NAN_SERVICE_ID in body[:64]


# -- Monitor Mode --------------------------------------------------------------

_NM_CONF = "/etc/NetworkManager/conf.d/droneaware.conf"


def _get_backhaul_iface() -> str | None:
    """Return the interface currently carrying the default route."""
    try:
        r = subprocess.run(
            ["ip", "route", "get", "1.1.1.1"],
            capture_output=True, text=True, timeout=5, check=False,
        )
        parts = r.stdout.split()
        if "dev" in parts:
            return parts[parts.index("dev") + 1]
    except Exception:
        pass
    return None


def _get_iface_mac(iface: str) -> str | None:
    try:
        with open(f"/sys/class/net/{iface}/address") as f:
            return f.read().strip()
    except Exception:
        return None


def _set_nm_unmanaged(mac: str):
    """
    Tell NetworkManager to leave the monitor-mode adapter alone. If NM manages
    the interface it fights monitor mode, causing zero packet capture.
    """
    nm_body = (
        "# DroneAware — keep NetworkManager off the monitor-mode adapter.\n"
        "[keyfile]\n"
        f"unmanaged-devices=mac:{mac}\n"
    )
    try:
        os.makedirs(os.path.dirname(_NM_CONF), exist_ok=True)
        with open(_NM_CONF, "w") as f:
            f.write(nm_body)
        log.info(f"[Monitor] NetworkManager set to ignore {mac}")
    except Exception as e:
        log.warning(f"[Monitor] Could not update NM config: {e}")


def _ensure_monitor_safe(iface: str):
    """
    Refuse to monitor-mode the active backhaul interface. If the interface is
    NM-managed but not the backhaul, release it so monitor mode sticks.
    """
    backhaul = _get_backhaul_iface()
    if backhaul and iface == backhaul:
        log.error(f"Refusing to monitor {iface} — it is your active network interface.")
        log.error("Plug the USB adapter into a different interface and re-run.")
        sys.exit(1)

    try:
        r = subprocess.run(
            ["nmcli", "-g", "GENERAL.STATE", "device", "show", iface],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if "unmanaged" not in r.stdout.lower():
            mac = _get_iface_mac(iface)
            log.warning(f"[Monitor] {iface} is NetworkManager-managed — releasing it.")
            subprocess.run(
                ["nmcli", "device", "set", iface, "managed", "no"],
                capture_output=True, check=False,
            )
            if mac:
                _set_nm_unmanaged(mac)
    except Exception:
        pass


def set_monitor_mode(iface: str):
    """Bring interface up in monitor mode."""
    _ensure_monitor_safe(iface)
    log.info(f"Setting {iface} to monitor mode...")
    subprocess.run(["rfkill", "unblock", "all"], check=False, capture_output=True)
    subprocess.run(["ip", "link", "set", iface, "down"], check=True)
    subprocess.run(["iw", "dev", iface, "set", "type", "monitor"], check=True)
    subprocess.run(["ip", "link", "set", iface, "up"], check=True)
    log.info(f"{iface} is now in monitor mode")


def restore_managed_mode(iface: str):
    """Restore interface to managed mode on exit."""
    log.info(f"Restoring {iface} to managed mode...")
    try:
        subprocess.run(["ip", "link", "set", iface, "down"], check=False)
        subprocess.run(["iw", "dev", iface, "set", "type", "managed"], check=False)
        subprocess.run(["ip", "link", "set", iface, "up"], check=False)
    except Exception as e:
        log.warning(f"Could not restore managed mode: {e}")


def set_channel(iface: str, channel: int):
    """Set the monitor interface to a specific 2.4 GHz channel."""
    subprocess.run(
        ["iw", "dev", iface, "set", "channel", str(channel)],
        check=False, capture_output=True,
    )


# -- Channel Hopper ------------------------------------------------------------

class ChannelHopper(threading.Thread):
    """Cycles through 2.4 GHz channels at a fixed dwell time."""

    def __init__(self, iface: str, channels: list, dwell: float):
        super().__init__(daemon=True)
        self.iface    = iface
        self.channels = channels
        self.dwell    = dwell
        self._stop    = threading.Event()

    def run(self):
        log.info(f"Channel hopper started: {self.channels} @ {self.dwell}s dwell")
        while not self._stop.is_set():
            for ch in self.channels:
                if self._stop.is_set():
                    break
                set_channel(self.iface, ch)
                time.sleep(self.dwell)

    def stop(self):
        self._stop.set()


# -- WiFi Detector -------------------------------------------------------------

class WiFiFeeder:
    def __init__(self, iface: str, verbose: bool = False, channel_dwell: float = 0.2,
                 tracker: "Tracker | None" = None):
        self.iface     = iface
        self.verbose   = verbose
        self.tracker   = tracker      # optional; when set, decoded messages also feed it
        self.hopper    = ChannelHopper(iface, CHANNELS_24, channel_dwell)
        self.count     = 0
        self.nan_count = 0
        self._stop     = threading.Event()  # for orderly shutdown from another thread

    def _on_packet(self, data: bytes):
        rt_len, rssi = _parse_radiotap(data)
        if rt_len >= len(data):
            return

        mac_data = data[rt_len:]
        header = _parse_dot11_mgmt(mac_data)
        if header is None:
            return

        subtype, addr2, body_offset = header
        body = mac_data[body_offset:]

        # ---- Wi-Fi Beacon Remote ID (subtype 8) ----
        if subtype == 8:
            rid_payload = _extract_beacon_rid(body)
            if rid_payload is None:
                return

            decoded = decode_rid_message(rid_payload)
            if decoded is None:
                return

            if decoded.get("message_type") == "Message Pack":
                sub_messages = decoded.get("messages", [])
            else:
                sub_messages = [decoded]

            # Process Basic ID first within a pack so the tracker's MAC →
            # uas_id mapping is in place when Location / System / Operator-ID
            # arrive in the same beacon.
            sub_messages = sorted(
                sub_messages,
                key=lambda m: 0 if m.get("message_type") == "Basic ID" else 1,
            )

            for msg in sub_messages:
                mtype = msg.get("message_type", "?")
                # Drop Location/Vector messages with no valid GPS fix.
                if mtype == "Location/Vector" and "latitude" not in msg:
                    continue
                self.count += 1

                # Feed the per-drone tracker if one was injected.
                if self.tracker is not None:
                    self._update_tracker(addr2, mtype, msg, rssi)

                if self.verbose or mtype in ("Basic ID", "Location/Vector"):
                    uas_id = msg.get("uas_id", "")
                    lat    = msg.get("latitude")
                    lon    = msg.get("longitude")
                    if uas_id:
                        detail = f"UAS-ID={uas_id}"
                    elif lat is not None:
                        detail = f"lat={lat} lon={lon}"
                    else:
                        detail = ""
                    log.info(
                        f"[WiFi-Beacon] MAC={addr2}  RSSI={rssi}dBm  "
                        f"Type={mtype}  {detail}"
                    )
            return

        # ---- Wi-Fi NAN Remote ID (subtype 13 — action frame) ----
        if subtype == 13 and _is_nan_action(body):
            self.nan_count += 1
            if self.verbose:
                log.info(
                    f"[WiFi-NAN] MAC={addr2}  RSSI={rssi}dBm  "
                    f"raw={body.hex().upper()[:40]}..."
                )

    def _update_tracker(self, mac: str, mtype: str, msg: dict, rssi) -> None:
        """Route a decoded sub-message to the appropriate Tracker.update_*."""
        if mtype == "Basic ID":
            self.tracker.update_basic_id(
                mac=mac, uas_id=msg.get("uas_id", ""),
                id_type_raw=msg.get("id_type_raw", 0),
                ua_type_raw=msg.get("ua_type_raw", 0),
                rssi=rssi, rid_source="wifi_beacon",
            )
        elif mtype == "Location/Vector" and "latitude" in msg:
            self.tracker.update_location(
                mac=mac,
                lat=msg["latitude"], lon=msg["longitude"],
                alt_geo_m=msg.get("altitude_geo"),
                height_agl_m=msg.get("height_agl"),
                gs_mps=msg.get("ground_speed"),
                heading_deg=msg.get("heading"),
                vspeed_mps=msg.get("vertical_speed"),
                rssi=rssi, rid_source="wifi_beacon",
            )
        elif mtype == "System":
            self.tracker.update_system(
                mac=mac,
                op_lat=msg.get("operator_lat"),
                op_lon=msg.get("operator_lon"),
                alt_takeoff_m=msg.get("alt_takeoff_geo"),
                rssi=rssi, rid_source="wifi_beacon",
            )
        elif mtype == "Operator ID":
            self.tracker.update_operator_id(
                mac=mac, operator_id=msg.get("operator_id", ""),
                rssi=rssi, rid_source="wifi_beacon",
            )

    def _heartbeat_loop(self):
        while True:
            time.sleep(60)
            log.info(f"[Heartbeat] Beacon RID={self.count}  NAN={self.nan_count}")

    def stop(self) -> None:
        """Signal run() to exit; restore_managed_mode() runs in the finally."""
        self._stop.set()

    def run(self):
        log.info(f"DroneAware WiFi detector — interface {self.iface}")
        log.info(f"Channels: {CHANNELS_24}")

        set_monitor_mode(self.iface)
        self.hopper.start()

        log.info("Scanning for Remote ID beacon frames (ASTM F3411)...")

        sock = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(3))
        sock.bind((self.iface, 0))
        sock.settimeout(1.0)

        threading.Thread(target=self._heartbeat_loop, daemon=True).start()

        try:
            while not self._stop.is_set():
                try:
                    data = sock.recv(65535)
                    self._on_packet(data)
                except socket.timeout:
                    continue
        except KeyboardInterrupt:
            log.info("Stopped.")
        finally:
            sock.close()
            self.hopper.stop()
            restore_managed_mode(self.iface)
            log.info(f"[Summary] Beacon RID={self.count}  NAN frames={self.nan_count}")


def _handle_sigterm(signum, frame):
    """systemctl stop sends SIGTERM — raise so run()'s finally restores the adapter."""
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(
        description="DroneAware WiFi Remote ID detector (offline)"
    )
    parser.add_argument(
        "--iface", default="wlan1",
        help="Monitor-mode interface — the USB adapter (default: wlan1)"
    )
    parser.add_argument(
        "--channel-dwell", type=float, default=0.2,
        help="Seconds to dwell on each channel before hopping (default: 0.2)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every decoded message and NAN frame"
    )
    args = parser.parse_args()

    signal.signal(signal.SIGTERM, _handle_sigterm)

    feeder = WiFiFeeder(
        iface=args.iface,
        verbose=args.verbose,
        channel_dwell=args.channel_dwell,
    )
    feeder.run()


if __name__ == "__main__":
    main()
