"""The flatsat's real hardware (magnetometer, gyro, MRAM, UHF radio, camera, and the EPS's fuel gauges unless the
tester's emulated ones are wired) is present and reads plausibly with the board sitting still on the bench. The display
has no device check: it is write-only over SPI, so nothing can show it is there."""

import math

import pytest

REAL = ["MAGNETOMETER_ID", "GYROSCOPE_ID", "MRAM_ID", "UHF_ID", "CAMERA_ID"]
# Logged every ADCS_SENSOR_LOG_STEPS steps (about 5 s), in nT and mrad/s: the log has no %f
SENSOR_LOG = r"adcs: mag \[(-?\d+),(-?\d+),(-?\d+)\] nT, gyro \[(-?\d+),(-?\d+),(-?\d+)\] mrad/s"
SENSOR_LOG_S = 15.0  # Long enough for a few of them
EARTH_FIELD_UT = (20.0, 70.0)  # Anywhere on the surface, plus some room for the lab
STILL_RAD_S = math.radians(1.0)


@pytest.fixture(scope="module")
def boot_log(tester_session, flatsat):
    from conftest import set_environment

    tester_session.reset()
    set_environment(tester_session)
    mark = flatsat.boot()
    flatsat.collect(r"adcs: mag", SENSOR_LOG_S)
    return mark


@pytest.mark.parametrize("device", REAL)
def test_real_device_passes_its_check(flatsat, boot_log, device):
    assert flatsat.device_checks(boot_log).get(device), f"{device} failed its startup check"


def sensor_readings(flatsat, since):
    out = []
    for m in flatsat.find_all(SENSOR_LOG, since):
        values = [int(x) for x in m.groups()]
        out.append(([v / 1000 for v in values[:3]], [v / 1000 for v in values[3:]]))  # uT, rad/s
    return out


def test_magnetometer_sees_the_earth_field(flatsat, boot_log):
    readings = sensor_readings(flatsat, boot_log)
    assert readings, "no 'adcs: mag' debug lines (is this a DEVBUILD?)"
    magnitudes = [math.sqrt(sum(x * x for x in mag)) for mag, _ in readings[-10:]]
    lo, hi = EARTH_FIELD_UT
    assert all(lo <= m <= hi for m in magnitudes), f"field magnitudes {magnitudes} uT"


def test_gyro_reads_still(flatsat, boot_log):
    readings = sensor_readings(flatsat, boot_log)
    assert readings
    rates = [math.sqrt(sum(x * x for x in gyro)) for _, gyro in readings[-10:]]
    assert all(r < STILL_RAD_S for r in rates), f"rates {rates} rad/s with the board still"


def test_real_fuel_gauges_pass_their_check(tester_session, flatsat, boot_log):
    from conftest import skip_unless_fuel_gauges

    skip_unless_fuel_gauges(tester_session, emulated=False)
    assert flatsat.device_checks(boot_log).get("EPS_ID"), "EPS_ID failed its startup check"


def test_fuel_gauges_report_a_battery_level(tester_session, flatsat, boot_log):
    from conftest import skip_unless_fuel_gauges

    skip_unless_fuel_gauges(tester_session, emulated=False)
    match = flatsat.expect(r"fuel_gauge: battery level (-?\d+)/1000", timeout=30, since=boot_log)
    level = int(match.group(1))
    assert 0 <= level <= 1000
    assert not flatsat.find_all(r"fuel_gauge: no pack read", boot_log), "no fuel gauge answered"
