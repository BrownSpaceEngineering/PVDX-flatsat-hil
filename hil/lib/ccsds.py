"""The ground station's view of PVDXos: its own SPP, USLP, CFDP PDU, uplink parser and telemetry serializer code, built
natively for this machine and called through ctypes, so the ground wraps, unwraps and decodes exactly as the satellite
does.

The library is built from ccsds_host/ (stand-ins for FreeRTOS and the HAL, the shim for logging, and ground_api.c) plus
the PVDXos sources under paths.PVDX (PVDX_ROOT), and rebuilt whenever any of them changes. Values PVDXos keeps in C are
never copied here:
- Integer macros in the radio headers (HEADER_MACROS) and the members of the enums in ENUMS are exported by a table
  generated from the headers at build time, so the compiler checks every name and value.
- The route table and the callsigns, which live in .c files, are read with regexes that fail loudly when their format
  changes.
Point PVDX_ROOT at the checkout the flatsat's build came from (the radio code differs between main and radio-task).
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
PVDX_SOURCES = ["src/ccsds/uslp.c", "src/ccsds/spp.c", "src/tasks/cfdp/uplink_parse.c", "src/telemetry/telemetry.c"]
# cfdp_pdu.c also holds the senders, which need the whole CFDP task: only its parsers (PARSER_FUNCTIONS) are built, taken
# from it verbatim into a generated file that includes nothing but cfdp_pdu.h
CFDP_PDU_SOURCE = "src/tasks/cfdp/cfdp_pdu.c"
PARSER_FUNCTIONS = re.compile(r"^(?:int|void)\s+(cfdp_pdu_\w+_parse|cfdp_data_view_clear_data)\s*\(")
HOST_SOURCES = ["shim.c", "ground_api.c"]
INCLUDES = ["src", "src/ccsds", "src/misc/logging", "src/misc/printf", "src/misc/utc", "src/tasks", "src/tasks/cfdp",
            "src/telemetry"]
CALLSIGN_SIZE = 6

# Headers whose integer macros are exported (a macro whose value isn't a plain integer expression is skipped)
HEADER_MACROS = ["src/ccsds/ccsds.h", "src/ccsds/spp.h", "src/ccsds/radio_routing.h", "src/tasks/uhf_radio/pusp.h",
                 "src/tasks/uhf_radio/uhf_radio_task.h", "src/tasks/cfdp/uplink_parse.h", "src/tasks/cfdp/cfdp_pdu.h",
                 "src/tasks/cfdp/cfdp_task.h", "src/telemetry/telemetry.h"]
# (header, enum name): every member is exported
ENUMS = [("src/tasks/cfdp/uplink_parse.h", "uplink_cmd_type_e"), ("src/tasks/uhf_radio/pusp.h", "pusp_directive"),
         ("src/telemetry/telemetry.h", "tel_device_status_t"), ("src/telemetry/telemetry.h", "tel_adcs_opmode_t"),
         ("src/ccsds/radio_routing.h", "radio_stream_t"), ("src/globals.h", "device_id_t"),
         ("src/tasks/uhf_radio/uhf_radio_task.h", "uhf_state"), ("src/globals.h", "operation_t")]

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


def _strip_comments(text):
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


_INT_EXPR = re.compile(r"^[\s()0-9A-Za-z_xX<>|&+\-*/~]+$")


def _macros(header):
    """Names of the object-like macros in a header whose values look like integer expressions: numbers (with u/l
    suffixes), operators and other upper-case names. The compiler has the final say: each is checked with #ifdef and
    built into the table, so a wrong guess here fails the build rather than giving a wrong value."""
    names = []
    for m in re.finditer(r"^\s*#\s*define\s+([A-Z][A-Z0-9_]*)[ \t]+([^\n]+)$", _strip_comments(_source(header)), re.M):
        name, value = m.group(1), m.group(2).strip()
        words = re.findall(r"[A-Za-z_]\w*", value)
        if not _INT_EXPR.match(value) or any(not w.isupper() and not re.fullmatch(r"[0-9]*[xX]?[0-9a-fA-FuUlL]*", w)
                                             for w in words):
            continue
        if name.endswith("_H"):  # Include guards
            continue
        names.append(name)
    return names


def _enum_members(header, enum):
    """Member names of `typedef enum [tag] {...} enum;` or `enum enum {...}` in a header."""
    text = _strip_comments(_source(header))
    match = (re.search(r"typedef\s+enum\s*\w*\s*\{([^}]*)\}\s*" + enum + r"\s*;", text)
             or re.search(r"enum\s+" + enum + r"\s*\{([^}]*)\}", text))
    if not match:
        raise RuntimeError(f"no enum {enum} in {header}; its format changed")
    return [m.group(1) for m in re.finditer(r"(?:^|,)\s*([A-Za-z_]\w*)\s*(?:=[^,]*)?(?=,|$)", match.group(1).strip())]


def _generated_table():
    """C source for the constant table: every exported macro (guarded by #ifdef) and enum member."""
    seen = set()
    lines = []
    for header in HEADER_MACROS:
        if not os.path.exists(os.path.join(PVDX, header)):
            continue
        for name in _macros(header):
            if name not in seen:
                seen.add(name)
                lines.append(f'#ifdef {name}\n    {{"{name}", (long)({name})}},\n#endif')
    for header, enum in ENUMS:
        if not os.path.exists(os.path.join(PVDX, header)):
            raise FileNotFoundError(f"{PVDX} has no {header}")
        for name in _enum_members(header, enum):
            if name not in seen:
                seen.add(name)
                lines.append(f'    {{"{name}", (long)({name})}},')
    return "\n".join(lines) + "\n"


def _cfdp_parsers():
    """The CFDP PDU parsers from cfdp_pdu.c, verbatim, as a source file of their own."""
    lines = _source(CFDP_PDU_SOURCE).splitlines(keepends=True)
    out = ['#include <string.h>\n#include "cfdp_pdu.h"\n']
    found = []
    i = 0
    while i < len(lines):
        match = PARSER_FUNCTIONS.match(lines[i])
        if not match:
            i += 1
            continue
        found.append(match.group(1))
        depth, started = 0, False
        while i < len(lines):
            out.append(lines[i])
            depth += lines[i].count("{") - lines[i].count("}")
            started = started or "{" in lines[i]
            i += 1
            if started and depth == 0:
                break
    if "cfdp_pdu_header_parse" not in found or len(found) < 5:
        raise RuntimeError(f"found only {found} in {CFDP_PDU_SOURCE}; its format changed")
    return "".join(out)


def _build():
    missing = [p for p in PVDX_SOURCES + [CFDP_PDU_SOURCE, "src/ccsds/radio_routing.h"] if not os.path.exists(os.path.join(PVDX, p))]
    if missing:
        raise FileNotFoundError(f"{PVDX} has no {missing}: PVDX_ROOT must be a PVDXos checkout with the UHF radio task")
    table = _generated_table()
    parsers = _cfdp_parsers()
    inputs = [os.path.join(PVDX, p) for p in PVDX_SOURCES] + [os.path.join(HOST_DIR, p) for p in HOST_SOURCES]
    headers = []
    for root in (os.path.join(PVDX, "src"), os.path.join(HOST_DIR, "stubs")):
        for dirpath, _, files in os.walk(root):
            headers += [os.path.join(dirpath, f) for f in files if f.endswith(".h")]
    digest = hashlib.sha256((table + parsers).encode())
    for path in sorted(inputs + headers):
        with open(path, "rb") as f:
            digest.update(path.encode() + f.read())
    tag = digest.hexdigest()[:16]
    lib = os.path.join(BUILD_DIR, f"libccsds-{tag}.so")
    if not os.path.exists(lib):
        gen_dir = os.path.join(BUILD_DIR, f"gen-{tag}")
        os.makedirs(gen_dir, exist_ok=True)
        with open(os.path.join(gen_dir, "ground_constants.inc"), "w") as f:
            f.write(table)
        with open(os.path.join(gen_dir, "cfdp_parsers.c"), "w") as f:
            f.write(parsers)
        cmd = ["cc", "-shared", "-fPIC", "-std=gnu11", "-O1", "-Wall", "-Wno-unused-function", "-DDEVBUILD",
               '-DGIT_BRANCH_NAME="host"', '-DGIT_COMMIT_HASH="host"', '-D__FILENAME__="host"',
               f"-I{gen_dir}", f"-I{os.path.join(HOST_DIR, 'stubs')}"]
        cmd += [f"-I{os.path.join(PVDX, i)}" for i in INCLUDES]
        cmd += inputs + [os.path.join(gen_dir, "cfdp_parsers.c"), "-o", lib + ".tmp"]
        env = {**os.environ, "TMPDIR": BUILD_DIR}
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode:
            raise RuntimeError(f"building PVDXos's radio code for the host failed:\n{result.stderr}")
        os.replace(lib + ".tmp", lib)
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
            lib.ground_telemetry_field_name.restype = ctypes.c_char_p
            lib.ground_cfdp_parse.argtypes = [ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_long),
                                              ctypes.c_uint32]
            if lib.ground_frame_size() != ctypes.sizeof(GroundFrame):
                raise RuntimeError("ccsds_host/ground_api.c and GroundFrame in ccsds.py disagree")
            _lib = lib
        return _lib


