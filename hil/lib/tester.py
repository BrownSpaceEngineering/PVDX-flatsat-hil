"""Client for the flatsat tester (tester/, Metro M4 Grand Central), over SEGGER RTT through the tester's own J-Link.

Every command is one line; every reply is one JSON object with "ok". The commands:

    ping                               firmware name, protocol version, uptime
    arm / disarm                       connect / disconnect (high impedance) every analog output
    out get                            setpoints in mV, armed state, full scale
    out set <name|all> <volts>         name is phd0..phd17 or therm0..therm3 (those this tester drives)
    out fullscale <volts>              output voltage at 100% (the tester measures or assumes it)
    pixel get                          [isc_ua, voc_mv] for each of the 16 perovskite pixels
    pixel set <0-15|all> <isc_ua> <voc_mv>
    pmb get                            MCP23017 / INA226 emulator state, PVD_CRNT voltage
    pmb log <from>                     CURRENT register reads the flatsat made: [selected mask, follower mV, nA]
    stxg2 get                          S-band emulator state and SPI statistics
    fault <device> <mode>              stxg2 none|nack|bad_id|hold_bus, ina226 none|nack|no_cvrf|bad_id,
                                       mcp23017 none|nack|stuck, pmb_bus none|hold_bus
    reset                              emulators to power-on, faults cleared, outputs 0 V, pixels dark
    selftest analog|i2c|spi            see tester/src/selftest.c for the jumpers each needs
    bootloader                         reboot into the UF2 bootloader (Grand Central only)

The tester disconnects its outputs after 10 s without a command; this client pings it every few seconds while open.
"""

import json
import threading
import time

import rtt

PHOTODIODE_COUNT = 18
THERMISTOR_COUNT = 4
PIXEL_COUNT = 16
KEEPALIVE_S = 3.0
LINK_CHANNEL = 0  # RTT channel of the Grand Central tester's command link


class TesterError(RuntimeError):
    pass


class RttTransport:
    def __init__(self, serial_number, elf=rtt.TESTER_ELF):
        self.jlink = rtt.open_jlink(serial_number)
        self.link = rtt.RttLink(self.jlink, rtt.control_block_address(elf))

    def write(self, data, timeout=2.0):
        end = time.monotonic() + timeout
        while data:
            n = self.link.write(LINK_CHANNEL, data)
            data = data[n:]
            if data:
                if time.monotonic() > end:
                    raise TesterError("the tester isn't reading its RTT link (running? tester.elf matches the flash?)")
                time.sleep(0.005)

    def read(self):
        data = self.link.read(LINK_CHANNEL)
        if not data:
            time.sleep(0.005)
        return data

    def close(self):
        with self.link.lock:
            self.jlink.close()


def open_transport():
    """The link to the tester through its J-Link, or None if there is no tester."""
    try:
        _, tester_serial = rtt.probe_serials()
    except rtt.ProbeError:
        return None
    return RttTransport(tester_serial) if tester_serial else None


class Tester:
    def __init__(self, transport=None):
        self.transport = transport or open_transport()
        if self.transport is None:
            raise TesterError("no tester found: no second J-Link with the tester on it")
        self.lock = threading.Lock()
        self.pending = b""
        self.last_command = time.monotonic()
        self.running = True
        self.firmware = self.ping()["firmware"]
        self.driven = set(self.outputs()["volts"])  # Analog outputs this tester has
        self.keepalive = threading.Thread(target=self._keepalive, daemon=True)
        self.keepalive.start()

    def close(self):
        self.running = False
        self.keepalive.join(timeout=KEEPALIVE_S + 1)
        self.transport.close()

    def _keepalive(self):
        while self.running:
            time.sleep(0.5)
            if self.running and time.monotonic() - self.last_command > KEEPALIVE_S:
                try:
                    self.command("ping")
                except Exception:  # The test using the tester reports any real link problem
                    pass

    def _readline(self, timeout):
        end = time.monotonic() + timeout
        while b"\n" not in self.pending:
            if time.monotonic() > end:
                return None
            self.pending += self.transport.read()
        line, self.pending = self.pending.split(b"\n", 1)
        return line

    def command(self, text, timeout=5.0):
        """Sends one command and returns its reply; raises TesterError if the reply has ok = false."""
        with self.lock:
            self.last_command = time.monotonic()
            self.transport.write((text + "\n").encode())
            line = self._readline(timeout)
        if line is None:
            raise TesterError(f"no reply to '{text}'")
        reply = json.loads(line)
        if not reply.get("ok"):
            raise TesterError(f"'{text}': {reply.get('error')}")
        return reply

    # ---------- Convenience wrappers ----------

    def ping(self):
        return self.command("ping")

    def arm(self):
        self.command("arm")

    def disarm(self):
        self.command("disarm")

    def reset(self):
        self.command("reset")

    def set_output(self, name, volts):
        self.command(f"out set {name} {volts:.4f}")

    def drives(self, name):
        return name in self.driven

    def set_photodiodes(self, volts):
        """volts: 18 values, PHD_0 first. Returns what the flatsat actually gets: 0 V on any this tester doesn't drive."""
        assert len(volts) == PHOTODIODE_COUNT
        applied = []
        for i, v in enumerate(volts):
            if self.drives(f"phd{i}"):
                self.set_output(f"phd{i}", v)
                applied.append(v)
            else:
                applied.append(0.0)
        return applied

    def set_thermistors(self, volts):
        assert len(volts) == THERMISTOR_COUNT
        for i, v in enumerate(volts):
            self.set_output(f"therm{i}", v)

    def outputs(self):
        return self.command("out get")

    def set_pixel(self, pixel, isc_ua, voc_mv):
        self.command(f"pixel set {pixel} {isc_ua:.3f} {voc_mv:.3f}")

    def pmb(self):
        return self.command("pmb get")

    def pmb_log(self, start=0):
        """Every CURRENT register read since `start` (log index): list of (selected mask, follower mV, current nA)."""
        entries = []
        index = start
        while True:
            reply = self.command(f"pmb log {index}")
            entries += [tuple(e) for e in reply["entries"]]
            if reply["next"] >= reply["total"] or not reply["entries"]:
                return entries, reply["next"]
            index = reply["next"]

    def stxg2(self):
        return self.command("stxg2 get")

    def fault(self, device, mode):
        self.command(f"fault {device} {mode}")

    def selftest(self, which):
        return self.command(f"selftest {which}", timeout=30.0)

    def enter_bootloader(self):
        """Reboots into the UF2 bootloader (Grand Central); the link goes away."""
        try:
            self.command("bootloader")
        finally:
            self.close()
