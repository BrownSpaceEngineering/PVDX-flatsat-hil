"""The ground station's framing: PVDXos's own SPP and USLP code (src/ccsds), built natively for this machine and
called through ctypes, so the ground wraps and unwraps UHF frames exactly as the satellite does.

The library is built from ccsds_host/ (a few stand-ins for FreeRTOS and the HAL, the shim for logging, and
ground_api.c) plus the PVDXos sources under paths.PVDX, and rebuilt whenever any of them changes. The radio code lives
on the radio-task branch: point PVDX_ROOT at a checkout of it. Values PVDXos keeps in C (VC routes, the callsigns)
are read from its sources, never copied, so a change there reaches the ground at once or fails loudly here.
"""

import ctypes
import hashlib
import os
import re
import subprocess
import threading

from paths import HIL_REPO, PVDX

HOST_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ccsds_host")
BUILD_DIR = os.path.join(HIL_REPO, "build", "ccsds_host")
PVDX_SOURCES = ["src/ccsds/uslp.c", "src/ccsds/spp.c", "src/tasks/cfdp/uplink_parse.c"]
HOST_SOURCES = ["shim.c", "ground_api.c"]
INCLUDES = ["src", "src/ccsds", "src/misc/logging", "src/misc/printf", "src/tasks"]
CALLSIGN_SIZE = 6

ERRORS = {1: "bad arguments", 2: "USLP frame rejected", 3: "not an unsegmented frame of space packets",
          4: "space packet rejected", 5: "space packet shorter than its secondary header", 6: "SPP wrap failed",
          7: "USLP wrap failed"}


class FramingError(RuntimeError):
    def __init__(self, code):
        super().__init__(ERRORS.get(code, f"error {code}"))
        self.code = code


class GroundFrame(ctypes.Structure):
    _fields_ = [
        ("vcid", ctypes.c_uint8), ("map_id", ctypes.c_uint8), ("src_or_dest", ctypes.c_uint8),
        ("vc_frame_count", ctypes.c_uint8), ("scid", ctypes.c_uint16), ("apid", ctypes.c_uint16),
        ("packet_type", ctypes.c_uint8), ("secondary_header_flag", ctypes.c_uint8), ("sequence_flags", ctypes.c_uint8),
        ("sequence_count", ctypes.c_uint16), ("timestamp", ctypes.c_uint32), ("boot_timestamp", ctypes.c_uint32),
        ("callsign", ctypes.c_uint8 * CALLSIGN_SIZE), ("data_offset", ctypes.c_uint32), ("data_len", ctypes.c_uint32),
    ]


class Frame:
    """A received UHF frame, unwrapped: USLP and SPP header fields, and the SPP user data."""

    def __init__(self, raw, fields):
        self.raw = raw
        for name, _ in GroundFrame._fields_:
            setattr(self, name, getattr(fields, name))
        self.callsign = bytes(fields.callsign)
        self.data = raw[fields.data_offset:fields.data_offset + fields.data_len]

    def __repr__(self):
        return (f"Frame(vc={self.vcid}, count={self.vc_frame_count}, apid=0x{self.apid:x}, seq={self.sequence_count}, "
                f"{len(self.data)} bytes)")


def _source(path):
    with open(os.path.join(PVDX, path)) as f:
        return f.read()


def _build():
    missing = [p for p in PVDX_SOURCES + ["src/ccsds/radio_routing.h"] if not os.path.exists(os.path.join(PVDX, p))]
    if missing:
        raise FileNotFoundError(f"{PVDX} has no {missing}: the UHF radio code is on the radio-task branch; set "
                                f"PVDX_ROOT to a checkout of it")
    inputs = [os.path.join(PVDX, p) for p in PVDX_SOURCES] + [os.path.join(HOST_DIR, p) for p in HOST_SOURCES]
    headers = []
    for root in (os.path.join(PVDX, "src"), os.path.join(HOST_DIR, "stubs")):
        for dirpath, _, files in os.walk(root):
            headers += [os.path.join(dirpath, f) for f in files if f.endswith(".h")]
    digest = hashlib.sha256()
    for path in sorted(inputs + headers):
        with open(path, "rb") as f:
            digest.update(path.encode() + f.read())
    lib = os.path.join(BUILD_DIR, f"libccsds-{digest.hexdigest()[:16]}.so")
    if not os.path.exists(lib):
        os.makedirs(BUILD_DIR, exist_ok=True)
        cmd = ["cc", "-shared", "-fPIC", "-std=gnu99", "-O1", "-Wall", "-DDEVBUILD", '-DGIT_BRANCH_NAME="host"',
               '-DGIT_COMMIT_HASH="host"', '-D__FILENAME__="host"', f"-I{os.path.join(HOST_DIR, 'stubs')}"]
        cmd += [f"-I{os.path.join(PVDX, i)}" for i in INCLUDES] + inputs + ["-o", lib]
        env = {**os.environ, "TMPDIR": BUILD_DIR}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError(f"building PVDXos's CCSDS code for the host failed:\n{result.stderr}")
    return lib