_constants = None


def constants():
    """{PVDXos macro or enum member name: value} for everything ground_api.c exports."""
    global _constants
    if _constants is None:
        lib = library()
        _constants = {lib.ground_constant_name(i).decode(): lib.ground_constant_value(i)
                      for i in range(lib.ground_constant_count())}
    return _constants


def enum(header, name):
    """{member: value} of one of the enums in ENUMS, in declaration order."""
    c = constants()
    return {m: c[m] for m in _enum_members(header, name)}


def header_int(header, name):
    """An integer macro from a PVDXos header the host library doesn't build (e.g. PEROVSKITE_STEP_COUNT), read as
    text: only a plain number (with u/l suffixes) is accepted, anything else raises."""
    match = re.search(r"^\s*#\s*define\s+" + name + r"\s+\(?\s*(0[xX][0-9a-fA-F]+|\d+)[uUlL]*\s*\)?\s*(?://.*)?$",
                      _source(header), re.M)
    if not match:
        raise RuntimeError(f"no plain integer #define {name} in {header}; its format changed")
    return int(match.group(1), 0)


def routes():
    """{stream name: vcid} from the radio_routes table in src/tasks/cfdp/radio_routing.c."""
    text = _strip_comments(_source("src/tasks/cfdp/radio_routing.c"))
    found = {m.group(1): int(m.group(2)) for m in
             re.finditer(r"\[(RADIO_STREAM_\w+)\]\s*=\s*\{\s*\.vcid\s*=\s*(\d+)", text)}
    if len(found) != constants()["RADIO_STREAM_COUNT"]:
        raise RuntimeError(f"read {len(found)} routes from radio_routing.c, expected RADIO_STREAM_COUNT; its format changed")
    return found


