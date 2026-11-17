"""Perovskite IV sweep on the flatsat's perovskite measurement board (PVDX-ADCS-PMB), with tester cells on some pixels.

A sweep starts about 30 s after boot: IDLE goes to FAKE_HEARTBEAT, whose mocked sunlight sends IDLE on to
PEROVSKITE_CAPTURE. It steps the DAC through PEROVSKITE_STEP_COUNT voltages for each of the 16 pixels and logs every
step.

Each tester cell (pvd0-3, on tester.PVD_PIXELS) is a PWM output behind a series resistor; the INA226's integrating conversions average
the PWM, so it acts like a cell with a straight-line IV curve (Voc set by the tester, Isc = Voc / R). Its logged currents
must follow that line at the voltages the flatsat says it applied, and the pixels without a cell read open.
"""

import time

import numpy as np
import pytest

from conftest import DARK, NOMINAL_PVD_VOCS, set_environment
from tester import PVD_PIXELS, PVD_SERIES_OHMS

pytestmark = pytest.mark.slow

PIXELS = 16
STEPS = 64  # PEROVSKITE_STEP_COUNT
START_TIMEOUT_S = 120
SWEEP_TIMEOUT_S = 300
STEP_LOG = r"perovskite: px\s+(\d+) step\s+(\d+) set\s+(\d+) mV I\s+(-?\d+) nA"
CELL_TOLERANCE = 0.05  # Series resistor tolerance, PWM averaging and INA226 gain
CELL_OFFSET_NA = 2000  # INA226 offset, and the follower sitting a few mV off 0 V while sinking at step 0
OPEN_PIXEL_NA = 2000  # A pixel with nothing on it
PIXEL_FULL_SCALE_MV = 1200  # Most a pixel can get: 3.3 V through the PMB's 21k/12k divider


def run_sweep(flatsat):
    """Boots, waits for one sweep, and returns (flatsat step lines, log mark)."""
    mark = flatsat.boot()
    flatsat.expect(r"perovskite: Starting sweep", timeout=START_TIMEOUT_S, since=mark)
    flatsat.expect(r"perovskite: (Sweep #\d+ done|Every step failed|Hardware initialization failed)",
                   timeout=SWEEP_TIMEOUT_S, since=mark)
    steps = [tuple(int(x) for x in m.groups()) for m in flatsat.find_all(STEP_LOG, mark)]
    return steps, mark


def fit_line(steps):
    """Least-squares current (nA) = intercept + slope * set voltage (mV), and the worst residual."""
    set_mv = np.array([s for _, _, s, _ in steps], dtype=float)
    current_na = np.array([i for _, _, _, i in steps], dtype=float)
    slope, intercept = np.polyfit(set_mv, current_na, 1)
    return slope, intercept, np.max(np.abs(current_na - (intercept + slope * set_mv)))


def test_sweep_measures_the_tester_cells(tester, flatsat):
    """Each cell pixel reads I = (Voc - V) / R across the sweep, at the V the flatsat says it applied; every other
    pixel reads open. A cell on the wrong pixel shows up as the wrong Voc, and a wrong pixel voltage (such as a
    divider other than the one pvd_dac.h assumes) as the wrong slope."""
    set_environment(tester)
    steps, mark = run_sweep(flatsat)

    assert len(steps) == PIXELS * STEPS, f"{len(steps)} steps logged"
    assert [px for px, *_ in steps] == [p for p in range(PIXELS) for _ in range(STEPS)], "pixels out of order"
    ohms = PVD_SERIES_OHMS
    problems = []
    for px in range(PIXELS):
        px_steps = steps[px * STEPS:(px + 1) * STEPS]
        if px not in PVD_PIXELS:
            worst = max(abs(i) for *_, i in px_steps)
            if worst > OPEN_PIXEL_NA:
                problems.append(f"px {px} (open): up to {worst} nA")
            continue
        voc_mv = NOMINAL_PVD_VOCS[PVD_PIXELS.index(px)] * 1000
        slope, intercept, residual = fit_line(px_steps)
        want_slope, want_isc = -1e6 / ohms, voc_mv * 1e6 / ohms  # nA per mV, nA
        if abs(slope / want_slope - 1) > CELL_TOLERANCE:
            problems.append(f"px {px}: slope {slope:.1f} nA/mV, expected {want_slope:.1f}: the pixel gets "
                            f"{slope / want_slope:.3f}x the voltage the flatsat logs")
        if abs(intercept - want_isc) > CELL_TOLERANCE * want_isc + CELL_OFFSET_NA:
            problems.append(f"px {px}: Isc {intercept:.0f} nA, expected {want_isc:.0f} nA (Voc {voc_mv:.0f} mV)")
        if residual > CELL_TOLERANCE * PIXEL_FULL_SCALE_MV * 1e6 / ohms + CELL_OFFSET_NA:
            problems.append(f"px {px}: not a straight line, a step is {residual:.0f} nA off the fit")
    assert not problems, "; ".join(problems)
    assert not flatsat.find_all(r"perovskite: Sweep not saved", mark)


def test_dark_cells_only_sink_current(tester, flatsat):
    """With every cell at 0 V the pixels are plain resistors to ground, so no step reads current flowing out."""
    set_environment(tester, pvds=DARK)
    steps, _ = run_sweep(flatsat)
    sourcing = [(px, step, i) for px, step, _, i in steps if px in PVD_PIXELS and i > CELL_OFFSET_NA]
    assert not sourcing, f"dark cells sourced current (px, step, nA): {sourcing[:10]}"
