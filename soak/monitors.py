"""Checks that run for the whole soak, fed the flatsat's log lines as they arrive and a tick about once a second.

Each monitor turns what it sees into Events (fail: the satellite did something it must not; warn: worth a look;
info: a recovery or a milestone) and keeps counters for the metrics and reports. Monitors take their times from the
caller (wall-clock seconds), so a recorded log can be replayed through them (run.py --replay). Their state() is JSON
and restore() takes it back, so a restarted runner carries on where it stopped.

Phase 1 monitors watch the RTT log and the tester's edge captures. The radio monitors (downlink cadence, uplink
acknowledgement) come with the ground station; see the soak section of hil/README.md.
"""

import re
from dataclasses import asdict, dataclass, field

import firmware
from flatsat import DROPPED

FATAL = re.compile(r"\[FATAL")
WARNING = re.compile(r"\[WARNING\|[^\]]*\]: (.*)|^\[!\] (.*)")
BOOT = re.compile(r"Built from commit")
BOOT_COUNT = re.compile(r"Boot (\d+); burn wire")
TRANSITION = re.compile(firmware.TRANSITION)
SWEEP_DONE = re.compile(r"perovskite: Sweep #(\d+) done, (\d+) steps failed")
SWEEP_FAILED = re.compile(r"perovskite: (Sweep not saved|Every step failed|Hardware initialization failed)")
BATTERY = re.compile(r"fuel_gauge: battery level (-?\d+)/1000")
NUMBERS = re.compile(r"\b0x[0-9a-fA-F]+\b|(?<![A-Za-z_])-?\d+(\.\d+)?\b")  # Values, not names like I2C


@dataclass
class Event:
    t: float
    severity: str  # "fail", "warn" or "info"
    monitor: str
    message: str
    context: list = field(default_factory=list)  # Log lines around it

    def to_json(self):
        return asdict(self)


class Monitor:
    name = "monitor"

    def __init__(self, emit):
        self.emit = emit  # emit(Event)

    def event(self, t, severity, message, context=()):
        self.emit(Event(t, severity, self.name, message, list(context)))

    def on_line(self, t, text):
        pass

    def on_tick(self, t):
        pass

    def metrics(self):
        return {}

    def state(self):
        return {}

    def restore(self, state):
        for key, value in state.items():
            setattr(self, key, value)


class LogSilence(Monitor):
    """The flatsat logs several lines a second while it runs; a long silence means it hung, lost power, or the J-Link
    stopped reading."""

    name = "liveness"

    def __init__(self, emit, max_silence_s):
        super().__init__(emit)
        self.max_silence_s = max_silence_s
        self.last_line_t = None
        self.silent = False
        self.lines = 0

    def on_line(self, t, text):
        self.lines += 1
        if self.silent:
            self.event(t, "info", f"log resumed after {t - self.last_line_t:.0f} s of silence")
            self.silent = False
        self.last_line_t = t

    def on_tick(self, t):
        if self.last_line_t is None:
            self.last_line_t = t  # Silence is counted from the start of the run
        if not self.silent and t - self.last_line_t > self.max_silence_s:
            self.silent = True
            self.event(t, "fail", f"no log lines for {self.max_silence_s:.0f} s")

    def metrics(self):
        return {"log_lines": self.lines}

    def state(self):
        return {"lines": self.lines}


