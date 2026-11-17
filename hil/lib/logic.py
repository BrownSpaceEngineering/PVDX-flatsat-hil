"""The flatsat's digital outputs, recorded by the tester (tester/src/watch.c) in place of a logic analyzer.

Signal names: mtq_{x,y,z}_in{1,2}, mtq_{x,y,z}_slp, heater1, heater2, burn, sband_en, sband_rst. All are wired at once
(hil/README.md). While recording, the tester sends one 8-byte record per edge on RTT channel 2: the time in us, the
level of every input and which inputs had an edge. A capture turns them into each signal's transitions.
"""

import struct
import threading
import time

RECORD = struct.Struct("<IHH")  # t_us, levels, edges
DRAIN_QUIET_S = 0.05  # How long the edge channel must stay empty to count as drained
STOP_TIMEOUT_S = 10.0  # For the last records to arrive after stopping


class Signal:
    """One channel's transitions over a capture: levels[i] holds from times[i] (seconds from the capture start)."""

    def __init__(self, name, initial, times, levels, duration):
        self.name = name
        self.times = [0.0] + times
        self.levels = [initial] + levels
        self.duration = duration

    def level_at(self, t):
        level = self.levels[0]
        for when, value in zip(self.times, self.levels):
            if when > t:
                break
            level = value
        return level

    def edges(self, rising=True):
        return [t for t, level in zip(self.times[1:], self.levels[1:]) if level == (1 if rising else 0)]

    def pulses(self):
        """(start, width) of every complete high pulse."""
        out = []
        start = None
        for t, level in zip(self.times, self.levels):
            if level and start is None:
                start = t
            elif not level and start is not None:
                if start > 0.0 or self.levels[0] == 0:
                    out.append((start, t - start))
                start = None
        return out

    def high_time(self, start=0.0, end=None):
        end = self.duration if end is None else end
        total = 0.0
        for i, (t, level) in enumerate(zip(self.times, self.levels)):
            t_next = self.times[i + 1] if i + 1 < len(self.times) else self.duration
            lo, hi = max(t, start), min(t_next, end)
            if level and hi > lo:
                total += hi - lo
        return total

    def duty(self, start=0.0, end=None):
        end = self.duration if end is None else end
        return self.high_time(start, end) / (end - start) if end > start else 0.0

    def period(self):
        """Mean time between rising edges, or None with fewer than two."""
        rises = self.edges(True)
        return (rises[-1] - rises[0]) / (len(rises) - 1) if len(rises) >= 2 else None


class Capture:
    def __init__(self, signals, duration):
        self.signals = signals
        self.duration = duration

    def __getitem__(self, name):
        return self.signals[name]


class CaptureError(RuntimeError):
    pass


def build_signals(records, lines, names, t0_us, duration):
    """Each named signal's transitions from the records, in seconds from t0_us. An edge flag with no change of level
    was a pulse shorter than the tester's interrupt latency: it becomes a zero-width pulse."""
    signals = {}
    for name in names:
        bit = 1 << lines[name]
        initial = 1 if records[0][1] & bit else 0
        level, times, levels = initial, [], []
        for t_us, record_levels, edges in records[1:]:
            t = ((t_us - t0_us) & 0xFFFFFFFF) / 1e6
            now = 1 if record_levels & bit else 0
            if now != level:
                times.append(t)
                levels.append(now)
            elif edges & bit:
                times += [t, t]
                levels += [1 - now, now]
            level = now
        signals[name] = Signal(name, initial, times, levels, duration)
    return signals


class PendingCapture:
    """A recording running in the background; wait() returns its Capture."""

    def __init__(self, tester, names, lines, seconds):
        self.tester = tester
        self.names = names
        self.lines = lines
        self.seconds = seconds
        self.data = bytearray()
        self.error = None
        self.result = None
        self.t0_us = tester.command("watch start")["t_us"]
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _run(self):
        try:
            end = time.monotonic() + self.seconds
            while time.monotonic() < end:
                self._read()
            stop = self.tester.command("watch stop")
            deadline = time.monotonic() + STOP_TIMEOUT_S
            while len(self.data) < stop["records"] * RECORD.size and time.monotonic() < deadline:
                self._read()
            if stop["dropped"]:
                raise CaptureError(f"the tester dropped {stop['dropped']} edges (the host didn't keep up)")
            if len(self.data) < stop["records"] * RECORD.size:
                raise CaptureError(f"{len(self.data) // RECORD.size} of {stop['records']} edge records arrived")
            records = [RECORD.unpack_from(self.data, i) for i in range(0, stop["records"] * RECORD.size, RECORD.size)]
            duration = ((stop["t_us"] - self.t0_us) & 0xFFFFFFFF) / 1e6
            self.result = Capture(build_signals(records, self.lines, self.names, self.t0_us, duration), duration)
        except Exception as e:  # Raised from wait(), in the test
            self.error = e

    def _read(self):
        data = self.tester.read_edges()
        if data:
            self.data += data
        else:
            time.sleep(0.005)

    def wait(self):
        self.thread.join(self.seconds + STOP_TIMEOUT_S + 30)
        if self.error:
            raise self.error
        if self.result is None:
            raise CaptureError("the capture didn't finish")
        return self.result


class LogicRecorder:
    """Records the flatsat's outputs with the tester. One capture at a time."""

    def __init__(self, tester):
        self.tester = tester
        self.lines = tester.command("watch get")["lines"]  # Signal name -> bit in the records

    def start(self, signals, seconds):
        """Starts recording `signals` for `seconds`; returns once recording has begun. Call .wait() after."""
        unknown = [s for s in signals if s not in self.lines]
        if unknown:
            raise ValueError(f"the tester doesn't watch {unknown}")
        # Leftovers of an earlier capture that didn't finish
        self.tester.command("watch stop")
        quiet_since = time.monotonic()
        while time.monotonic() - quiet_since < DRAIN_QUIET_S:
            if self.tester.read_edges():
                quiet_since = time.monotonic()
        return PendingCapture(self.tester, list(signals), self.lines, seconds)

    def capture(self, signals, seconds):
        return self.start(signals, seconds).wait()
