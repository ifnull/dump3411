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

from bleak import BleakScanner

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
        "id_type": ID_TYPE.get(id_type, f"Unknown({id_type})"),
        "ua_type": UA_TYPE.get(ua_type, f"Unknown({ua_type})"),
        "uas_id":  uas_id,
    }


def parse_location(data: bytes) -> dict:
    if len(data) < 25:
        return {}
    speed_mult  = data[1] & 0x01
    height_type = (data[1] >> 2) & 0x01
    lat = struct.unpack_from('<i', data, 2)[0] * 1e-7
    lon = struct.unpack_from('<i', data, 6)[0] * 1e-7
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


# -- BLE Detector --------------------------------------------------------------

class BLEFeeder:
    def __init__(self, adapter: str = "hci0", verbose: bool = False):
        self.adapter = adapter
        self.verbose = verbose
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

        self.count += 1
        for msg in sub_messages:
            if self.verbose or msg.get("message_type") in ("Basic ID", "Location/Vector"):
                mtype  = msg.get("message_type", "?")
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
                    f"[BLE] MAC={device.address}  RSSI={adv.rssi}dBm  "
                    f"Type={mtype}  {detail}"
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
