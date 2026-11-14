"""The flatsat's digital outputs through a sigrok logic analyzer (fx2lafw, see the capture-samd51-logic-analyzer skill).

Which analyzer channel watches which flatsat signal is set in hil/logic_channels.json ({"mtq_x_in1": "D0", ...}),
since an 8-channel analyzer can't watch every output at once. Tests ask for signals by name and skip when one isn't
wired. Signal names: mtq_{x,y,z}_in{1,2}, mtq_{x,y,z}_slp, heater1, heater2, burn, sband_en, sband_rst.

Captures run sigrok-cli in the background and are read back as VCD, which lists only the transitions.
"""

import json
import os
import shutil
import subprocess
import tempfile
import time

CONFIG = os.path.join(os.path.dirname(__file__), "..", "logic_channels.json")
DRIVER = os.environ.get("HIL_LOGIC_DRIVER", "fx2lafw")
START_DELAY_S = 1.5  # sigrok-cli takes about a second to open the device and start sampling


def load_channel_map(path=CONFIG):
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return {signal: channel for signal, channel in json.load(f).items() if not signal.startswith("_") and channel}


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


def parse_vcd(text, channel_to_signal, duration):
    timescale_s = 1e-12
    ids = {}
    initial = {}
    changes = {}
    now = 0.0
    in_header = True
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if in_header:
            if line.startswith("$timescale"):
                value = line.split()[1] if len(line.split()) > 2 else "1"
                units = {"s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12}
                number = "".join(c for c in value if c.isdigit()) or "1"
                unit = value[len(number):] or line.split()[2]
                timescale_s = int(number) * units[unit]
            elif line.startswith("$var"):
                parts = line.split()
                ids[parts[3]] = parts[4]  # identifier -> channel name (D0...)
            elif line.startswith("$enddefinitions"):
                in_header = False
            continue
        for token in line.split():  # sigrok writes "#<time> <value><id> ..." on one line
            if token[0] == "#":
                now = int(token[1:]) * timescale_s
            elif token[0] in "01" and token[1:] in ids:
                channel = ids[token[1:]]
                level = int(token[0])
                if channel not in initial:
                    initial[channel] = level
                else:
                    changes.setdefault(channel, []).append((now, level))
    signals = {}
    for channel, name in channel_to_signal.items():
        events = changes.get(channel, [])
        signals[name] = Signal(name, initial.get(channel, 0), [t for t, _ in events], [lv for _, lv in events], duration)
    return signals


class PendingCapture:
    def __init__(self, process, path, channel_to_signal, duration, workdir):
        self.process = process
        self.path = path
        self.channel_to_signal = channel_to_signal
        self.duration = duration
        self.workdir = workdir

    def wait(self):
        _, err = self.process.communicate(timeout=self.duration + 60)
        if self.process.returncode != 0:
            raise RuntimeError(f"sigrok-cli failed: {err.decode(errors='replace')}")
        with open(self.path) as f:
            text = f.read()
        shutil.rmtree(self.workdir, ignore_errors=True)
        return Capture(parse_vcd(text, self.channel_to_signal, self.duration), self.duration)


class LogicAnalyzer:
    def __init__(self, channel_map=None):
        self.channel_map = load_channel_map() if channel_map is None else channel_map

    @staticmethod
    def available():
        if shutil.which("sigrok-cli") is None:
            return False
        out = subprocess.run(["sigrok-cli", "--scan"], capture_output=True, text=True).stdout
        return DRIVER in out

    def missing(self, *signals):
        return [s for s in signals if s not in self.channel_map]

    def start(self, signals, seconds, samplerate=1_000_000):
        """Starts capturing `signals` for `seconds`; returns once sampling has (probably) begun. Call .wait() after."""
        channel_to_signal = {self.channel_map[s]: s for s in signals}
        workdir = tempfile.mkdtemp(prefix="hil_logic_")
        path = os.path.join(workdir, "capture.vcd")
        process = subprocess.Popen(
            ["sigrok-cli", "-d", DRIVER, "-c", f"samplerate={samplerate}", "-C", ",".join(channel_to_signal),
             "--time", str(int(seconds * 1000)), "-O", "vcd", "-o", path],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        time.sleep(START_DELAY_S)
        return PendingCapture(process, path, channel_to_signal, seconds, workdir)

    def capture(self, signals, seconds, samplerate=1_000_000):
        return self.start(signals, seconds, samplerate).wait()
