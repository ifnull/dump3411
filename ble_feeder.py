#!/usr/bin/env python3
"""
DroneAware BLE detector — offline Remote ID capture.

Listens for BLE Remote ID advertisements (ASTM F3411, service UUID 0xFFFA),
decodes them, and prints detections to the terminal / systemd journal.
No network connection, no token, no data sharing.

Usage:
    sudo python3 ble_feeder.py [--adapter hci0] [--verbose]

Requirements:
    sudo apt install python3-bleak bluez
"""

import argparse
import asyncio
import logging
import struct
from typing import TYPE_CHECKING

from bleak import BleakScanner

if TYPE_CHECKING:                       # avoid hard import — feeder must run standalone
    from tracker import Tracker

# -- Logging -------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("droneaware.ble")

# -- Constants -----------------------------------------------------------------
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


# -- Health Check --------------------------------------------------------------

def get_cpu_temp() -> float | None:
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return None


# -- Payload Extraction --------------------------------------------------------

def extract_rid_payload(service_data: bytes) -> tuple[str, str] | tuple[None, None]:
    """
    Strip the 2-byte ASTM header (App Code 0x0D + counter) from BLE service
    data and return (rid_payload_hex, strategy).

    ASTM F3411-22a BLE service data layout:
      Byte 0:    App Code (0x0D)
      Byte 1:    Rotation counter
      Bytes 2-26: 25-byte ODID message
    """
    if len(service_data) == 27 and service_data[0] == 0x0D:
        return service_data[2:].hex(), "tail25_of_27"
    if len(service_data) == 26 and service_data[0] == 0x0D:
        return service_data[1:].hex(), "tail25_of_26"
    if len(service_data) == 25:
        return service_data.hex(), "raw25"
    return None, None


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
    """Parse a Location/Vector message (msg type 0x1) per ASTM F3411.

    See wifi_feeder.parse_location for the full byte-layout commentary.
    """
    if len(data) < 25:
        return {}

    height_type = (data[1] >> 3) & 0x01
    dir_segment = (data[1] >> 4) & 0x01
    speed_mult  = (data[1] >> 5) & 0x01

    heading = float(data[2]) + (180.0 if dir_segment else 0.0)
    speed   = data[3] * (0.75 if speed_mult else 0.25)
    vspeed  = struct.unpack_from('<b', data, 4)[0] * 0.5

    lat = struct.unpack_from('<i', data, 5)[0] * 1e-7
    lon = struct.unpack_from('<i', data, 9)[0] * 1e-7

    if abs(lat) > 90.0 or abs(lon) > 180.0:
        return {}

    geodetic_alt = struct.unpack_from('<H', data, 15)[0] * 0.5 - 1000.0
    height_m     = struct.unpack_from('<H', data, 17)[0] * 0.5 - 1000.0

    result = {
        "latitude":       round(lat, 7),
        "longitude":      round(lon, 7),
        "ground_speed":   round(speed, 2),
        "vertical_speed": round(vspeed, 2),
        "heading":        round(heading, 1),
        "height_type":    "AGL" if height_type == 1 else "Above Takeoff",
    }
    if geodetic_alt > -1000.0:
        result["altitude_geo"] = round(geodetic_alt, 1)
    if height_m > -1000.0:
        result["height_agl"] = round(height_m, 1)
    return result


def parse_system_msg(data: bytes) -> dict:
    """Parse a System message (msg type 0x4) per ASTM F3411.

    See wifi_feeder.parse_system_msg for the full byte-layout commentary.
    """
    if len(data) < 19:
        return {}
    op_lat      = struct.unpack_from('<i', data,  2)[0] * 1e-7
    op_lon      = struct.unpack_from('<i', data,  6)[0] * 1e-7
    area_count  = data[10]
    area_radius = data[11] * 10
    alt_takeoff = struct.unpack_from('<H', data, 17)[0] * 0.5 - 1000.0

    result: dict = {
        "area_count":    area_count,
        "area_radius_m": area_radius,
    }
    if abs(op_lat) <= 90.0 and abs(op_lon) <= 180.0:
        result["operator_lat"] = round(op_lat, 7)
        result["operator_lon"] = round(op_lon, 7)
    if alt_takeoff > -1000.0:
        result["alt_takeoff_geo"] = round(alt_takeoff, 1)
    return result


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


