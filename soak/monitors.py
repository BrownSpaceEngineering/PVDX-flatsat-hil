"""Checks that run for the whole soak, fed the flatsat's log lines as they arrive and a tick about once a second.

Each monitor turns what it sees into Events (fail: the satellite did something it must not; warn: worth a look;
info: a recovery or a milestone) and keeps counters for the metrics and reports. Monitors take their times from the
caller (wall-clock seconds), so a recorded log can be replayed through them (run.py --replay). Their state() is JSON
and restore() takes it back, so a restarted runner carries on where it stopped.

Most monitors watch the RTT log and the tester's edge captures. With a HackRF ground station (hil/lib/ground.py),
RadioLink watches the UHF link, Uplinks follows each uplinked command until the satellite's telemetry shows it was
carried out, and Telemetry checks every decoded beacon against the flatsat's own log.
"""

import re
from dataclasses import asdict, dataclass, field

import firmware
from flatsat import DROPPED

FATAL = re.compile(r"\[FATAL")
WARNING = re.compile(r"\[WARNING\|[^\]]*\]: (.*)|^\[!\] (.*)")
BOOT = re.compile(r"Built from commit")
BOOT_COUNT = re.compile(r"Boot (\d+); burn wire")
RESET_CAUSE = re.compile(r"Reset cause: ([^(]+?) \(RCAUSE")
TRANSITION = re.compile(firmware.TRANSITION)
SWEEP_DONE = re.compile(r"perovskite: Sweep #(\d+) done, (\d+) steps failed")
SWEEP_FAILED = re.compile(r"perovskite: (Sweep not saved|Every step failed|Hardware initialization failed)")
BATTERY = re.compile(r"fuel_gauge: battery level (-?\d+)/1000")
FUEL_PACK = re.compile(r"fuel_gauge: pack (\d+) SOC=(-?\d+) m%\s+Vpack=(-?\d+) mV\s+I=(-?\d+) mA\s+T=(-?\d+) mC")
ADCS_MODE = re.compile(r"adcs: mode set to (do point|detumble only)")
UTC_SET = re.compile(r"adcs: UTC set to (\d+)")
# How the satellite says it ignored an uplinked command: radio-task logs it, main only logs that its task is missing
NOT_IMPLEMENTED = re.compile(r"uplink: command type (\d+) not implemented|uplink: command \d+ \(type (\d+)\) not sent")
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

    def finish(self, t):
        """The run is ending: settle anything still waiting."""

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
    run started (flashed just before), not a reset. Firmware that logs its reset cause (RSTC.RCAUSE, a line after
    "Built from commit") has it named in the event; a watchdog reset is always a failure, even when one was commanded.
    The boot count kept in MRAM also catches boots whose log was lost."""

    name = "resets"
    CAUSE_WAIT_S = 5.0  # How long after a boot line its reset cause line may come

    def __init__(self, emit, reboot_grace_s=120.0):
        super().__init__(emit)
        self.reboot_grace_s = reboot_grace_s
        self.expected_until = None  # A commanded reboot may show up until then
        self.boots = 0
        self.unexpected = 0
        self.causes = {}  # Reset cause -> count
        self.last_boot_count = None
        self.lines_seen = 0
        self.pending = None  # (time, line, first line of the run) of a boot waiting for its reset cause

    def expect_reboot(self, t):
        self.expected_until = t + self.reboot_grace_s

    def _judge(self, t, cause):
        boot_t, text, first = self.pending
        self.pending = None
        if cause:
            self.causes[cause] = self.causes.get(cause, 0) + 1
        why = f" (reset cause: {cause})" if cause else ""
        if first:
            self.event(boot_t, "info", f"the run starts at a boot{why}", [text])
        elif cause == "watchdog":
            self.unexpected += 1
            self.event(boot_t, "fail", "the watchdog reset the flatsat (a hung task, or fatal())", [text])
        elif self.expected_until is not None and boot_t <= self.expected_until:
            self.expected_until = None
            self.event(boot_t, "info", f"commanded reboot came back{why}")
        else:
            self.unexpected += 1
            self.event(boot_t, "fail", f"the flatsat reset on its own (no reboot was commanded){why}", [text])

    def on_line(self, t, text):
        self.lines_seen += 1
        if BOOT.search(text):
            if self.pending:
                self._judge(t, None)
            self.boots += 1
            self.pending = (t, text, self.lines_seen == 1)
            return
        match = RESET_CAUSE.search(text)
        if match and self.pending:
            self._judge(t, match.group(1))
            return
        match = BOOT_COUNT.search(text)
        if match:
            count = int(match.group(1))
            if self.last_boot_count is not None and count - self.last_boot_count > 1:
                self.event(t, "fail", f"boot count went from {self.last_boot_count} to {count}: "
                                      f"{count - self.last_boot_count - 1} boot(s) the log didn't show", [text])
            self.last_boot_count = count

    def on_tick(self, t):
        if self.pending and t - self.pending[0] > self.CAUSE_WAIT_S:
            self._judge(t, None)  # Firmware that doesn't log its reset cause

    def finish(self, t):
        if self.pending:
            self._judge(t, None)

    def metrics(self):
        return {"boots": self.boots, "unexpected_resets": self.unexpected, "boot_count": self.last_boot_count}

    def state(self):
        return {"boots": self.boots, "unexpected": self.unexpected, "causes": self.causes,
                "last_boot_count": self.last_boot_count, "lines_seen": self.lines_seen}


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


class RadioLink(Monitor):
    """The UHF link, from the ground station's side (hil/lib/ground.py). Fed every frame the ground hears (on_frame)
    and every pass it runs (on_pass), not log lines.

    Outside a link the satellite sends a beacon burst every beacon period. One missed burst is a warn event (a fade);
    a gap of more than 2.5 periods (two bursts in a row), away from passes, fails. A burst repeats one frame, so its VC frame count is the same throughout and goes up by one from
    burst to burst; a bigger step means bursts the ground missed. Frames the ground can't read are counted by reason,
    and the first of each reason is a warn event (a bad FCS on a good USLP frame means the ground's FCS check, not the
    link, is wrong). A pass the satellite doesn't answer fails."""

    name = "radio"

    def __init__(self, emit, beacon_period_s, link_timeout_s):
        super().__init__(emit)
        self.beacon_period_s = beacon_period_s
        self.link_timeout_s = link_timeout_s
        self.last_beacon_t = None
        self.quiet_until = 0.0  # Beacons stop during a pass and until the satellite's link times out after it
        self.late = False
        self.last_counts = {}  # VC -> last frame count
        self.beacons = 0
        self.bursts = 0
        self.missed_bursts = 0
        self.bad = {}  # Reason -> count
        self.passes = 0
        self.passes_failed = 0
        self.power_db = None
        self.freq_offset_hz = None
        self.fec = {}  # How coded frames were decoded ("viterbi", "systematic:0", ...) -> count
        self.corrected_bits = 0  # Coded bits the Viterbi decoder corrected, in all
        self.decoded_bits = 0

    def on_frame(self, t, rx, is_beacon):
        self.power_db, self.freq_offset_hz = rx.power_db, rx.freq_offset_hz
        if rx.coded:
            kind = rx.fec.split(":")[0] if rx.fec else "failed"
            if kind == "systematic" and kind not in self.fec:
                self.event(t, "info", "coded frames are read from their systematic bits: the RSC variant in mrfsk.py "
                                      "isn't confirmed yet (run soak/tools/fit_rsc.py on this run's coded.jsonl)")
            self.fec[kind] = self.fec.get(kind, 0) + 1
            if rx.corrected_bits is not None and rx.coded_bits is not None:
                self.corrected_bits += rx.corrected_bits
                self.decoded_bits += len(rx.coded_bits)
        if rx.error:
            self.bad[rx.error] = self.bad.get(rx.error, 0) + 1
            if self.bad[rx.error] == 1:
                self.event(t, "warn", f"unreadable frame: {rx.error}")
            if rx.frame is None:
                return
        f = rx.frame
        last = self.last_counts.get(f.vcid)
        if is_beacon:
            self.beacons += 1
            if self.late:
                self.event(t, "info", f"beacons resumed after {t - self.last_beacon_t:.0f} s")
                self.late = False
            self.last_beacon_t = t
            if last != f.vc_frame_count:
                self.bursts += 1
                step = None if last is None else (f.vc_frame_count - last) % 256
                if step is not None and step > 1:
                    self.missed_bursts += step - 1
                    self.event(t, "warn", f"missed {step - 1} beacon burst(s) (VC {f.vcid} frame count {last} -> "
                                          f"{f.vc_frame_count})")
        self.last_counts[f.vcid] = f.vc_frame_count

    def pass_started(self, t, duration_s):
        self.quiet_until = max(self.quiet_until, t + duration_s + self.link_timeout_s)

    def on_pass(self, t, report):
        self.passes += 1
        self.quiet_until = max(self.quiet_until, t + self.link_timeout_s)
        if not report.established or report.failure:
            self.passes_failed += 1
            self.event(t, "fail", f"pass failed: {report.failure} (established: {report.established}, "
                                  f"turns {report.turns_given}/{report.turns_taken}, {report.resent} re-sends)")
        else:
            self.event(t, "info", f"pass: link up in {report.establish_s:.1f} s, {report.turns_taken} turns, "
                                  f"{len(report.received)} frames, {report.resent} re-sends")

    def on_tick(self, t):
        if self.last_beacon_t is None:
            self.last_beacon_t = t
        limit = 2.5 * self.beacon_period_s
        if t < self.quiet_until:
            self.last_beacon_t = max(self.last_beacon_t, t)
        elif not self.late and t - self.last_beacon_t > limit:
            self.late = True
            self.event(t, "fail", f"no beacon for {limit:.0f} s")

    def metrics(self):
        return {"beacons": self.beacons, "beacon_bursts": self.bursts, "missed_bursts": self.missed_bursts,
                "unreadable_frames": sum(self.bad.values()), "passes": self.passes, "passes_failed": self.passes_failed,
                "rx_power_db": None if self.power_db is None else round(self.power_db, 1),
                "rx_offset_hz": None if self.freq_offset_hz is None else round(self.freq_offset_hz),
                "fec_viterbi": self.fec.get("viterbi", 0), "fec_systematic": self.fec.get("systematic", 0),
                "fec_failed": self.fec.get("failed", 0),
                # Coded bits in error before correction: the link's margin, long before frames start failing
                "fec_bit_error_rate": round(self.corrected_bits / self.decoded_bits, 6) if self.decoded_bits else None}

    def state(self):
        return {"beacons": self.beacons, "bursts": self.bursts, "missed_bursts": self.missed_bursts, "bad": self.bad,
                "passes": self.passes, "passes_failed": self.passes_failed, "fec": self.fec,
                "corrected_bits": self.corrected_bits, "decoded_bits": self.decoded_bits}


