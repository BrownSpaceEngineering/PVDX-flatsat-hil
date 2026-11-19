"""The soak monitors against crafted logs; no hardware. Run with `pytest soak/tests`."""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, os.path.join(HERE, "..", "..", "hil", "lib"))

import tomllib  # noqa: E402

import firmware  # noqa: E402
import monitors  # noqa: E402
import report  # noqa: E402
import run  # noqa: E402
import schedule  # noqa: E402

with open(os.path.join(HERE, "..", "config.toml"), "rb") as f:
    CONFIG = tomllib.load(f)
STATES = firmware.state_ids()
T0 = 1_800_000_000.0


def transition(frm, to):
    return f"[INFO|state_machine.c:87]: state_machine: state {STATES[frm]} -> state {STATES[to]}"


def healthy(start, seconds):
    """A log that keeps the monitors happy: a line a second, the IDLE/heartbeat cycle, a sweep every 5 minutes."""
    lines = []
    for s in range(int(seconds)):
        t = start + s
        lines.append((t, "[DEBUG|watchdog_main.c:40]: watchdog: checked in"))
        cycle = s % 300
        if cycle == 0:
            lines.append((t, transition("STATE_PEROVSKITE_CAPTURE", "STATE_IDLE")))
        elif cycle == 25:
            lines.append((t, transition("STATE_IDLE", "STATE_FAKE_HEARTBEAT")))
        elif cycle == 30:
            lines.append((t, transition("STATE_FAKE_HEARTBEAT", "STATE_IDLE")))
        elif cycle == 31:
            lines.append((t, transition("STATE_IDLE", "STATE_PEROVSKITE_CAPTURE")))
        elif cycle == 150:
            lines.append((t, f"[INFO|perovskite_task.c:339]: perovskite: Sweep #{s // 300} done, 0 steps failed"))
        if s % 30 == 0:
            lines.append((t, "[INFO|fuel_gauge_main.c:90]: fuel_gauge: battery level 900/1000"))
    return lines


def run_lines(lines, end=None):
    """Feeds (t, text) lines with a tick each second; returns the events."""
    events = []
    ms = monitors.build(CONFIG, events.append)
    next_tick = T0
    for t, text in lines:
        while next_tick <= t:
            for m in ms:
                m.on_tick(next_tick)
            next_tick += 1
        for m in ms:
            m.on_line(t, text)
    for tick in range(int(next_tick), int(end or next_tick) + 1):
        for m in ms:
            m.on_tick(tick)
    return events


def fails(events):
    return [e for e in events if e.severity == "fail"]


def test_a_healthy_hour_has_no_failures():
    events = run_lines(healthy(T0, 3600))
    assert not fails(events), [e.message for e in fails(events)]


def test_an_uncommanded_reset_fails():
    lines = healthy(T0, 600) + [(T0 + 600, "[INFO|main.c:199]: [+] Built from commit: abc1234")]
    events = run_lines(lines, end=T0 + 610)
    assert [e.monitor for e in fails(events)] == ["resets"]


def test_a_boot_at_the_start_of_the_run_is_not_a_reset():
    lines = [(T0, "[INFO|main.c:199]: [+] Built from commit: abc1234")] + healthy(T0 + 1, 120)
    assert not fails(run_lines(lines))


def test_the_reset_cause_is_reported_and_a_watchdog_reset_always_fails():
    events = []
    resets = monitors.Resets(events.append)
    resets.on_line(T0, "[INFO|main.c:1]: [+] Built from commit: abc1234")
    resets.on_line(T0, "[+] Reset cause: power-on (RCAUSE 0x01)")
    resets.expect_reboot(T0 + 10)
    resets.on_line(T0 + 20, "[INFO|main.c:1]: [+] Built from commit: abc1234")
    resets.on_line(T0 + 20, "[+] Reset cause: watchdog (RCAUSE 0x20)")
    assert [e.severity for e in events] == ["info", "fail"]
    assert "power-on" in events[0].message and "watchdog" in events[1].message
    assert resets.causes == {"power-on": 1, "watchdog": 1}


def test_a_boot_without_a_reset_cause_is_judged_after_a_wait():
    events = []
    resets = monitors.Resets(events.append)
    resets.on_line(T0, "tick")
    resets.on_line(T0 + 1, "[+] Built from commit: abc1234")
    resets.on_tick(T0 + 2)
    assert not events
    resets.on_tick(T0 + 10)
    assert [e.severity for e in events] == ["fail"]


def test_a_commanded_reboot_does_not_fail():
    events = []
    resets = monitors.Resets(events.append, reboot_grace_s=120)
    resets.on_line(T0, "tick")
    resets.expect_reboot(T0)
    resets.on_line(T0 + 30, "[+] Built from commit: abc1234")
    resets.on_tick(T0 + 40)
    assert not fails(events)
    resets.on_line(T0 + 400, "[+] Built from commit: abc1234")
    resets.on_tick(T0 + 410)
    assert len(fails(events)) == 1, "a second boot after the grace period is uncommanded"


