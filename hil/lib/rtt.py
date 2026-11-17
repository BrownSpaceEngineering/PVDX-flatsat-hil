"""SEGGER RTT over a J-Link by direct memory access, and telling the flatsat's J-Link from the tester's.

The control block address comes from the `_SEGGER_RTT` symbol in the target's ELF, so a reset never leaves the reader
following a stale block (the J-Link DLL's own RTT search can latch onto the old one). Layout (SEGGER_RTT.h): a 24-byte
header ("SEGGER RTT", MaxNumUpBuffers at +16, MaxNumDownBuffers at +20), then the up buffers, then the down buffers,
24 bytes each: sName, pBuffer, SizeOfBuffer, WrOff, RdOff, Flags.
"""

import functools
import os
import struct
import subprocess
import threading

import pylink  # pip install pylink-square

from paths import HIL_REPO, PVDX

DEVICE = "ATSAMD51P20A"
FLATSAT_ELF = os.path.join(PVDX, "src", "PVDXos.elf")
TESTER_ELF = os.path.join(HIL_REPO, "tester", "tester.elf")
TESTER_LINK_CHANNEL = 1  # tester/src/platform.c
TESTER_CHANNEL_NAME = b"PVDX tester"


def control_block_address(elf):
    out = subprocess.run(["arm-none-eabi-nm", elf], capture_output=True, text=True, check=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[2] == "_SEGGER_RTT":
            return int(parts[0], 16)
    raise RuntimeError(f"_SEGGER_RTT not found in {elf}")


def open_jlink(serial_number=None):
    # Each probe gets its own copy of the J-Link library: on macOS and Linux pylink otherwise loads one shared copy,
    # which holds a single connection, so the flatsat's and the tester's probes couldn't be open at once
    jlink = pylink.JLink(lib=pylink.Library(use_tmpcpy=True))
    jlink.open(serial_no=serial_number) if serial_number else jlink.open()
    jlink.set_tif(pylink.enums.JLinkInterfaces.SWD)
    jlink.set_speed(4000)
    jlink.connect(DEVICE)
    return jlink


class RttLink:
    def __init__(self, jlink, control_block, lock=None):
        self.jlink = jlink
        self.cb = control_block
        self.lock = lock or threading.Lock()  # pylink is not thread safe

    def _header(self):
        header = self._read_bytes(self.cb, 24)
        if header[:10] != b"SEGGER RTT":
            return None  # Not set up yet after a reset
        return struct.unpack_from("<II", header, 16)  # (up buffers, down buffers)

    def _read_bytes(self, address, count):
        """count bytes of target memory, read as aligned 32-bit words. The target updates each buffer offset with one
        32-bit store, which a byte-wise read can catch halfway (a torn offset makes a whole buffer of old data look new,
        or skips data); word reads are also about 4x faster."""
        start = address & ~3
        words = (address + count - start + 3) // 4
        data = struct.pack(f"<{words}I", *self.jlink.memory_read32(start, words))
        return data[address - start:address - start + count]

    def _descriptor(self, offset):
        return struct.unpack("<IIIIII", self._read_bytes(self.cb + offset, 24))

    def read(self, channel):
        """Everything waiting in up buffer `channel` (target to host); b"" if none or not set up."""
        with self.lock:
            counts = self._header()
            if counts is None or channel >= counts[0]:
                return b""
            offset = 24 + 24 * channel
            _, buf, size, wr, rd, _ = self._descriptor(offset)
            if not (0 < size <= 1 << 20 and wr < size and rd < size) or wr == rd:
                return b""
            n = (wr - rd) % size
            first = min(n, size - rd)
            data = self._read_bytes(buf + rd, first)
            if n > first:
                data += self._read_bytes(buf, n - first)
            self.jlink.memory_write32(self.cb + offset + 16, [wr])
            return data

    def write(self, channel, data):
        """Puts as much of `data` as fits into down buffer `channel` (host to target); returns how many bytes."""
        with self.lock:
            counts = self._header()
            if counts is None or channel >= counts[1]:
                return 0
            offset = 24 + 24 * counts[0] + 24 * channel
            _, buf, size, wr, rd, _ = self._descriptor(offset)
            if not (0 < size <= 1 << 20 and wr < size and rd < size):
                return 0
            n = min(len(data), (rd - wr - 1) % size)
            first = min(n, size - wr)
            if first:
                self.jlink.memory_write8(buf + wr, list(data[:first]))
            if n > first:
                self.jlink.memory_write8(buf, list(data[first:n]))
            self.jlink.memory_write32(self.cb + offset + 12, [(wr + n) % size])
            return n

    def channel_name(self, channel):
        with self.lock:
            counts = self._header()
            if counts is None or channel >= counts[0]:
                return None
            name_ptr = self._descriptor(24 + 24 * channel)[0]
            if not 0x20000000 <= name_ptr < 0x20040000 and not name_ptr < 0x00100000:
                return None
            return bytes(self.jlink.memory_read8(name_ptr, 32)).split(b"\0")[0]


class ProbeError(RuntimeError):
    pass


def _env_serial(name):
    value = os.environ.get(name)
    return int(value) if value else None


def _runs_tester(serial):
    """Whether the target on this J-Link has the tester's RTT channel (False without a built tester.elf)."""
    if not os.path.exists(TESTER_ELF):
        return False
    jlink = open_jlink(serial)
    try:
        return RttLink(jlink, control_block_address(TESTER_ELF)).channel_name(TESTER_LINK_CHANNEL) == TESTER_CHANNEL_NAME
    finally:
        jlink.close()


@functools.cache  # Identifying the probes connects to each; once per test run is enough
def probe_serials():
    """(flatsat J-Link serial or None, tester J-Link serial or None).

    HIL_FLATSAT_JLINK and HIL_TESTER_JLINK set them. Otherwise the tester's is the one whose target has the tester's
    RTT channel, so with one J-Link it is the tester's (flatsat None) or the flatsat's (tester None).
    """
    flatsat, tester = _env_serial("HIL_FLATSAT_JLINK"), _env_serial("HIL_TESTER_JLINK")
    if flatsat and tester:
        return flatsat, tester
    serials = [e.SerialNumber for e in pylink.JLink().connected_emulators()]
    if not serials:
        raise ProbeError("no J-Link connected")
    if len(serials) == 1:
        if tester == serials[0] or (not flatsat and _runs_tester(serials[0])):
            return None, serials[0]
        return serials[0], None
    if flatsat or tester:
        others = [s for s in serials if s not in (flatsat, tester)]
        if len(others) != 1:
            raise ProbeError(f"J-Links {serials}: set both HIL_FLATSAT_JLINK and HIL_TESTER_JLINK")
        return (flatsat, others[0]) if flatsat else (others[0], tester)
    if not os.path.exists(TESTER_ELF):
        raise ProbeError(f"two J-Links but no {TESTER_ELF} to identify the tester by; build it or set the env vars")
    testers = [s for s in serials if _runs_tester(s)]
    if len(testers) != 1 or len(serials) != 2:
        raise ProbeError(f"J-Links {serials}, tester found on {testers}: set HIL_FLATSAT_JLINK and HIL_TESTER_JLINK")
    return next(s for s in serials if s != testers[0]), testers[0]
