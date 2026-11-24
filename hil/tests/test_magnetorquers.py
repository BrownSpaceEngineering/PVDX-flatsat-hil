"""Magnetorquer H-bridge drive (src/drivers/magnetorquer/magnetorquer_driver.c), recorded by the tester.

Each axis has a DRV8837C: IN1 carries the PWM for positive duty, IN2 for negative, both low to coast (both high would
brake), and nSLEEP high only while driving. ADCS drives the coils for ADCS_LOOP_PERIOD_MS (100 ms), then stops them while the magnetometer
measures, so a capture shows bursts of 1 kHz PWM separated by gaps of at least 10 ms. The real magnetometer and gyro
are the inputs, so the duty is whatever B-dot asks for; each burst's duty is compared with the duty ADCS logged.
"""

import re

import pytest

from conftest import set_environment
from logic import both_high_time

AXES = ["x", "y", "z"]
PWM_PERIOD_S = 1e-3  # CONF_TCC0_WAVE_PER_VAL / CONF_TCC1_WAVE_PER_VAL, 1000 us
GAP_S = 5e-3  # Longer than any PWM low time: separates one ADCS step's burst from the next
CAPTURE_S = 4.0
DUTY_TOLERANCE = 0.03
# At 100% duty the driver sets the compare value to the period, which still drops the output for one timer count (8 ns
# at 120 MHz) each period. The DRV8837 can't follow that, so pulses this close together are one pulse.
GLITCH_S = 2e-6
DUTY_LOG = r"adcs: magnetorquer duty \[x,y,z\]: \[(-?\d+),(-?\d+),(-?\d+)\]/1000"  # Thousandths: the log has no %f


def merged_pulses(signal):
    """(start, width) of each pulse, joining pulses separated by less than GLITCH_S."""
    out = []
    for start, width in signal.pulses():
        if out and start - (out[-1][0] + out[-1][1]) < GLITCH_S:
            out[-1] = (out[-1][0], start + width - out[-1][0])
        else:
            out.append((start, width))
    return out


def bursts(in1, in2):
    """Signed duty of each burst of PWM, from the pulses on whichever input is active."""
    pulses = sorted([(s, w, +1) for s, w in merged_pulses(in1)] + [(s, w, -1) for s, w in merged_pulses(in2)])
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
        end = group[-1][0] + group[-1][1]
        out.append((group[0][0], end, sign * duty))
    return out


def pair_by_order(measured, logged):
    """Pairs the k-th burst with the k-th logged duty, at the offset into the log that matches best: the log also
    covers the bursts the capture cut off at either end. Returns [(burst duty, logged duty)]."""
    best = []
    for offset in range(len(logged) - len(measured) + 1):
        pairs = list(zip(measured, logged[offset:]))
        if not best or sum(abs(m - lg) > DUTY_TOLERANCE for m, lg in pairs) < sum(abs(m - lg) > DUTY_TOLERANCE for m, lg in best):
            best = pairs
    return best


@pytest.fixture(scope="module")
def drive_capture(tester_session, flatsat, logic):
    signals = [f"mtq_{axis}_in{n}" for axis in AXES for n in (1, 2)]
    tester_session.reset()
    set_environment(tester_session)
    flatsat.boot()
    flatsat.collect(r"adcs:", 3.0)  # Let ADCS settle into its loop
    slp = [f"mtq_{axis}_slp" for axis in AXES]
    since = flatsat.mark()
    capture = logic.capture(signals + slp, CAPTURE_S)
    duties = [tuple(int(x) / 1000 for x in m.groups()) for m in flatsat.find_all(DUTY_LOG, since)]
    return capture, duties


@pytest.mark.parametrize("axis", AXES)
def test_never_brakes(drive_capture, axis):
    """IN1 and IN2 both high is the DRV8837C's brake, not shoot-through (it protects itself), but the driver means to
    coast: one input at a time. Checked on the levels over the whole capture, so a brake held from before the capture
    starts, or past its end, counts too."""
    capture, _ = drive_capture
    both_high = both_high_time(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])
    assert both_high == 0.0, f"IN1 and IN2 of {axis} were both high (brake) for {both_high * 1e6:.1f} us"


