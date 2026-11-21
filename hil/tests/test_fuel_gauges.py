"""The fuel gauges (src/drivers/fuel_gauge, src/tasks/fuel_gauge) against the tester's emulated MAX17205s, one on each
channel of the flatsat's own TCA9546A mux, and the battery level they give the state machine. Needs the emulated gauges
wired in place of the EPS's (hil/README.md); each test skips if the EPS's real ones are wired."""

import re
import time

import pytest

import firmware
import models
from conftest import NOMINAL_PACKS, set_environment, skip_unless_fuel_gauges

PACK = r"fuel_gauge: pack (\d+) SOC=(-?\d+) m%  Vpack=(-?\d+) mV  I=(-?\d+) mA  T=(-?\d+) mC"
LEVEL = r"fuel_gauge: battery level (-?\d+)/1000"
CYCLE_TIMEOUT_S = 30


def read_cycle(flatsat, since, timeout=CYCLE_TIMEOUT_S):
    """The first whole fuel gauge task cycle after `since` (each ends with its battery level line):
    ({pack: (charge m%, mV, mA, mC)}, level in thousandths, the cycle's log lines)."""
    end = time.monotonic() + timeout
    while True:
        lines = [line.text for line in flatsat.log(since)]
        ends = [i for i, text in enumerate(lines) if re.search(LEVEL, text)]
        if len(ends) >= 2:
            cycle = lines[ends[0] + 1:ends[1] + 1]
            packs = {}
            for text in cycle:
                m = re.search(PACK, text)
                if m:
                    packs[int(m.group(1))] = tuple(int(x) for x in m.groups()[1:])
            return packs, int(re.search(LEVEL, cycle[-1]).group(1)), cycle
        if time.monotonic() > end:
            raise TimeoutError(f"no whole fuel gauge cycle within {timeout} s")
        time.sleep(0.2)


def lowest_level(packs):
    """The battery level the task should report: the lowest charge of these packs, in thousandths."""
    return min(soc for soc, _, _, _ in packs) * 10


@pytest.fixture(scope="module")
def nominal_boot(tester_session, flatsat):
    tester_session.reset()
    set_environment(tester_session)
    mark = flatsat.boot()
    skip_unless_fuel_gauges(tester_session, emulated=True)
    return mark, read_cycle(flatsat, mark)


def test_each_pack_reads_its_own_gauge(flatsat, nominal_boot):
    mark, (packs, _, _) = nominal_boot
    assert flatsat.device_checks(mark).get("EPS_ID"), "the fuel gauges failed their startup check"
    tol = models.fuel_gauge_tolerances()
    for pack, (soc, volts, amps, temp_c) in enumerate(NOMINAL_PACKS):
        assert pack in packs, f"pack {pack} wasn't read"
        got_soc, got_mv, got_ma, got_mc = packs[pack]
        assert abs(got_soc - soc * 1000) <= tol["soc"], f"pack {pack}: charge {got_soc} m%, served {soc} %"
        assert abs(got_mv - volts * 1000) <= tol["volts"], f"pack {pack}: {got_mv} mV, served {volts} V"
        assert abs(got_ma - amps * 1000) <= tol["amps"], f"pack {pack}: {got_ma} mA, served {amps} A"
        assert abs(got_mc - temp_c * 1000) <= tol["temp"], f"pack {pack}: {got_mc} mC, served {temp_c} C"


def test_battery_level_is_the_lowest_pack(nominal_boot):
    _, (_, level, _) = nominal_boot
    assert abs(level - lowest_level(NOMINAL_PACKS)) <= 1, f"battery level {level}/1000"


def test_the_flatsat_never_writes_the_gauges(tester_session, nominal_boot):
    """The driver only reads: writes could reach the gauges' nonvolatile memory, which takes few writes in its life."""
    for pack, state in enumerate(tester_session.fuel_gauges()):
        assert state["words_read"] > 0, f"pack {pack} was never read"
        assert state["words_written"] == 0, f"the flatsat wrote {state['words_written']} words to pack {pack}"


def test_a_dead_gauge_is_left_out(nominal, flatsat):
    """Pack 3 has the lowest charge, so the level shows whether its stale or default value leaked in."""
    nominal.fault("fg3", "nack")
    mark = flatsat.boot()
    skip_unless_fuel_gauges(nominal, emulated=True)
    packs, level, cycle = read_cycle(flatsat, mark)
    assert 3 not in packs, "a pack whose gauge doesn't answer was reported"
    assert any(re.search(r"fuel_gauge: pack 3 no data", text) for text in cycle)
    assert sorted(packs) == [0, 1, 2]
    assert abs(level - lowest_level(NOMINAL_PACKS[:3])) <= 1, f"battery level {level}/1000"


def test_with_no_gauge_the_battery_reads_full(nominal, flatsat):
    """A dead fuel gauge subsystem must never force the satellite into low power."""
    for pack in range(models.FG_PACKS):
        nominal.fault(f"fg{pack}", "nack")
    mark = flatsat.boot()
    skip_unless_fuel_gauges(nominal, emulated=True)
    _, level, _ = read_cycle(flatsat, mark)
    assert level == 1000


def test_low_battery_enters_low_power_idle_and_recovers(tester, flatsat):
    low, recovered = firmware.battery_thresholds()
    low_power, idle = firmware.state_ids()["STATE_LOW_POWER_IDLE"], firmware.state_ids()["STATE_IDLE"]
    drained = tuple((low * 100 / 2, 6.8, -0.2, 20.0) for _ in range(models.FG_PACKS))
    set_environment(tester, packs=drained)
    mark = flatsat.boot()
    skip_unless_fuel_gauges(tester, emulated=True)
    flatsat.expect(rf"state_machine: state \d+ -> state {low_power}\b", timeout=60, since=mark)

    charged = tuple(((recovered * 100 + 100) / 2, 7.8, 0.4, 20.0) for _ in range(models.FG_PACKS))
    mark = flatsat.mark()
    for pack, state in enumerate(charged):
        tester.set_pack(pack, *state)
    flatsat.expect(rf"state_machine: state {low_power} -> state {idle}\b", timeout=60, since=mark)


def test_a_hung_gauge_does_not_stop_the_flatsat(nominal, flatsat):
    """A gauge holding SCL low hangs I2C_SENSORS through the mux, which the camera and perovskite board share. The
    flatsat keeps running, and reads every pack again once the gauge lets go."""
    flatsat.boot()
    skip_unless_fuel_gauges(nominal, emulated=True)
    mark = flatsat.mark()
    nominal.fault("fg1", "hold_bus")
    packs, _, _ = read_cycle(flatsat, mark)
    assert 1 not in packs, "pack 1 read while its gauge held the bus"
    read_cycle(flatsat, flatsat.mark())  # Another whole cycle: the task carries on
    assert not flatsat.find_all(r"Built from commit", mark), "the flatsat reset while the bus was held"

    mark = flatsat.mark()
    nominal.fault("fg1", "none")
    flatsat.expect(r"fuel_gauge: pack 1 SOC=", timeout=CYCLE_TIMEOUT_S, since=mark)
    packs, _, _ = read_cycle(flatsat, flatsat.mark())
    assert sorted(packs) == [0, 1, 2, 3], f"only packs {sorted(packs)} read after the bus was released"