_lib = None
_lock = threading.Lock()


def library():
    global _lib
    with _lock:
        if _lib is None:
            lib = ctypes.CDLL(_build())
            lib.ground_wrap.argtypes = [ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint8, ctypes.c_uint16, ctypes.c_uint32,
                                        ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint16, ctypes.c_char_p,
                                        ctypes.POINTER(ctypes.c_uint32)]
            lib.ground_unwrap.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(GroundFrame)]
            lib.ground_constant_name.restype = ctypes.c_char_p
            lib.ground_constant_value.restype = ctypes.c_long
            lib.ground_gmap_id.restype = ctypes.c_uint32
            lib.ground_parse_uplink.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint16),
                                                ctypes.c_uint32]
            if lib.ground_frame_size() != ctypes.sizeof(GroundFrame):
                raise RuntimeError("ccsds_host/ground_api.c and GroundFrame in ccsds.py disagree")
            _lib = lib
        return _lib


def constants():
    """{PVDXos macro name: value} for everything ground_api.c exports."""
    lib = library()
    return {lib.ground_constant_name(i).decode(): lib.ground_constant_value(i) for i in range(lib.ground_constant_count())}


def routes():
    """{stream name: (vcid, radio name)} from the radio_routes table in src/tasks/cfdp/radio_routing.c."""
    text = _source("src/tasks/cfdp/radio_routing.c")
    found = {m.group(1): (int(m.group(2)), m.group(3)) for m in
             re.finditer(r"\[(RADIO_STREAM_\w+)\]\s*=\s*\{\s*\.vcid\s*=\s*(\d+)\s*,\s*\.radio\s*=\s*(RADIO_\w+)", text)}
    if len(found) != constants()["RADIO_STREAM_COUNT"]:
        raise RuntimeError(f"read {len(found)} routes from radio_routing.c, expected RADIO_STREAM_COUNT; its format changed")
    return found


def _callsign(name, text):
    match = re.search(name + r"\[[^\]]*\]\s*=\s*\{([^}]*)\}", text)
    if not match:
        raise RuntimeError(f"no {name} in uhf_radio_task.c; its format changed")
    return bytes(ord(c) for c in re.findall(r"'(.)'", match.group(1)))


def ground_callsign():
    """The callsign the satellite accepts uplinks from (ground_callsign in uhf_radio_task.c)."""
    return _callsign("ground_callsign", _source("src/tasks/uhf_radio/uhf_radio_task.c"))


def satellite_callsign():
    """The callsign in the satellite's downlink secondary headers (spp_wrap() in uhf_radio_task.c)."""
    text = _source("src/tasks/uhf_radio/uhf_radio_task.c")
    match = re.search(r"\.callsign\s*=\s*\{([^}]*)\}", text)
    if not match:
        raise RuntimeError("no .callsign in spp_wrap() in uhf_radio_task.c; its format changed")
    return bytes(ord(c) for c in re.findall(r"'(.)'", match.group(1)))


def wrap(vcid, apid, data, sequence_count, packet_type=None, callsign=None, timestamp=0):
    """A ground-to-satellite frame (a telecommand space packet in a USLP frame), as the PSDU to transmit."""
    lib = library()
    c = constants()
    packet_type = c["SPP_PACKET_TYPE_REQUESTING"] if packet_type is None else packet_type
    callsign = ground_callsign() if callsign is None else callsign
    out = ctypes.create_string_buffer(4096)
    out_len = ctypes.c_uint32(len(out))
    code = lib.ground_wrap(vcid, apid, packet_type, sequence_count, timestamp, callsign, bytes(data), len(data), out,
                           ctypes.byref(out_len))
    if code:
        raise FramingError(code)
    return out.raw[:out_len.value]


def unwrap(raw):
    """A received PSDU as a Frame. Raises FramingError at the first layer that rejects it."""
    fields = GroundFrame()
    buf = ctypes.create_string_buffer(bytes(raw), len(raw))
    code = library().ground_unwrap(buf, len(raw), ctypes.byref(fields))
    if code:
        raise FramingError(code)
    return Frame(bytes(raw), fields)


def parse_uplink(data):
    """The command types in an uplink file, as PVDXos's own parser (uplink_packet_parse()) reads it, or None if it
    rejects the file."""
    types = (ctypes.c_uint16 * 64)()
    n = library().ground_parse_uplink(bytes(data), len(data), types, len(types))
    return None if n < 0 else list(types[:n])


def uplink_callsign():
    """UPLINK_CALLSIGN from src/tasks/cfdp/uplink_parse.h: the callsign an uplink file must start with."""
    match = re.search(r'#define\s+UPLINK_CALLSIGN\s+"([^"]*)"', _source("src/tasks/cfdp/uplink_parse.h"))
    if not match:
        raise RuntimeError("no UPLINK_CALLSIGN in uplink_parse.h; its format changed")
    return match.group(1).encode()