# Telemetry health field of each device that has one (device_id_t name -> tel_downlink_t field)
HEALTH_FIELD = {"GYROSCOPE_ID": "gyro.health", "CAMERA_ID": "camera.health", "DISPLAY_ID": "display_health",
                "UHF_ID": "uhf_health", "SBAND_ID": "sband.health"}


class Uplinks(Monitor):
    """Each uplinked command file (soak/uplinks.py) and what it should change, followed until a beacon shows it.

    A transfer the satellite doesn't finish fails at once. Commands take effect by the time the pass ends (radio-task
    holds most until uplink_commands_pass_exit()), so each check must pass in a beacon heard within settle_s of the
    pass ending, or it fails: the file's timestamp appears in command_timestamps; SET_UNIX_TIME sets UTC-valid and a
    timestamp within the beacon's age of the ground's clock; SET_ADCS_OP_MODE shows in adcs.opmode;
    ENABLE_DISABLE_DEVICE shows in the device's health field. A command the satellite logs as not implemented is a warn
    event instead (once per command type)."""

    name = "uplink"

    def __init__(self, emit, constants, settle_s, beacon_age_s):
        super().__init__(emit)
        self.c = constants
        self.settle_s = settle_s
        self.time_tolerance_s = beacon_age_s + 5  # A burst repeats the telemetry it collected when it started
        self.pending = []  # {"what", "deadline", ...} checks not yet seen in telemetry
        self.last_seen = {}  # what -> the last value seen, for the failure message
        self.sent = 0
        self.failed = 0
        self.confirmed = 0
        self.unconfirmed = 0
        self.not_implemented = []  # Command type numbers the satellite said it ignored
        self.delivery_warned = False

    def on_pass(self, t, report, commands, file_timestamp):
        uplink = report.uplink
        if uplink is None:
            return
        self.sent += 1
        names = ", ".join(cmd["name"] for cmd in commands)
        if not uplink.succeeded:
            self.failed += 1
            self.event(t, "fail", f"uplink {uplink.seq} ({names}) not finished: {report.failure or 'no Finished'}",
                       uplink.events)
            return
        self.event(t, "info", f"uplink {uplink.seq} ({names}) finished in {uplink.turns} turns, "
                              f"{uplink.resent_pdus} PDUs re-sent", uplink.events)
        if uplink.delivery_incomplete and not self.delivery_warned:
            self.delivery_warned = True
            self.event(t, "warn", "the satellite's Finished says delivery incomplete for a file it received whole: "
                                  "cfdp_send_fin() sends delivery_complete, which only a send transaction ever sets")
        deadline = t + self.settle_s
        self.pending.append({"what": "command_timestamps", "deadline": deadline, "value": file_timestamp,
                             "after": t, "label": f"uplink {uplink.seq} in command_timestamps"})
        for cmd in commands:
            check = dict(cmd, deadline=deadline, after=t)
            if cmd["name"] == "SET_UNIX_TIME":
                check.update(what="utc", label="UTC set (UTC-valid flag and the ground's time)")
            elif cmd["name"] == "SET_ADCS_OP_MODE":
                check.update(what="adcs.opmode", label=f"ADCS opmode {cmd['value']}")
            elif cmd["name"] == "ENABLE_DISABLE_DEVICE":
                field_name = HEALTH_FIELD.get(cmd["device_name"])
                if field_name is None:
                    self.event(t, "info", f"{cmd['device_name']} has no health field in telemetry to check")
                    continue
                want = "TEL_DEVICE_STATUS_OK" if cmd["enabled"] else "TEL_DEVICE_STATUS_DISABLED"
                check.update(what=field_name, value=self.c[want],
                             label=f"{cmd['device_name']} {'enabled' if cmd['enabled'] else 'disabled'} ({field_name})")
            else:
                continue
            self.pending.append(check)

    def _holds(self, t, check, tel):
        what = check["what"]
        if what == "command_timestamps":
            self.last_seen[what] = tel["command_timestamps"][:3]
            return check["value"] in tel["command_timestamps"]
        if what == "utc":
            valid = bool(tel["adcs.flags"] & self.c["TEL_ADCS_FLAG_UTC_VALID"])
            self.last_seen[what] = {"utc_valid": valid, "timestamp": tel["timestamp"], "ground": round(t)}
            return valid and abs(tel["timestamp"] - t) <= self.time_tolerance_s
        self.last_seen[what] = tel[what]
        return tel[what] == check["value"]

    def on_telemetry(self, t, tel):
        keep = []
        for check in self.pending:
            if t >= check["after"] and self._holds(t, check, tel):
                self.confirmed += 1
                self.event(t, "info", f"confirmed in telemetry: {check['label']}")
            else:
                keep.append(check)
        self.pending = keep

    def on_line(self, t, text):
        match = NOT_IMPLEMENTED.search(text)
        if not match:
            return
        kind = int(match.group(1) or match.group(2))
        dropped = [c for c in self.pending if self.c.get(c.get("name", "")) == kind]
        if dropped:
            self.pending = [c for c in self.pending if c not in dropped]
            if kind not in self.not_implemented:
                self.not_implemented.append(kind)
                self.event(t, "warn", f"the satellite ignored {dropped[0]['name']}: not implemented in this build", [text])

    def on_tick(self, t):
        keep = []
        for check in self.pending:
            if t > check["deadline"]:
                self.unconfirmed += 1
                self.event(t, "fail", f"never seen in telemetry within {self.settle_s:.0f} s of the pass: "
                                      f"{check['label']} (last seen: {self.last_seen.get(check['what'])})")
            else:
                keep.append(check)
        self.pending = keep

    def metrics(self):
        return {"uplinks": self.sent, "uplinks_failed": self.failed, "uplink_checks_confirmed": self.confirmed,
                "uplink_checks_failed": self.unconfirmed}

    def state(self):
        return {"pending": self.pending, "sent": self.sent, "failed": self.failed, "confirmed": self.confirmed,
                "unconfirmed": self.unconfirmed, "not_implemented": self.not_implemented,
                "delivery_warned": self.delivery_warned}