def test_a_boot_count_jump_shows_lost_boots():
    events = run_lines([(T0, "[+] Boot 7; burn wire burned (1 attempts), 0 s of the burn wait done"),
                        (T0 + 1, "[+] Boot 9; burn wire burned (1 attempts), 0 s of the burn wait done")])
    assert any("boot count went from 7 to 9" in e.message for e in fails(events))


def test_fatal_and_dropped_lines_fail():
    lines = healthy(T0, 120) + [(T0 + 120, "[FATAL|adcs_task.c:12]: adcs: queue full"),
                                (T0 + 121, "[!] 12 log lines dropped")]
    messages = [e.message for e in fails(run_lines(lines))]
    assert "fatal error" in messages
    assert any("dropped 12 log lines" in m for m in messages)


def test_warnings_are_reported_once_per_shape_and_allowlisted_ones_not_at_all():
    lines = healthy(T0, 60) + [
        (T0 + 61, "[WARNING|stxg2.c:246]: stxg2: no answer on I2C after 5000 ms (i2c error -2)"),
        (T0 + 62, "[WARNING|stxg2.c:246]: stxg2: no answer on I2C after 5000 ms (i2c error -5)"),
        (T0 + 63, "[WARNING|fuel_gauge_main.c:83]: fuel_gauge: no pack read, reporting full battery"),
    ]
    warns = [e for e in run_lines(lines) if e.severity == "warn"]
    assert len(warns) == 1 and "stxg2: no answer on I2C" in warns[0].message


def test_a_warning_storm_fails():
    lines = healthy(T0, 60) + [(T0 + 61 + i, f"[WARNING|x.c:1]: thing {i} failed") for i in range(200)]
    assert any("warnings in the last hour" in e.message for e in fails(run_lines(lines)))


def test_log_silence_fails_and_recovers():
    lines = healthy(T0, 60) + healthy(T0 + 200, 10)
    events = run_lines(lines)
    assert any(e.monitor == "liveness" and e.severity == "fail" for e in events)
    assert any(e.monitor == "liveness" and e.severity == "info" for e in events)


def test_a_stuck_state_fails():
    lines = [(T0 + s, "tick") for s in range(400)]
    lines.insert(1, (T0 + 1, transition("STATE_IDLE", "STATE_PEROVSKITE_CAPTURE")))
    events = run_lines(lines)
    assert any(e.monitor == "state_machine" and "STATE_PEROVSKITE_CAPTURE" in e.message for e in fails(events))


def test_sweeps_that_stop_fail():
    lines = [(T0 + s, "tick") for s in range(CONFIG["monitors"]["max_sweep_gap_s"] + 10)]
    assert any(e.monitor == "payload" for e in fails(run_lines(lines)))


def test_low_battery_fails():
    lines = healthy(T0, 60) + [(T0 + 61, "[INFO|fuel_gauge_main.c:90]: fuel_gauge: battery level 150/1000")]
    assert any(e.monitor == "power" for e in fails(run_lines(lines)))


def test_monitor_state_survives_a_restart():
    events = []
    first = monitors.build(CONFIG, events.append)
    for m in first:
        m.on_line(T0, "[INFO|perovskite_task.c:339]: perovskite: Sweep #3 done, 0 steps failed")
    saved = json.loads(json.dumps({m.name: m.state() for m in first}))
    second = monitors.build(CONFIG, events.append)
    for m in second:
        m.restore(saved[m.name])
    assert next(m for m in second if m.name == "payload").sweeps == 1


def test_orbit_schedule():
    orbit = schedule.Orbit(T0, 100.0, 0.35, -10.0, 30.0)
    assert orbit.sunlit(T0 + 10) and not orbit.sunlit(T0 + 80) and orbit.sunlit(T0 + 110)
    temps = [orbit.temperature_c(T0 + s) for s in range(100)]
    assert -10.01 < min(temps) and max(temps) < 30.01
    hottest = temps.index(max(temps))
    assert 50 < hottest < 65, "the temperature should peak a quarter orbit after the sunlit middle"


def test_replay_writes_a_run_directory(tmp_path):
    log = tmp_path / "rtt.log"
    lines = healthy(T0, 900) + [(T0 + 900, "[INFO|main.c:199]: [+] Built from commit: abc1234")]
    log.write_text("".join(f"{report.iso(t)}\t{text}\n" for t, text in lines))
    info = {"id": "test", "start": T0, "config": "config.toml", "mode": "replay"}
    runner = run.Runner(CONFIG, str(tmp_path), info, echo=False)
    end = run.replay(runner, str(log))
    runner.finish(end)
    events = runner.events.all()
    assert [e["monitor"] for e in events if e["severity"] == "fail"] == ["resets"]
    assert (tmp_path / "summary.md").read_text().count("FAILING") == 1
    assert (tmp_path / "metrics.csv").exists() and (tmp_path / "state.json").exists()
    assert list((tmp_path / "daily").iterdir())
    assert any(name.startswith("rtt-") for name in os.listdir(tmp_path))