class Resets(Monitor):
    """The soak never resets the flatsat, so every boot is a reset the satellite did on its own, unless the runner
    commanded one (expect_reboot(), for the REBOOT uplink). A boot that is the first thing the run sees is where the
    run started (flashed just before), not a reset. The boot count kept in MRAM also catches boots whose log was
    lost."""

    name = "resets"

    def __init__(self, emit, reboot_grace_s=120.0):
        super().__init__(emit)
        self.reboot_grace_s = reboot_grace_s
        self.expected_until = None  # A commanded reboot may show up until then
        self.boots = 0
        self.unexpected = 0
        self.last_boot_count = None
        self.lines_seen = 0

    def expect_reboot(self, t):
        self.expected_until = t + self.reboot_grace_s

    def on_line(self, t, text):
        self.lines_seen += 1
        if BOOT.search(text):
            self.boots += 1
            if self.lines_seen == 1:
                self.event(t, "info", "the run starts at a boot", [text])
            elif self.expected_until is not None and t <= self.expected_until:
                self.expected_until = None
                self.event(t, "info", "commanded reboot came back")
            else:
                self.unexpected += 1
                self.event(t, "fail", "the flatsat reset on its own (no reboot was commanded)", [text])
            return
        match = BOOT_COUNT.search(text)
        if match:
            count = int(match.group(1))
            if self.last_boot_count is not None and count - self.last_boot_count > 1:
                self.event(t, "fail", f"boot count went from {self.last_boot_count} to {count}: "
                                      f"{count - self.last_boot_count - 1} boot(s) the log didn't show", [text])
            self.last_boot_count = count

    def metrics(self):
        return {"boots": self.boots, "unexpected_resets": self.unexpected, "boot_count": self.last_boot_count}

    def state(self):
        return {"boots": self.boots, "unexpected": self.unexpected, "last_boot_count": self.last_boot_count,
                "lines_seen": self.lines_seen}


class LogHealth(Monitor):
    """Fatals and dropped log lines always fail. Warnings not on the allowlist are reported once per distinct message
    (numbers stripped), and fail if they come faster than max_warnings_per_hour."""

    name = "log"

    def __init__(self, emit, allowlist, max_warnings_per_hour):
        super().__init__(emit)
        self.allowlist = [re.compile(p) for p in allowlist]
        self.max_per_hour = max_warnings_per_hour
        self.fatals = 0
        self.dropped = 0
        self.warnings = 0
        self.seen = {}  # Message shape -> count
        self.window = []  # Times of recent non-allowlisted warnings
        self.rate_failing = False

    def on_line(self, t, text):
        if FATAL.search(text):
            self.fatals += 1
            self.event(t, "fail", "fatal error", [text])
            return
        match = re.search(DROPPED, text)
        if match:
            self.dropped += int(match.group(1))
            self.event(t, "fail", f"the flatsat dropped {match.group(1)} log lines (RTT buffer full)", [text])
            return
        match = WARNING.search(text)
        if not match or any(p.search(text) for p in self.allowlist):
            return
        self.warnings += 1
        message = (match.group(1) or match.group(2)).strip()
        shape = NUMBERS.sub("#", message)
        self.seen[shape] = self.seen.get(shape, 0) + 1
        if self.seen[shape] == 1:
            self.event(t, "warn", f"new warning: {message}", [text])
        self.window = [w for w in self.window if t - w < 3600] + [t]
        if len(self.window) > self.max_per_hour and not self.rate_failing:
            self.rate_failing = True
            self.event(t, "fail", f"{len(self.window)} warnings in the last hour (limit {self.max_per_hour})")
        elif len(self.window) <= self.max_per_hour // 2:
            self.rate_failing = False

    def metrics(self):
        return {"fatals": self.fatals, "dropped_log_lines": self.dropped, "warnings": self.warnings}

    def state(self):
        return {"fatals": self.fatals, "dropped": self.dropped, "warnings": self.warnings, "seen": self.seen}


class StateMachine(Monitor):
    """Every state has a longest stay; staying longer means the task manager or the task that owns the exit stalled.
    Also counts transitions, which keep coming as long as the state machine runs."""

    name = "state_machine"

    def __init__(self, emit, max_state_s, default_max_s):
        super().__init__(emit)
        names = {v: k for k, v in firmware.state_ids().items()}
        self.names = names
        self.max_s = {names_id: max_state_s.get(name, default_max_s) for names_id, name in names.items()}
        self.default_max_s = default_max_s
        self.state_id = None
        self.since = None
        self.overdue = False
        self.transitions = 0

    def on_line(self, t, text):
        if BOOT.search(text):
            self.state_id, self.since, self.overdue = None, t, False  # Starts over; the first transition says where
            return
        match = TRANSITION.search(text)
        if match:
            self.transitions += 1
            self.state_id, self.since, self.overdue = int(match.group(2)), t, False

    def on_tick(self, t):
        if self.since is None:
            self.since = t
        limit = self.max_s.get(self.state_id, self.default_max_s)
        if not self.overdue and t - self.since > limit:
            self.overdue = True
            name = self.names.get(self.state_id, "no transition yet")
            self.event(t, "fail", f"{name} for more than {limit:.0f} s")

    def metrics(self):
        return {"state": self.names.get(self.state_id, ""), "transitions": self.transitions}

    def state(self):
        return {"transitions": self.transitions}


