"""Thermistor readings and the bang-bang heater control (src/drivers/thermistor/thermistor_driver.c).

Heater 1 follows thermistors 0 and 1, heater 2 follows 2 and 3. It turns on at or below HEATER_ON_C and off at or above
HEATER_OFF_C, and keeps its state in between. A thermistor that reads open, or far from the others, is replaced by its
pair partner. The heater lines are checked on the logic analyzer when heater1/heater2 are mapped, and in the log.
"""

import pytest

import models
from conftest import require_signals, set_environment

THERMISTOR_TASK_TODO = pytest.mark.xfail(
    reason="no state enables the thermistor task yet, so it never runs (TODO in src/states.c)", strict=False)
TASK_PERIOD_S = 2.0
CHECK_TOLERANCE_C = 1.5


def test_device_check_reads_the_simulated_temperatures(tester, flatsat):
    temps = [-10.0, 5.0, 20.0, 40.0]
    set_environment(tester, temps_c=temps)
    mark = flatsat.boot()
    logged = {int(m.group(1)): int(m.group(2)) / 1000.0
              for m in flatsat.find_all(r"thermistor check: T(\d) code \d+ -> (-?\d+) mC", mark)}
    assert sorted(logged) == [0, 1, 2, 3], f"thermistor check logged {logged}"
    errors = {i: round(logged[i] - t, 2) for i, t in enumerate(temps) if abs(logged[i] - t) > CHECK_TOLERANCE_C}
    assert not errors, f"thermistors off by more than {CHECK_TOLERANCE_C} C: {errors}"
    assert flatsat.device_checks(mark).get("THERMISTOR_ID")


def test_open_thermistor_fails_the_device_check(tester, flatsat):
    set_environment(tester, temps_c=(20.0, None, 20.0, 20.0))
    mark = flatsat.boot()
    assert flatsat.find_all(r"thermistor check: T1 code \d+ railed", mark)
    assert flatsat.device_checks(mark).get("THERMISTOR_ID") is False


def heater_states(flatsat, since):
    return [(m.group(1) == "ON", m.group(2) == "ON") for m in flatsat.find_all(r"thermistor: heater1=(ON|OFF) heater2=(ON|OFF)", since)]


def settle(tester, flatsat, temps):
    tester.set_thermistors([models.thermistor_volts(t) if t is not None else models.OPEN_VOLTS for t in temps])
    since = flatsat.mark()
    flatsat.collect(r"thermistor: heater1", 2.5 * TASK_PERIOD_S)
    states = heater_states(flatsat, since)
    assert states, "the thermistor task logged no heater state"
    return states[-1]


@THERMISTOR_TASK_TODO
def test_heaters_follow_temperature_with_hysteresis(tester, flatsat):
    set_environment(tester, temps_c=(20.0,) * 4)
    flatsat.boot()
    between = (models.HEATER_ON_C + models.HEATER_OFF_C) / 2
    assert settle(tester, flatsat, (20.0,) * 4) == (False, False)
    assert settle(tester, flatsat, (0.0, 0.0, 20.0, 20.0)) == (True, False)
    assert settle(tester, flatsat, (between, between, 20.0, 20.0)) == (True, False), "hysteresis: stays on in between"
    assert settle(tester, flatsat, (10.0, 10.0, 0.0, 0.0)) == (False, True)
    assert settle(tester, flatsat, (between, between, between, between)) == (False, True), "hysteresis: stays as it was"


@THERMISTOR_TASK_TODO
def test_open_thermistor_is_replaced_by_its_partner(tester, flatsat):
    set_environment(tester, temps_c=(20.0,) * 4)
    flatsat.boot()
    mark = flatsat.mark()
    assert settle(tester, flatsat, (None, 0.0, 20.0, 20.0)) == (True, False)
    assert flatsat.find_all(r"thermistor: idx=0 code \d+ out of range", mark)


@THERMISTOR_TASK_TODO
def test_heater_line_matches_the_logged_state(tester, flatsat, logic):
    require_signals(logic, "heater1")
    set_environment(tester, temps_c=(0.0, 0.0, 20.0, 20.0))
    flatsat.boot()
    pending = logic.start(["heater1"], seconds=3 * TASK_PERIOD_S, samplerate=100_000)
    capture = pending.wait()
    assert capture["heater1"].duty() > 0.99, "heater 1 should be on for the whole capture"
