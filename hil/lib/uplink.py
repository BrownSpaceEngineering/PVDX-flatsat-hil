"""Uplink command files: what the ground sends the satellite over CFDP to run commands (src/tasks/cfdp/uplink_parse.h).

    preamble: callsign (6) | message_size (4) | n_cmds (2) | timestamp (4)
    command:  type (2) | data size (4) | data

Big-endian throughout, floats as their IEEE 754 bits. Command type numbers, the callsign and the sizes that differ
between PVDXos branches are read from PVDXos (ccsds.py), and tests check each command built here against PVDXos's own
parser.
"""

import struct

import ccsds

JD_UNIX_EPOCH = 2440587.5  # Julian date of 1970-01-01T00:00:00Z


def command(name, data=b""):
    """One command: its PVDXos type name (e.g. "SET_UNIX_TIME") and its data bytes."""
    return ccsds.constants()[name], bytes(data)


def enable_disable_device(device_id, enabled):
    """device_id is a device_id_t (src/globals.h); enabled is 1 to enable, 0 to disable."""
    return command("ENABLE_DISABLE_DEVICE", struct.pack(">HB", device_id, 1 if enabled else 0))


def update_display(bitmap):
    """A whole display image: UPLINK_DISPLAY_BITMAP_SIZE bytes (256 x 64 pixels, 4 bits each)."""
    size = ccsds.constants()["UPLINK_DISPLAY_BITMAP_SIZE"]
    if len(bitmap) != size:
        raise ValueError(f"a display bitmap is {size} bytes, not {len(bitmap)}")
    return command("UPDATE_DISPLAY", bitmap)


def take_iv_curve(steps):
    """Sets the sampling resolution (voltage steps per pixel) of the perovskite sweeps that follow."""
    return command("TAKE_IV_CURVE", struct.pack(">I", steps))


def send_iv_curve():
    return command("SEND_IV_CURVE", b"\0")  # Its byte has no meaning


def transmit_on_off(on):
    return command("TRANSMIT_ON_OFF", struct.pack(">B", 1 if on else 0))


def reboot(flag=1):
    """Any non-zero flag reboots the satellite once the pass ends."""
    return command("REBOOT", struct.pack(">B", flag))


def take_photo(unix_time):
    """Takes the student picture at this Unix time (0 for as soon as possible)."""
    return command("TAKE_PHOTO", struct.pack(">I", int(unix_time)))


def send_photo(count=1):
    return command("SEND_PHOTO", struct.pack(">i", count))


def set_kepler_coeff(coefficients):
    n = ccsds.constants()["UPLINK_KEPLER_COEFFICIENTS"]
    if len(coefficients) != n:
        raise ValueError(f"{n} Kepler coefficients, not {len(coefficients)}")
    return command("SET_KEPLER_COEFF", struct.pack(f">{n}f", *coefficients))


def set_unix_time(t):
    return command("SET_UNIX_TIME", struct.pack(">I", int(t)))


def set_adcs_op_mode(mode):
    """mode is a tel_adcs_opmode_t, e.g. ccsds.constants()["TEL_ADCS_OPMODE_DETUMBLE_ONLY"]."""
    return command("SET_ADCS_OP_MODE", struct.pack(">H", mode))


def adcs_update_params(epoch_jd, inclination, raan, eccentricity, argument_of_perigee, mean_anomaly, mean_motion, bstar,
                       device_rotation=(0, 0, 0), flags=0):
    """A TLE's mean elements (uplink_adcs_parameters_t), then, where PVDXos has them (UPLINK_ADCS_PARAMETERS_SIZE), the
    device rotation bytes and the flags byte."""
    data = struct.pack(">d7f", epoch_jd, inclination, raan, eccentricity, argument_of_perigee, mean_anomaly, mean_motion,
                       bstar)
    size = ccsds.constants().get("UPLINK_ADCS_PARAMETERS_SIZE")
    if size is not None:
        data += struct.pack(">3bB", *device_rotation, flags)
        if len(data) != size:
            raise ValueError(f"ADCS parameters are {size} bytes in PVDXos, {len(data)} here: its layout changed")
    return command("ADCS_UPDATE_PARAMS", data)


def set_power_mode(mode):
    return command("SET_PWR_MODE", struct.pack(">H", mode))


def build(commands, timestamp):
    """The uplink file for a list of command() tuples."""
    body = b"".join(struct.pack(">HI", kind, len(data)) + data for kind, data in commands)
    return ccsds.uplink_callsign() + struct.pack(">IHI", len(body), len(commands), int(timestamp)) + body