@pytest.mark.parametrize("axis", AXES)
def test_pwm_period(drive_capture, axis):
    capture, _ = drive_capture
    # Rising edges within a burst only: the gaps between bursts aren't PWM periods
    periods = [p for p in (capture[f"mtq_{axis}_in{n}"].period(max_interval=GAP_S) for n in (1, 2)) if p]
    if not periods:
        pytest.skip(f"no PWM on axis {axis} during the capture (B-dot asked for no drive; try the manual test)")
    for period in periods:
        assert abs(period - PWM_PERIOD_S) < 0.02 * PWM_PERIOD_S, f"PWM period {period * 1e3:.3f} ms"


@pytest.mark.parametrize("axis", AXES)
def test_burst_duty_matches_the_logged_duty(drive_capture, axis):
    capture, duties = drive_capture
    index = AXES.index(axis)
    measured = [d for _, _, d in bursts(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])]
    logged = [d[index] for d in duties if abs(d[index]) > 0.005]
    if not measured:
        pytest.skip(f"no PWM bursts on axis {axis} during the capture")
    assert len(logged) >= len(measured), f"{len(measured)} bursts of PWM, but ADCS logged {len(logged)} non-zero duties"
    pairs = pair_by_order(measured, logged)
    wrong = [(round(m, 3), round(lg, 3)) for m, lg in pairs if abs(m - lg) > DUTY_TOLERANCE]
    negated = pair_by_order([-m for m in measured], logged)
    wrong_negated = sum(abs(m - lg) > DUTY_TOLERANCE for m, lg in negated)
    if len(wrong) >= len(pairs) / 2 and wrong_negated <= len(negated) / 4:
        pytest.fail(f"{axis} bursts match their logged duty with the sign flipped ({len(negated) - wrong_negated} of "
                    f"{len(negated)}, against {len(pairs) - len(wrong)} unflipped): mtq_{axis}_in1 and mtq_{axis}_in2 look "
                    "swapped, in the wiring to the tester or on the board")
    assert not wrong, f"(burst duty, logged duty) pairs that differ: {wrong}"


@pytest.mark.parametrize("axis", AXES)
def test_sleep_line_tracks_the_drive(drive_capture, axis):
    """nSLEEP rises before each burst, stays high through it, and drops in the gap before the next one. Two slips the
    DRV8837 doesn't care about are allowed: the driver sets the new duty before raising nSLEEP, and the duty takes
    effect at the next PWM period, which can come within its 30 us wake time (datasheet p6; that first pulse is lost, about 1% of a burst); and on
    stopping it drops nSLEEP while the last period runs out (the driver ignores IN1/IN2 while asleep)."""
    capture, _ = drive_capture
    sleep_line = capture[f"mtq_{axis}_slp"]
    measured = bursts(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])
    if not measured:
        pytest.skip(f"no PWM bursts on axis {axis} during the capture")
    problems = []
    for start, end, duty in measured:
        woke = max((t for t in sleep_line.edges(True) if t <= start), default=None)
        if woke is None:
            problems.append(f"burst at {start:.4f} s: nSLEEP never rose before it")
        elif any(woke < t < end - PWM_PERIOD_S for t in sleep_line.edges(False)):
            problems.append(f"burst at {start:.4f} s (duty {duty:.3f}): nSLEEP dropped during it")
    for (_, end, _), (next_start, _, _) in zip(measured, measured[1:]):
        if not any(end - PWM_PERIOD_S <= t <= next_start for t in sleep_line.edges(False)):
            problems.append(f"nSLEEP stayed high between the bursts at {end:.4f} s and {next_start:.4f} s")
    assert not problems, "; ".join(problems[:10])


@pytest.mark.manual
def test_rotating_the_board_reverses_bdot(tester, flatsat, logic):
    """Rotate the flatsat back and forth about any axis during the 20 s capture: B-dot should drive both directions."""
    signals = [f"mtq_{axis}_in{n}" for axis in AXES for n in (1, 2)]
    flatsat.boot()
    print("\nRotate the flatsat back and forth for the next 20 seconds...")
    capture = logic.capture(signals, 20.0)
    signs = {axis: {d > 0 for _, _, d in bursts(capture[f"mtq_{axis}_in1"], capture[f"mtq_{axis}_in2"])} for axis in AXES}
    assert any(s == {True, False} for s in signs.values()), f"no axis was driven both ways: {signs}"


def test_duty_log_format_is_parsed():
    m = re.search(DUTY_LOG, "adcs: magnetorquer duty [x,y,z]: [125,-500,0]/1000")
    assert m and [int(x) / 1000 for x in m.groups()] == [0.125, -0.5, 0.0]
