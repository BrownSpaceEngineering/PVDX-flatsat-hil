"""The flatsat (flight computer running PVDXos) through its J-Link: reset it and read its RTT log.

One pylink session on the flatsat's J-Link for the whole test run (two sessions on one probe knock each other off),
with a background thread draining RTT channel 1, the firmware log (lib/rtt.py). With the Grand Central tester a
second J-Link is plugged in; rtt.probe_serials() tells them apart.

This never flashes. Flash the build under test first with `make flash_to_mram` (so the bootloader doesn't replace it
with the MRAM golden copy at the next reset), from the same build whose src/PVDXos.elf is in the tree.
"""

import re
import threading
import time

import pylink  # pip install pylink-square

from rtt import FLATSAT_ELF as ELF
from rtt import RttLink, control_block_address, open_jlink

LOG_CHANNEL = 1
ANSI = re.compile(r"\x1b\[[0-9;]*m")
DROPPED = r"\[!\] (\d+) log lines dropped"  # src/misc/logging/logging.c, when its RTT buffer was full


class LogLine:
    __slots__ = ("time", "text")

    def __init__(self, t, text):
        self.time = t
        self.text = text

    def __repr__(self):
        return f"LogLine({self.time:.3f}, {self.text!r})"


class Flatsat:
    def __init__(self, elf=ELF, serial_number=None):
        self.jlink = open_jlink(serial_number)
        self.lock = threading.Lock()  # pylink is not thread safe
        self.rtt = RttLink(self.jlink, control_block_address(elf), self.lock)
        self.read_lock = threading.Lock()  # Held from reading log data to adding it to self.lines
        self.lines = []
        self.partial = ""
        self.lines_lock = threading.Lock()
        self.new_line = threading.Condition(self.lines_lock)
        self.running = True
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def vdd(self):
        """The flatsat's supply voltage, which the J-Link measures on its VTref pin."""
        with self.lock:
            return self.jlink.hardware_status.voltage / 1000.0

    def close(self):
        self.running = False
        self.reader.join(timeout=2)
        with self.lock:
            self.jlink.close()

    # ---------- RTT ----------

    def _read_loop(self):
        while self.running:
            with self.read_lock:
                try:
                    data = self.rtt.read(LOG_CHANNEL)
                except pylink.errors.JLinkException:
                    data = None
                if data:
                    self._add(ANSI.sub("", data.decode("ascii", "replace")))
            if data is None:
                time.sleep(0.2)
            elif not data:
                time.sleep(0.002)  # The flatsat drops whole log lines when its buffer (SEGGER_RTT_LOG_BUFFER_SIZE) is full

    def _add(self, text):
        now = time.monotonic()
        with self.new_line:
            text = self.partial + text
            *complete, self.partial = text.split("\n")
            for line in complete:
                self.lines.append(LogLine(now, line.rstrip("\r")))
            if complete:
                self.new_line.notify_all()

    # ---------- Log queries ----------

    def mark(self):
        """A position in the log; pass it as `since` to only look at what came after."""
        with self.lines_lock:
            return len(self.lines)

    def log(self, since=0):
        with self.lines_lock:
            return list(self.lines[since:])

    def expect(self, pattern, timeout=30.0, since=0):
        """Waits for a log line matching `pattern` (regex) after `since`; returns its match. Raises TimeoutError."""
        regex = re.compile(pattern)
        end = time.monotonic() + timeout
        index = since
        with self.new_line:
            while True:
                while index < len(self.lines):
                    match = regex.search(self.lines[index].text)
                    if match:
                        return match
                    index += 1
                remaining = end - time.monotonic()
                if remaining <= 0:
                    tail = "\n".join(line.text for line in self.lines[max(since, len(self.lines) - 30):])
                    raise TimeoutError(f"no log line matching {pattern!r} within {timeout} s. Last lines:\n{tail}")
                self.new_line.wait(remaining)

    def collect(self, pattern, duration, since=None):
        """Every match of `pattern` in the log during the next `duration` seconds (or since `since`)."""
        start = self.mark() if since is None else since
        time.sleep(duration)
        regex = re.compile(pattern)
        return [m for m in (regex.search(line.text) for line in self.log(start)) if m]

    def find_all(self, pattern, since=0):
        regex = re.compile(pattern)
        return [m for m in (regex.search(line.text) for line in self.log(since)) if m]

    def dropped_lines(self, since=0):
        """How many log lines the flatsat reported dropping after `since` (its RTT buffer was full, so the log is
        incomplete and a check for a line's absence means nothing)."""
        return sum(int(m.group(1)) for m in self.find_all(DROPPED, since))

    # ---------- Target control ----------

    def reset(self):
        """Resets the board and returns a log mark from just before the new boot."""
        # Halt, then take in what the old boot logged that hasn't been read yet, so the mark is after all of it. The
        # reader stays out until the new boot runs, so none of its text is joined to the old boot's unfinished line
        with self.lock:
            self.jlink.halt()
        with self.read_lock:
            while data := self.rtt.read(LOG_CHANNEL):
                self._add(ANSI.sub("", data.decode("ascii", "replace")))
            if self.partial:
                self._add("\n")  # Ends the old boot's last line, which was cut off
            mark = self.mark()
            with self.lock:
                self.jlink.reset(halt=True)
                self.jlink.restart()
        return mark

    def boot(self, timeout=60.0):
        """Resets and waits until the device checks have finished; returns the log mark of this boot."""
        mark = self.reset()
        self.expect(r"AT_LEAST_ONE_DEVICE_FAILED", timeout=timeout, since=mark)
        return mark

    def device_checks(self, since):
        """{device name: passed} from the startup device check table logged after `since`."""
        return {m.group(1): m.group(2) == "1" for m in self.find_all(r"\b([A-Z_]+_ID)\s*:\s*([01])\s*$", since)}