class Payload(Monitor):
    """Perovskite sweeps keep completing (on the emulated board they always can)."""

    name = "payload"

    def __init__(self, emit, max_gap_s):
        super().__init__(emit)
        self.max_gap_s = max_gap_s
        self.last_done_t = None
        self.late = False
        self.sweeps = 0
        self.failed = 0

    def on_line(self, t, text):
        match = SWEEP_DONE.search(text)
        if match:
            self.sweeps += 1
            self.last_done_t = t
            if self.late:
                self.late = False
                self.event(t, "info", "perovskite sweeps resumed")
            if int(match.group(2)):
                self.event(t, "warn", f"sweep #{match.group(1)} had {match.group(2)} failed steps", [text])
            return
        if SWEEP_FAILED.search(text):
            self.failed += 1
            self.event(t, "warn", "perovskite sweep failed", [text])

    def on_tick(self, t):
        if self.last_done_t is None:
            self.last_done_t = t
        if not self.late and t - self.last_done_t > self.max_gap_s:
            self.late = True
            self.event(t, "fail", f"no perovskite sweep finished in {self.max_gap_s:.0f} s")

    def metrics(self):
        return {"sweeps": self.sweeps, "sweeps_failed": self.failed}

    def state(self):
        return {"sweeps": self.sweeps, "failed": self.failed}


class Power(Monitor):
    """The battery level the fuel-gauge task logs stays in range."""

    name = "power"

    def __init__(self, emit, min_level):
        super().__init__(emit)
        self.min_level = min_level
        self.level = None
        self.low = False

    def on_line(self, t, text):
        match = BATTERY.search(text)
        if not match:
            return
        self.level = int(match.group(1)) / 1000.0
        if not self.low and self.level < self.min_level:
            self.low = True
            self.event(t, "fail", f"battery level {self.level:.3f}, below {self.min_level}", [text])
        elif self.low and self.level >= self.min_level + 0.05:
            self.low = False
            self.event(t, "info", f"battery level back to {self.level:.3f}")

    def metrics(self):
        return {"battery_level": self.level}


def check_outputs(t, capture, emit):
    """The hourly edge capture: the magnetorquer H-bridges never brake (IN1 and IN2 both high), and a burned board's
    burn pin stays low."""
    from logic import both_high_time

    for axis in "xyz":
        both = both_high_time(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])
        if both > 0:
            emit(Event(t, "fail", "outputs", f"magnetorquer {axis}: IN1 and IN2 both high for {both * 1e6:.0f} us"))
    if capture["burn"].high_time() > 0:
        emit(Event(t, "fail", "outputs", f"burn pin high for {capture['burn'].high_time():.3f} s"))


OUTPUT_SIGNALS = [f"mtq_{axis}_in{n}" for axis in "xyz" for n in (1, 2)] + ["burn"]


def build(config, emit):
    """The monitors a soak config asks for, in the order they see each line."""
    m = config["monitors"]
    return [
        LogSilence(emit, m["max_log_silence_s"]),
        Resets(emit, m["reboot_grace_s"]),
        LogHealth(emit, m["warning_allowlist"], m["max_warnings_per_hour"]),
        StateMachine(emit, m["max_state_s"], m["default_max_state_s"]),
        Payload(emit, m["max_sweep_gap_s"]),
        Power(emit, m["min_battery_level"]),
    ]
