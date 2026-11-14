"""Perovskite IV sweep against the emulated perovskite board (MCP23017 + ADG734 routing, PVD DAC, INA226).

A sweep starts about 30 s after boot: IDLE goes to FAKE_HEARTBEAT, whose mocked sunlight sends IDLE on to
PEROVSKITE_CAPTURE. It steps the DAC through PEROVSKITE_STEP_COUNT voltages for each of the 16 pixels and logs every
step. The tester logs every CURRENT register read with the pixel routed at the time and the follower voltage it
measured from PVD_CRNT, so each logged step can be checked against what the board actually served.
"""

import time

import pytest

import models
from conftest import nominal_pixel, set_environment

pytestmark = pytest.mark.slow

PIXELS = 16
STEPS = 64  # PEROVSKITE_STEP_COUNT
START_TIMEOUT_S = 120
SWEEP_TIMEOUT_S = 300
FOLLOWER_TOLERANCE_MV = 40  # Flatsat DAC + tester ADC error
STEP_LOG = r"perovskite: px\s+(\d+) step\s+(\d+) set\s+(\d+) mV I\s+(-?\d+) nA"


def run_sweep(tester, flatsat):
    """Boots, waits for one sweep, and returns (flatsat step lines, tester current reads, log mark)."""
    mark = flatsat.boot()
    flatsat.expect(r"perovskite: Starting sweep", timeout=START_TIMEOUT_S, since=mark)
    served = []
    index = 0
    end = time.monotonic() + SWEEP_TIMEOUT_S
    done = None
    while time.monotonic() < end:
        entries, index = tester.pmb_log(index)  # The tester keeps only the last 256 reads, so drain as it goes
        served += entries
        done = flatsat.find_all(r"perovskite: (Sweep #\d+ done|Every step failed|Hardware initialization failed)", mark)
        if done:
            entries, index = tester.pmb_log(index)
            served += entries
            break
        time.sleep(2.0)
    assert done, f"no sweep finished within {SWEEP_TIMEOUT_S} s"
    steps = [tuple(int(x) for x in m.groups()) for m in flatsat.find_all(STEP_LOG, mark)]
    return steps, served, mark


def test_sweep_matches_what_the_board_served(tester, flatsat):
    set_environment(tester)
    steps, served, mark = run_sweep(tester, flatsat)

    assert len(steps) == PIXELS * STEPS, f"{len(steps)} steps logged"
    assert [px for px, *_ in steps] == [p for p in range(PIXELS) for _ in range(STEPS)], "pixels out of order"
    assert len(served) == len(steps), f"flatsat logged {len(steps)} steps, tester served {len(served)} current reads"

    problems = []
    for (px, step, set_mv, current_na), (selected, follow_mv, served_na) in zip(steps, served):
        if selected != 1 << px:
            problems.append(f"px {px} step {step}: routed mask 0x{selected:04x}")
        elif current_na != served_na:
            problems.append(f"px {px} step {step}: logged {current_na} nA, served {served_na} nA")
        elif abs(follow_mv - set_mv) > FOLLOWER_TOLERANCE_MV:
            problems.append(f"px {px} step {step}: set {set_mv} mV, follower measured {follow_mv} mV")
    assert not problems, f"{len(problems)} bad steps, first ones: {problems[:10]}"

    # Short-circuit current at step 0 identifies each pixel end to end
    for px in range(PIXELS):
        isc_ua, voc_mv = nominal_pixel(px)
        _, _, _, current_na = steps[px * STEPS]
        assert abs(current_na - isc_ua * 1000) < 0.03 * isc_ua * 1000 + 500, f"px {px}: Isc {current_na} nA, set {isc_ua} uA"

    pmb = tester.pmb()["mcp23017"]
    assert pmb["multi_select"] == 0, "more than one pixel was routed to measurement at once"
    assert pmb["visited_mask"] == 0xFFFF
    assert not flatsat.find_all(r"perovskite: Sweep not saved", mark)


def test_model_agrees_with_the_tester():
    """The host's pixel model (used to read results) matches tester/src/pixel_model.c at a few points."""
    assert abs(models.pixel_current_na(100, 900, 0) - 100_000) < 1
    assert abs(models.pixel_current_na(100, 900, 900)) < 1  # Open circuit at Voc
    assert models.pixel_current_na(100, 900, 950) < 0  # Past Voc the pixel sinks current


def test_ina226_never_ready_marks_steps_invalid(tester, flatsat):
    set_environment(tester)
    tester.fault("ina226", "no_cvrf")
    _, _, mark = run_sweep(tester, flatsat)
    assert flatsat.find_all(r"perovskite: px \d+ step \d+ failed", mark)
    assert flatsat.find_all(r"perovskite: Every step failed, not keeping the sweep", mark)
    assert len(flatsat.find_all(r"Built from commit", mark)) == 1, "the flatsat reset during the sweep"


def test_stuck_switch_latch_is_caught(tester, flatsat):
    set_environment(tester)
    tester.fault("mcp23017", "stuck")
    _, _, mark = run_sweep(tester, flatsat)
    assert flatsat.find_all(r"perovskite: (Failed to select pixel|Hardware initialization failed)", mark)
    assert len(flatsat.find_all(r"Built from commit", mark)) == 1
