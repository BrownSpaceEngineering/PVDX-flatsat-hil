"""Uplink command files: what the ground sends the satellite over CFDP to run commands (src/tasks/cfdp/uplink_parse.h).

    preamble: callsign (6) | message_size (4) | n_cmds (2) | timestamp (4)
    command:  type (2) | data size (4) | data

Big-endian throughout. Command type numbers and the callsign are read from PVDXos (ccsds.py), and tests check each
file this builds against PVDXos's own parser.
"""

import struct

import ccsds


def command(name, data=b""):
    """One command: its PVDXos type name (e.g. "SET_UNIX_TIME") and its data bytes."""
    return ccsds.constants()[name], bytes(data)


def set_unix_time(t):
    return command("SET_UNIX_TIME", struct.pack(">I", int(t)))


def reboot(kind=0):
    return command("REBOOT", struct.pack(">B", kind))


def enable_disable_device(device_id):
    return command("ENABLE_DISABLE_DEVICE", struct.pack(">H", device_id))


def build(commands, timestamp):
    """The uplink file for a list of command() tuples."""
    body = b"".join(struct.pack(">HI", kind, len(data)) + data for kind, data in commands)
    return ccsds.uplink_callsign() + struct.pack(">IHI", len(body), len(commands), int(timestamp)) + body
