"""The satellite's telemetry beacon (one serialized tel_downlink_t, src/telemetry/telemetry.h, sent as the user data of
a CCSDS_APID_TELEMETRY space packet), decoded.

The layout isn't written down here: ccsds.telemetry_layout() learns each field's offset, width and type from PVDXos's
own serializer (telemetry_downlink_serialize()), built for the Mac, and fails if tel_downlink_t has a field the ground
doesn't know. Enum and flag names come from PVDXos's headers.
"""

import struct

import ccsds
import firmware

_layout = None


def layout():
    global _layout
    if _layout is None:
        _layout = ccsds.telemetry_layout()
    return _layout


def size():
    """Bytes the serializer fills (the rest of the TELEMETRY_DOWNLINK_SIZE bytes are spare zeros)."""
    name, index, offset, width, kind = layout()[-1]
    return offset + width


def _value(raw, kind):
    if kind == "f":
        return struct.unpack(">f", raw)[0] if len(raw) == 4 else struct.unpack(">d", raw)[0]
    if kind == "b":
        return bool(raw[0])
    return int.from_bytes(raw, "big", signed=kind == "i")


def decode(payload):
    """{field: value} for a beacon's payload, an array field as a list, e.g. {"timestamp": 1764000000,
    "eps.voltage": [7.4, 7.4, 7.4, 7.4], ...}. Raises ValueError if the payload is too short."""
    payload = bytes(payload)
    if len(payload) < size():
        raise ValueError(f"telemetry payload of {len(payload)} bytes, the serializer fills {size()}")
    out = {}
    for name, index, offset, width, kind in layout():
        value = _value(payload[offset:offset + width], kind)
        if index is None:
            out[name] = value
        else:
            out.setdefault(name, []).append(value)
    return out


def encode(values):
    """The payload for {field: value} (the inverse of decode(); fields left out are 0). For tests: the satellite's own
    serializer is the real encoder."""
    out = bytearray(ccsds.constants()["TELEMETRY_DOWNLINK_SIZE"])
    for name, index, offset, width, kind in layout():
        value = values.get(name, [0] * (index + 1) if index is not None else 0)
        if index is not None:
            value = value[index]
        if kind == "f":
            raw = struct.pack(">f" if width == 4 else ">d", value)
        else:
            raw = int(value).to_bytes(width, "big", signed=kind == "i")
        out[offset:offset + width] = raw
    return bytes(out)


# ---------- Names ----------

def device_status_name(value):
    names = {v: k for k, v in ccsds.enum("src/telemetry/telemetry.h", "tel_device_status_t").items()}
    return names.get(value, f"unknown {value}")


def opmode_name(value):
    names = {v: k for k, v in ccsds.enum("src/telemetry/telemetry.h", "tel_adcs_opmode_t").items()}
    return names.get(value, f"unknown {value}")


def adcs_flags(value):
    """Names of the TEL_ADCS_FLAG_* bits set in adcs.flags."""
    c = ccsds.constants()
    return [name for name, bit in sorted(c.items(), key=lambda kv: kv[1])
            if name.startswith("TEL_ADCS_FLAG_") and value & bit]


def state_name(value):
    names = {v: k for k, v in firmware.state_ids().items()}
    return names.get(value, f"unknown {value}")


HEALTH_FIELDS = ("gyro.health", "camera.health", "display_health", "uhf_health", "sband.health")


def summary(t):
    """A short, readable version of a decoded beacon, for the event log."""
    health = {f.split(".")[0].replace("_health", ""): device_status_name(t[f]).replace("TEL_DEVICE_STATUS_", "")
              for f in HEALTH_FIELDS if f in t}
    return {
        "time": t.get("timestamp"),
        "boot": t.get("boot_timestamp"),
        "state": state_name(t["state_machine.state_history"][0]) if "state_machine.state_history" in t else None,
        "opmode": opmode_name(t["adcs.opmode"]) if "adcs.opmode" in t else None,
        "adcs_flags": [f.replace("TEL_ADCS_FLAG_", "") for f in adcs_flags(t.get("adcs.flags", 0))],
        "health": health,
        "soc": [round(x, 1) for x in t.get("eps.charge_percent", [])],
        "volts": [round(x, 2) for x in t.get("eps.voltage", [])],
    }
