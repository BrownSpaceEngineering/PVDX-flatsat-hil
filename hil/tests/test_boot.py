"""Startup device checks with the S-band radio and perovskite board emulated, and how boot copes when they misbehave."""

import pytest

from conftest import set_environment

EMULATED = ["SBAND_ID", "PEROVSKITE_ID", "PHOTODIODE_ID", "THERMISTOR_ID"]


def booted_to_scheduler(flatsat, since, timeout=30):
    """True once the boot after `since` has reached the task manager (the OS is up), without a further reset."""
    flatsat.expect(r"Cosmic Monkey Task initialized|TaskManager initialized", timeout=timeout, since=since)
    return len(flatsat.find_all(r"Built from commit", since)) == 1


def test_emulated_devices_pass_their_checks(nominal, flatsat):
    mark = flatsat.boot()
    checks = flatsat.device_checks(mark)
    failed = [d for d in EMULATED if not checks.get(d)]
    assert not failed, f"device checks failed: {failed} (all: {checks})"


@pytest.mark.parametrize(
    "mode, warning",
    [("nack", r"stxg2: no answer on I2C"), ("bad_id", r"stxg2: bad ID 0xdeadbeef"), ("hold_bus", r"stxg2:")],
)
def test_sband_fault_fails_its_check_and_boot_continues(nominal, flatsat, mode, warning):
    nominal.fault("stxg2", mode)
    mark = flatsat.reset()
    flatsat.expect(r"AT_LEAST_ONE_DEVICE_FAILED: 1", timeout=60, since=mark)
    assert flatsat.device_checks(mark).get("SBAND_ID") is False
    assert flatsat.find_all(warning, mark), f"no log line matching {warning!r}"
    assert booted_to_scheduler(flatsat, mark), "the flatsat reset during boot"


@pytest.mark.parametrize(
    "device, mode, warning",
    [
        ("ina226", "nack", r"perovskite check: INA226 did not ACK"),
        ("mcp23017", "nack", r"perovskite check: MCP23017 did not ACK"),
        ("ina226", "bad_id", r"perovskite check: INA226 ID mfg=0x1234"),
    ],
)
def test_perovskite_board_fault_fails_its_check(nominal, flatsat, device, mode, warning):
    nominal.fault(device, mode)
    mark = flatsat.boot()
    assert flatsat.device_checks(mark).get("PEROVSKITE_ID") is False
    assert flatsat.find_all(warning, mark)
    assert booted_to_scheduler(flatsat, mark)


def test_hung_sensor_bus_does_not_hang_the_flatsat(nominal, flatsat):
    """A perovskite-board device holding SCL low hangs I2C_SENSORS, which the real fuel gauges and camera share."""
    nominal.fault("pmb_bus", "hold_bus")
    mark = flatsat.reset()
    flatsat.expect(r"AT_LEAST_ONE_DEVICE_FAILED", timeout=90, since=mark)
    assert flatsat.device_checks(mark).get("PEROVSKITE_ID") is False
    assert booted_to_scheduler(flatsat, mark, timeout=60), "the flatsat reset while the sensor bus was held"

    # Releasing the bus lets the flatsat use it again
    nominal.fault("pmb_bus", "none")
    flatsat.expect(r"fuel_gauge: battery level", timeout=30, since=flatsat.mark())


def test_dark_and_cold_environment_boots(tester, flatsat):
    """Eclipse at -20 C with dark pixels: photodiodes only warn, and nothing stops the boot."""
    set_environment(tester, sun=None, temps_c=(-20.0,) * 4, pixels=False)
    mark = flatsat.boot()
    checks = flatsat.device_checks(mark)
    assert checks.get("PHOTODIODE_ID"), "a dark array should pass its check (eclipse is normal)"
    assert flatsat.find_all(r"photodiode check: no channel above the light floor", mark)
    assert booted_to_scheduler(flatsat, mark)
