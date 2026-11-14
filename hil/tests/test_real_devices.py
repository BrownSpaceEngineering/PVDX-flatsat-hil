"""The flatsat's real hardware (magnetometer, gyro, MRAM, UHF radio, fuel gauges, display, camera) is present and reads
plausibly with the board sitting still on the bench."""

import math

import pytest

REAL = ["MAGNETOMETER_ID", "GYROSCOPE_ID", "MRAM_ID", "UHF_ID", "EPS_ID", "DISPLAY_ID", "CAMERA_ID"]
SENSOR_LOG = (r"adcs: mag \[([-\d.e+]+),([-\d.e+]+),([-\d.e+]+)\] uT, "
              r"gyro \[([-\d.e+]+),([-\d.e+]+),([-\d.e+]+)\] rad/s")
EARTH_FIELD_UT = (20.0, 70.0)  # Anywhere on the surface, plus some room for the lab
STILL_RAD_S = math.radians(1.0)


@pytest.fixture(scope="module")
def boot_log(tester_session, flatsat):
    from conftest import set_environment

    tester_session.reset()
    set_environment(tester_session)
    mark = flatsat.boot()
    flatsat.collect(r"adcs: mag", 5.0)
    return mark


@pytest.mark.parametrize("device", REAL)
def test_real_device_passes_its_check(flatsat, boot_log, device):
    assert flatsat.device_checks(boot_log).get(device), f"{device} failed its startup check"


def sensor_readings(flatsat, since):
    out = []
    for m in flatsat.find_all(SENSOR_LOG, since):
        values = [float(x) for x in m.groups()]
        out.append((values[:3], values[3:]))
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


def test_fuel_gauges_report_a_battery_level(flatsat, boot_log):
    match = flatsat.expect(r"fuel_gauge: battery level (-?\d+)/1000", timeout=30, since=boot_log)
    level = int(match.group(1))
    assert 0 <= level <= 1000
    assert not flatsat.find_all(r"fuel_gauge: no pack read", boot_log), "no fuel gauge answered"