# -- BLE Detector --------------------------------------------------------------

class BLEFeeder:
    def __init__(self, adapter: str = "hci0", verbose: bool = False,
                 tracker: "Tracker | None" = None):
        self.adapter = adapter
        self.verbose = verbose
        self.tracker = tracker      # optional; when set, decoded messages also feed it
        self.count   = 0

    def on_advertisement(self, device, adv):
        """Callback for every BLE advertisement; acts on 0xFFFA service data."""
        svc_data = None
        for uuid, data in adv.service_data.items():
            if "fffa" in uuid.lower():
                svc_data = data
                break
        if svc_data is None:
            return

        rid_payload_hex, _strategy = extract_rid_payload(svc_data)
        if rid_payload_hex is None:
            log.warning(
                f"Unrecognised service data from {device.address} "
                f"({len(svc_data)} bytes: {svc_data.hex()}) — skipped"
            )
            return

        decoded = decode_rid_message(bytes.fromhex(rid_payload_hex))
        if not decoded:
            return

        if decoded.get("message_type") == "Message Pack":
            sub_messages = decoded.get("messages", [])
        else:
            sub_messages = [decoded]

        # Process Basic ID first within a pack so the tracker's MAC → uas_id
        # mapping is in place when Location / System / Operator-ID arrive.
        sub_messages = sorted(
            sub_messages, key=lambda m: 0 if m.get("message_type") == "Basic ID" else 1
        )

        self.count += 1
        mac = device.address
        for msg in sub_messages:
            mtype = msg.get("message_type", "?")

            # Feed the per-drone tracker if one was injected.
            if self.tracker is not None:
                self._update_tracker(mac, mtype, msg, adv.rssi)

            # Existing journald logging — unchanged.
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
                    f"[BLE] MAC={mac}  RSSI={adv.rssi}dBm  "
                    f"Type={mtype}  {detail}"
                )

    def _update_tracker(self, mac: str, mtype: str, msg: dict, rssi) -> None:
        """Route a decoded sub-message to the appropriate Tracker.update_*."""
        if mtype == "Basic ID":
            self.tracker.update_basic_id(
                mac=mac, uas_id=msg.get("uas_id", ""),
                id_type_raw=msg.get("id_type_raw", 0),
                ua_type_raw=msg.get("ua_type_raw", 0),
                rssi=rssi, rid_source="ble",
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
                rssi=rssi, rid_source="ble",
            )
        elif mtype == "System":
            self.tracker.update_system(
                mac=mac,
                op_lat=msg.get("operator_lat"),
                op_lon=msg.get("operator_lon"),
                alt_takeoff_m=msg.get("alt_takeoff_geo"),
                rssi=rssi, rid_source="ble",
            )
        elif mtype == "Operator ID":
            self.tracker.update_operator_id(
                mac=mac, operator_id=msg.get("operator_id", ""),
                rssi=rssi, rid_source="ble",
            )

    async def run(self):
        log.info(f"DroneAware BLE detector — adapter {self.adapter}")
        log.info("Scanning for Remote ID broadcasts (UUID 0xFFFA)...")

        scanner = BleakScanner(
            detection_callback=self.on_advertisement,
            adapter=self.adapter,
        )
        async with scanner:
            ticker = 0
            while True:
                await asyncio.sleep(1.0)
                ticker += 1
                if ticker % 60 == 0:
                    log.info(f"[Heartbeat] seen={self.count}  temp={get_cpu_temp()}°C")


def main():
    parser = argparse.ArgumentParser(
        description="DroneAware BLE Remote ID detector (offline)"
    )
    parser.add_argument(
        "--adapter", default="hci0",
        help="HCI adapter to scan with (default: hci0)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Log every decoded message, not just Basic ID / Location"
    )
    args = parser.parse_args()

    feeder = BLEFeeder(adapter=args.adapter, verbose=args.verbose)
    try:
        asyncio.run(feeder.run())
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
