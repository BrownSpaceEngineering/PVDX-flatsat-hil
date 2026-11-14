"""Photodiode inputs: each channel reads its own voltage, and ADCS estimates the simulated sun direction from them.

The tester drives the photodiodes with raw PWM. The flatsat averages only 4 instantaneous ADC samples, so unless its
inputs are filtered the readings jump in steps of about 25% of the PWM voltage. If test_each_channel_reads_its_own_voltage
fails with large, scattered errors, fit one capacitor per channel after the tester's series resistor (hil/README.md).
"""

import pytest

import firmware
import models
from conftest import set_environment

VDD = 3.3
CHANNEL_TOLERANCE_V = 0.12
SUN_ANGLE_TOLERANCE_DEG = 5.0  # Firmware estimate vs. the same estimator in Python on the voltages applied


def logged_photodiode_volts(flatsat, since):
    codes = {int(m.group(1)): int(m.group(2)) for m in flatsat.find_all(r"photodiode check: PHD_(\d+) code (\d+)", since)}
    assert len(codes) == 18, f"expected 18 photodiode codes in the boot log, found {sorted(codes)}"
    return [codes[i] / 4095.0 * VDD for i in range(18)]


@pytest.mark.parametrize("ramp", ["rising", "falling"])
def test_each_channel_reads_its_own_voltage(tester, flatsat, ramp):
    """A distinct voltage on every channel catches swapped wires and PHD_CFG table mistakes; two ramps catch stuck ones."""
    volts = [0.15 + 0.14 * i for i in range(18)]
    if ramp == "falling":
        volts.reverse()
    set_environment(tester)
    volts = tester.set_photodiodes(volts)  # 0 V where this tester doesn't drive the channel
    mark = flatsat.boot()
    measured = logged_photodiode_volts(flatsat, mark)
    errors = {f"PHD_{i}": round(m - v, 3) for i, (v, m) in enumerate(zip(volts, measured)) if abs(m - v) > CHANNEL_TOLERANCE_V}
    assert not errors, f"channels off by more than {CHANNEL_TOLERANCE_V} V (measured - set): {errors}"


def sun_vectors(flatsat, since, in_sun=True):
    pattern = r"adcs: photodiode sun vector \[([-\d.e+]+),([-\d.e+]+),([-\d.e+]+)\], in sun: (\d)"
    return [(float(m.group(1)), float(m.group(2)), float(m.group(3))) for m in flatsat.find_all(pattern, since)
            if m.group(4) == ("1" if in_sun else "0")]


SUN_DIRECTIONS = [
    (1.0, 0.0, 0.0),
    (-1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, 0.0, -1.0),
    (1.0, 1.0, 1.0),
    (-0.6, 0.3, -0.75),
]


@pytest.fixture(scope="module")
def running_flatsat(tester_session, flatsat):
    """One boot for the sun-direction tests, which only change the photodiode voltages."""
    tester_session.reset()
    set_environment(tester_session)
    flatsat.boot()
    return flatsat


@pytest.mark.parametrize("sun", SUN_DIRECTIONS, ids=lambda s: "sun" + "_".join(f"{x:+.1f}" for x in s))
def test_adcs_estimates_the_sun_direction(tester_session, running_flatsat, sun):
    """The flatsat's estimate matches the estimator run here on the voltages actually applied, which (with all 18
    photodiodes driven) is the simulated sun."""
    applied = tester_session.set_photodiodes(models.photodiode_volts(sun))
    in_sun, expected = models.estimate_sun_vector(applied)
    if not in_sun:
        pytest.skip(f"too few of this tester's photodiodes are lit by {sun} for an estimate")
    since = running_flatsat.mark()
    running_flatsat.collect(r"adcs: photodiode sun vector", 3.0)  # Settle a few ADCS steps
    estimates = sun_vectors(running_flatsat, since)[-5:]
    assert estimates, "ADCS logged no in-sun estimate (is this a DEVBUILD with debug logs?)"
    angles = [models.angle_deg(e, expected) for e in estimates]
    assert max(angles) < SUN_ANGLE_TOLERANCE_DEG, f"estimates {estimates} are {angles} degrees off {expected}"


def test_eclipse_reads_as_not_in_sun(tester_session, running_flatsat):
    tester_session.set_photodiodes([0.0] * 18)
    since = running_flatsat.mark()
    running_flatsat.collect(r"adcs: photodiode sun vector", 3.0)
    lines = running_flatsat.find_all(r"adcs: photodiode sun vector .*in sun: (\d)", since)[-5:]
    assert lines and all(m.group(1) == "0" for m in lines)


@pytest.mark.xfail(reason="ADCS doesn't report SUNLIGHT_STATUS_UPDATE yet (TODO in src/tasks/adcs/adcs_task.c)", strict=False)
def test_sunlight_starts_a_perovskite_sweep(tester, flatsat):
    """Sunlight alone (without the heartbeat task's mocked inputs) should move IDLE to PEROVSKITE_CAPTURE."""
    states = firmware.state_ids()
    set_environment(tester, sun=None)
    mark = flatsat.boot()
    flatsat.expect(firmware.TRANSITION, timeout=30, since=mark)  # Entered IDLE
    tester.set_photodiodes(models.photodiode_volts((1.0, 0.2, 0.1)))
    since = flatsat.mark()
    match = flatsat.expect(firmware.TRANSITION, timeout=15, since=since)
    assert int(match.group(2)) == states["STATE_PEROVSKITE_CAPTURE"], match.group(0)