class Telemetry(Monitor):
    """Each decoded beacon against what the flatsat logged around it: the fuel gauge readings, the state machine's
    state, the boot time and the UHF radio's own health. Telemetry that disagrees with the log is a warn event (the
    first time for each kind of mismatch; all are counted)."""

    name = "telemetry"
    EPS_TOLERANCE = {"charge_percent": 1.0, "voltage": 0.05, "current": 0.05, "temperature": 1.0}  # %, V, A, degC
    LOG_FRESH_S = 30  # A fuel gauge log line older than this isn't compared

    def __init__(self, emit, constants, beacon_age_s):
        super().__init__(emit)
        self.c = constants
        self.beacon_age_s = beacon_age_s
        self.packs = {}  # pack -> (t, {field: value in telemetry units})
        self.states = []  # (t, state id) of recent transitions
        self.boot_t = None
        self.last = None  # (t, timestamp, boot_timestamp) of the previous beacon
        self.decoded = 0
        self.mismatches = {}  # kind -> count

    def on_line(self, t, text):
        if BOOT.search(text):
            self.boot_t = t
            self.states = []
            return
        match = FUEL_PACK.search(text)
        if match:
            pack, soc, mv, ma, mc = (int(x) for x in match.groups())
            self.packs[pack] = (t, {"charge_percent": soc / 1000, "voltage": mv / 1000, "current": ma / 1000,
                                    "temperature": mc / 1000})
            return
        match = TRANSITION.search(text)
        if match:
            self.states.append((t, int(match.group(2))))
            self.states = [s for s in self.states if t - s[0] < 600] or self.states[-1:]

    def _mismatch(self, t, kind, message):
        self.mismatches[kind] = self.mismatches.get(kind, 0) + 1
        if self.mismatches[kind] == 1:
            self.event(t, "warn", f"telemetry disagrees with the log: {message}")

    def on_telemetry(self, t, tel):
        self.decoded += 1
        if tel["uhf_health"] != self.c["TEL_DEVICE_STATUS_OK"]:
            self._mismatch(t, "uhf_health", f"uhf_health is {tel['uhf_health']} in a beacon the UHF radio sent")
        for pack, (logged_t, values) in self.packs.items():
            if t - logged_t > self.LOG_FRESH_S:
                continue
            for name, tolerance in self.EPS_TOLERANCE.items():
                got = tel[f"eps.{name}"][pack]
                if abs(got - values[name]) > tolerance:
                    self._mismatch(t, f"eps.{name}", f"eps.{name}[{pack}] is {got:.3f}, the log says {values[name]:.3f}")
        if self.states:
            # The beacon's telemetry was collected when its burst started, up to beacon_age_s before it was heard
            recent = {s for st, s in self.states if st >= t - self.beacon_age_s - 5}
            current = [s for st, s in self.states if st < t - self.beacon_age_s - 5][-1:]
            allowed = recent | set(current)
            if tel["state_machine.state_history"][0] not in allowed:
                self._mismatch(t, "state", f"state {tel['state_machine.state_history'][0]}, the log says "
                                           f"{sorted(allowed)}")
        if self.last is not None:
            last_t, last_ts, last_boot = self.last
            rebooted = self.boot_t is not None and self.boot_t > last_t
            if last_boot and tel["boot_timestamp"] and abs(tel["boot_timestamp"] - last_boot) > 5 and not rebooted:
                self._mismatch(t, "boot_timestamp", f"boot_timestamp moved {tel['boot_timestamp'] - last_boot:+d} s "
                                                    f"without a reset")
            if last_ts and tel["timestamp"] and tel["timestamp"] < last_ts and not rebooted:
                self._mismatch(t, "timestamp", f"timestamp went back {last_ts - tel['timestamp']} s")
        self.last = (t, tel["timestamp"], tel["boot_timestamp"])

    def metrics(self):
        return {"telemetry_beacons": self.decoded, "telemetry_mismatches": sum(self.mismatches.values())}

    def state(self):
        return {"decoded": self.decoded, "mismatches": self.mismatches}


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