def _callsign_literal(match):
    return bytes(ord(c) for c in re.findall(r"'(.)'", match.group(1)))


def ground_callsign():
    """The callsign the satellite accepts uplinks from (ground_callsign in uhf_radio_task.c)."""
    match = re.search(r"ground_callsign\[[^\]]*\]\s*=\s*\{([^}]*)\}", _source("src/tasks/uhf_radio/uhf_radio_task.c"))
    if not match:
        raise RuntimeError("no ground_callsign in uhf_radio_task.c; its format changed")
    return _callsign_literal(match)


def satellite_callsign():
    """The callsign in the satellite's downlink secondary headers (spp_wrap())."""
    for path in ("src/tasks/cfdp/radio_routing.c", "src/tasks/uhf_radio/uhf_radio_task.c"):
        match = re.search(r"\.callsign\s*=\s*\{([^}]*)\}", _source(path))
        if match:
            return _callsign_literal(match)
    raise RuntimeError("no .callsign in spp_wrap(); its format changed")


def wrap(vcid, apid, data, sequence_count, packet_type=None, callsign=None, timestamp=0):
    """A ground-to-satellite frame (a telecommand space packet in a USLP frame), as the PSDU to transmit. The satellite
    sets its clock from `timestamp` (uhf_rx_classify()), so the ground sends the real Unix time."""
    lib = library()
    c = constants()
    packet_type = c["SPP_PACKET_TYPE_REQUESTING"] if packet_type is None else packet_type
    callsign = ground_callsign() if callsign is None else callsign
    out = ctypes.create_string_buffer(4096)
    out_len = ctypes.c_uint32(len(out))
    code = lib.ground_wrap(vcid, apid, packet_type, sequence_count, int(timestamp), callsign, bytes(data), len(data), out,
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


# ---------- CFDP PDUs, through PVDXos's parsers (cfdp_pdu.c) ----------

CFDP_FIELDS = ["error", "pdu_type", "direction", "transmission_mode", "crc", "largefile", "pdu_data_length",
               "entity_id_len", "source_entity_id", "transaction_seq", "dest_entity_id", "header_len", "directive",
               # Directive fields: ACK, FIN, NAK, EOF, Metadata, File Data
               "ack_directive", "ack_subtype", "condition_code", "transaction_status", "delivery_code", "file_status",
               "start_of_scope", "end_of_scope", "segments", "checksum", "filesize", "checksum_type", "closure_req",
               "file_length", "offset", "data_len", "data_offset"]
CFDP_SEGMENTS_AT = len(CFDP_FIELDS)  # Segment requests (start, end) follow the fields
CFDP_MAX_SEGMENTS = 32


def parse_cfdp(pdu):
    """A CFDP PDU as PVDXos's parsers read it: {field: value} (CFDP_FIELDS), plus "segment_requests" for a NAK.
    "error" is non-zero if a parser rejected it: 1 the header, 2 the PDU body."""
    out = (ctypes.c_long * (CFDP_SEGMENTS_AT + 2 * CFDP_MAX_SEGMENTS))()
    library().ground_cfdp_parse(bytes(pdu), len(pdu), out, len(out))
    fields = dict(zip(CFDP_FIELDS, out[:CFDP_SEGMENTS_AT]))
    n = min(fields["segments"], CFDP_MAX_SEGMENTS)
    fields["segment_requests"] = [(out[CFDP_SEGMENTS_AT + 2 * i], out[CFDP_SEGMENTS_AT + 2 * i + 1]) for i in range(n)]
    if fields["data_len"]:
        start = fields["data_offset"]
        fields["data"] = bytes(pdu[start:start + fields["data_len"]])
    return fields


# ---------- Telemetry layout, learned from PVDXos's serializer (telemetry.c) ----------

class TelemetryElement(ctypes.Structure):
    _fields_ = [("field", ctypes.c_uint16), ("index", ctypes.c_uint16), ("offset", ctypes.c_uint16),
                ("width", ctypes.c_uint8), ("kind", ctypes.c_char), ("big_endian", ctypes.c_uint8)]


def telemetry_layout():
    """[(field name, element index or None, offset, width, kind)] for every value in a serialized downlink, in
    order, where kind is 'u' (unsigned), 'i' (signed), 'f' (float), 'b' (bool) or 'e' (enum, sent as one byte).
    Learned by serializing a downlink with one value set at a time (ground_telemetry_probe()). Raises if the layout
    can't be learned: a value not sent big-endian, values overlapping, or bytes of the downlink that no known field
    covers (a field was added to tel_downlink_t without an entry in ground_api.c's table)."""
    lib = library()
    elements = (TelemetryElement * 512)()
    covered_to = ctypes.c_int(0)
    n = lib.ground_telemetry_probe(elements, len(elements), ctypes.byref(covered_to))
    if n < 0:
        raise RuntimeError(f"probing PVDXos's telemetry serializer failed (code {n})")
    layout = []
    for e in elements[:n]:
        name = lib.ground_telemetry_field_name(e.field).decode()
        count = lib.ground_telemetry_field_count(e.field)
        if not e.big_endian:
            raise RuntimeError(f"telemetry field {name} isn't sent big-endian as the ground assumes")
        layout.append((name, e.index if count > 1 else None, e.offset, e.width, e.kind.decode()))
    layout.sort(key=lambda x: x[2])
    end = 0
    for name, index, offset, width, _ in layout:
        if offset < end:
            raise RuntimeError(f"telemetry field {name}[{index}] overlaps the previous one")
        if offset > end:
            raise RuntimeError(f"bytes {end}..{offset - 1} of the downlink aren't any known field: tel_downlink_t has a "
                               f"field that ground_api.c's TEL_FIELDS doesn't list")
        end = offset + width
    if end != covered_to.value:
        raise RuntimeError(f"the downlink's fields end at byte {covered_to.value}, but the known ones end at {end}: "
                           f"tel_downlink_t has a field that ground_api.c's TEL_FIELDS doesn't list")
    return layout


# ---------- The radio task's PHY settings (src/drivers/at86rf215/at86_pvdx.c) ----------

def phy_config():
    """The baseband settings of the config the radio task uses (UHF_PHY_CONFIG), as {setting: value as written} from
    its case in at86_init_pvdx()'s switch, e.g. {"fsk.fecs": "AT86RF215_FSK_FEC_RSC", "fsk.sfd1": "0x72F6"}. Settings
    the case doesn't write are 0 (bb_conf is cleared first)."""
    header = _strip_comments(_source("src/drivers/at86rf215/at86_pvdx.h"))
    value = constants()["UHF_PHY_CONFIG"]
    names = {int(m.group(2), 0): m.group(1) for m in re.finditer(r"(AT86_\w+)\s*=\s*(\w+)", header)}
    if value not in names:
        raise RuntimeError(f"UHF_PHY_CONFIG is {value}, which at86_config_t in at86_pvdx.h doesn't name")
    text = _strip_comments(_source("src/drivers/at86rf215/at86_pvdx.c"))
    match = re.search(r"case\s+" + names[value] + r"\s*:(.*?)\bbreak\s*;", text, re.S)
    if not match:
        raise RuntimeError(f"no case {names[value]} in at86_pvdx.c; its format changed")
    if "memset(&bb_conf, 0" not in text:
        raise RuntimeError("at86_pvdx.c no longer clears bb_conf, so settings it leaves out aren't 0")
    settings = {m.group(1): m.group(2).strip() for m in
                re.finditer(r"bb_conf\.([\w.]+)\s*=\s*([^;]+);", match.group(1))}
    settings["config"] = names[value]
    return settings
