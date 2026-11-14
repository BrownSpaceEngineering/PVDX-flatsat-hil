"""Magnetorquer H-bridge drive (src/drivers/magnetorquer/magnetorquer_driver.c), on the logic analyzer.

Each axis has a DRV8837: IN1 carries the PWM for positive duty, IN2 for negative, both low to coast, and nSLEEP high
only while driving. ADCS drives the coils for ADCS_LOOP_PERIOD_MS (100 ms), then stops them while the magnetometer
measures, so a capture shows bursts of 1 kHz PWM separated by gaps of at least 10 ms. The real magnetometer and gyro
are the inputs, so the duty is whatever B-dot asks for; each burst's duty is compared with the duty ADCS logged.
"""

import re

import pytest

from conftest import require_signals, set_environment

AXES = ["x", "y", "z"]
PWM_PERIOD_S = 1e-3  # CONF_TCC0_WAVE_PER_VAL / CONF_TCC1_WAVE_PER_VAL, 1000 us
GAP_S = 5e-3  # Longer than any PWM low time: separates one ADCS step's burst from the next
CAPTURE_S = 4.0
DUTY_TOLERANCE = 0.02
DUTY_LOG = r"adcs: magnetorquer duty \[x,y,z\]: \[([-\d.e+]+),([-\d.e+]+),([-\d.e+]+)\]"


def overlap(a, b):
    """Total time both signals are high (both pulse lists are in time order)."""
    pa, pb = a.pulses(), b.pulses()
    total, i, j = 0.0, 0, 0
    while i < len(pa) and j < len(pb):
        a_start, a_end = pa[i][0], pa[i][0] + pa[i][1]
        b_start, b_end = pb[j][0], pb[j][0] + pb[j][1]
        total += max(0.0, min(a_end, b_end) - max(a_start, b_start))
        if a_end < b_end:
            i += 1
        else:
            j += 1
    return total


def bursts(in1, in2):
    """Signed duty of each burst of PWM, from the pulses on whichever input is active."""
    pulses = sorted([(s, w, +1) for s, w in in1.pulses()] + [(s, w, -1) for s, w in in2.pulses()])
    groups = []
    for start, width, sign in pulses:
        if groups and start - (groups[-1][-1][0] + groups[-1][-1][1]) < GAP_S:
            groups[-1].append((start, width, sign))
        else:
            groups.append([(start, width, sign)])
    out = []
    for group in groups[1:-1]:  # The first and last may be cut off by the capture
        sign = group[0][2]
        widths = [w for _, w, _ in group]
        duty = min(1.0, sum(widths) / len(widths) / PWM_PERIOD_S)
        out.append((group[0][0], sign * duty))
    return out


@pytest.fixture(scope="module")
def drive_capture(tester_session, flatsat, logic):
    signals = [f"mtq_{axis}_in{n}" for axis in AXES for n in (1, 2)]
    require_signals(logic, *signals)
    tester_session.reset()
    set_environment(tester_session)
    flatsat.boot()
    flatsat.collect(r"adcs:", 3.0)  # Let ADCS settle into its loop
    slp = [s for s in (f"mtq_{axis}_slp" for axis in AXES) if s in logic.channel_map]
    since = flatsat.mark()
    capture = logic.capture(signals + slp, CAPTURE_S, samplerate=1_000_000)
    duties = [tuple(float(x) for x in m.groups()) for m in flatsat.find_all(DUTY_LOG, since)]
    return capture, duties, slp


@pytest.mark.parametrize("axis", AXES)
def test_no_shoot_through(drive_capture, axis):
    capture, _, _ = drive_capture
    both_high = overlap(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])
    assert both_high == 0.0, f"IN1 and IN2 of {axis} were both high for {both_high * 1e6:.1f} us"


@pytest.mark.parametrize("axis", AXES)
def test_pwm_period(drive_capture, axis):
    capture, _, _ = drive_capture
    periods = [p for p in (capture[f"mtq_{axis}_in{n}"].period() for n in (1, 2)) if p]
    if not periods:
        pytest.skip(f"no PWM on axis {axis} during the capture (B-dot asked for no drive; try the manual test)")
    for period in periods:
        assert abs(period - PWM_PERIOD_S) < 0.02 * PWM_PERIOD_S, f"PWM period {period * 1e3:.3f} ms"


@pytest.mark.parametrize("axis", AXES)
def test_burst_duty_matches_the_logged_duty(drive_capture, axis):
    capture, duties, _ = drive_capture
    index = AXES.index(axis)
    measured = bursts(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])
    logged = [d[index] for d in duties if abs(d[index]) > 0.005]
    if not measured:
        pytest.skip(f"no PWM bursts on axis {axis} during the capture")
    assert logged, "PWM on the coil, but ADCS logged no non-zero duty for it"
    unmatched = [round(d, 3) for _, d in measured if not any(abs(d - lg) <= DUTY_TOLERANCE for lg in logged)]
    assert not unmatched, f"burst duties {unmatched} match no logged duty (logged: {[round(x, 3) for x in logged]})"


@pytest.mark.parametrize("axis", AXES)
def test_sleep_line_tracks_the_drive(drive_capture, axis):
    capture, _, slp = drive_capture
    name = f"mtq_{axis}_slp"
    if name not in slp:
        pytest.skip(f"{name} not mapped in hil/logic_channels.json")
    sleep_line = capture[name]
    for start, duty in bursts(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"]):
        assert sleep_line.level_at(start + 1e-4) == 1, f"nSLEEP low while driving at {start:.4f} s (duty {duty:.3f})"


@pytest.mark.manual
def test_rotating_the_board_reverses_bdot(tester, flatsat, logic):
    """Rotate the flatsat back and forth about any axis during the 20 s capture: B-dot should drive both directions."""
    signals = [f"mtq_{axis}_in{n}" for axis in AXES for n in (1, 2)]
    require_signals(logic, *signals)
    flatsat.boot()
    print("\nRotate the flatsat back and forth for the next 20 seconds...")
    capture = logic.capture(signals, 20.0, samplerate=1_000_000)
    signs = {axis: {d > 0 for _, d in bursts(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])} for axis in AXES}
    assert any(s == {True, False} for s in signs.values()), f"no axis was driven both ways: {signs}"


def test_duty_log_format_is_parsed():
    assert re.search(DUTY_LOG, "adcs: magnetorquer duty [x,y,z]: [0.125000,-0.500000,0.000000]")