def build(config, emit, radio_constants=None):
    """The monitors a soak config asks for, in the order they see each line. radio_constants (ccsds.constants()) adds
    the radio, uplink and telemetry monitors."""
    m = config["monitors"]
    built = [
        LogSilence(emit, m["max_log_silence_s"]),
        Resets(emit, m["reboot_grace_s"]),
        LogHealth(emit, m["warning_allowlist"], m["max_warnings_per_hour"]),
        StateMachine(emit, m["max_state_s"], m["default_max_state_s"]),
        Payload(emit, m["max_sweep_gap_s"]),
        Power(emit, m["min_battery_level"]),
    ]
    if radio_constants is not None:
        c = radio_constants
        beacon_period_s = (c["UHF_BEACON_PERIOD_MS"] + c["UHF_BEACON_BURST_MS"]) / 1000
        beacon_age_s = c["UHF_BEACON_BURST_MS"] / 1000
        link_timeout_s = c["UHF_LINK_TIMEOUT_MS"] / 1000
        built.append(RadioLink(emit, beacon_period_s, link_timeout_s))
        # A command is checked in the beacons after the pass: the link has to time out first, then a burst starts
        settle_s = config.get("radio", {}).get("uplink_settle_s", link_timeout_s + 3 * beacon_period_s)
        built.append(Uplinks(emit, c, settle_s, beacon_age_s))
        built.append(Telemetry(emit, c, beacon_age_s))
    return built
