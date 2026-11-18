"""Fixtures for the flatsat hardware-in-the-loop suite. See hil/README.md for the wiring and how to run it.

- tester:  the Grand Central tester over its own J-Link (hil/lib/tester.py); reset before every test
- flatsat: the flight computer over its J-Link: reset, RTT log (hil/lib/flatsat.py). With two J-Links plugged in,
           lib/rtt.py tells them apart (or set HIL_FLATSAT_JLINK / HIL_TESTER_JLINK to their serial numbers)
- logic:   the flatsat's digital outputs, recorded by the tester (hil/lib/logic.py)
- nominal: a benign environment (sunlit photodiodes, 20 C, lit perovskite cells and emulated pixels), armed

A fixture whose hardware is missing skips the tests that use it.
"""

import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "lib"))

import logic as logic_lib  # noqa: E402
import models  # noqa: E402
import rtt  # noqa: E402
from flatsat import ELF, Flatsat  # noqa: E402
from models import nominal_pixel  # noqa: E402
from tester import Tester, TesterError  # noqa: E402

NOMINAL_PHOTODIODE_VOLTS = (1.2, 0.9, 0.6, 0.3)  # Sunlit, distinct per channel
NOMINAL_TEMP_C = 20.0
# Voc of the 4 perovskite cells: distinct, inside the perovskite board's 0-1.2 V sweep, and one 0 V (a dark cell, which
# only sinks current) so both current directions are measured
NOMINAL_PVD_VOCS = (0.9, 0.6, 0.3, 0.0)
DARK = (0.0,) * 4  # For photodiodes= or pvds=


@pytest.fixture(scope="session")
def tester_session():
    try:
        t = Tester()
    except TesterError as e:
        pytest.skip(str(e))
    yield t
    try:
        t.reset()
        t.disarm()
    finally:
        t.close()


@pytest.fixture
def tester(tester_session, request):
    # The flatsat's ADCs and DAC measure relative to its own supply, which its firmware takes to be models.VDD. With the
    # flatsat in the test, the tester's voltages are scaled so that each is that fraction of the flatsat's real supply
    full_scale = tester_session.vdd()
    if "flatsat" in request.fixturenames:
        flatsat_vdd = request.getfixturevalue("flatsat").vdd()
        if flatsat_vdd < 1.0:
            pytest.fail(f"the flatsat's J-Link reads {flatsat_vdd:.2f} V on VTref: is the flatsat powered?")
        full_scale *= models.VDD / flatsat_vdd
    tester_session.set_full_scale(full_scale)
    tester_session.reset()
    yield tester_session
    tester_session.reset()


@pytest.fixture(scope="session")
def flatsat():
    if not os.path.exists(ELF):
        pytest.skip(f"{ELF} not built")
    try:
        flatsat_serial, _ = rtt.probe_serials()
        if flatsat_serial is None:
            raise rtt.ProbeError("only the tester's J-Link is connected")
        f = Flatsat(serial_number=flatsat_serial)
    except Exception as e:  # No J-Link, or it can't reach the target
        pytest.skip(f"flatsat not reachable over J-Link: {e}")
    yield f
    f.close()


@pytest.fixture(autouse=True)
def complete_flatsat_log(request):
    """Fails a test during which the flatsat dropped log lines: its checks may have missed them."""
    if "flatsat" not in request.fixturenames:
        yield
        return
    flatsat = request.getfixturevalue("flatsat")
    mark = flatsat.mark()
    yield
    dropped = flatsat.dropped_lines(mark)
    assert not dropped, f"the flatsat dropped {dropped} log lines (RTT buffer full), so this test's log checks are unreliable"


@pytest.fixture(scope="session")
def logic(tester_session):
    return logic_lib.LogicRecorder(tester_session)


def set_environment(tester, photodiodes=NOMINAL_PHOTODIODE_VOLTS, temps_c=(NOMINAL_TEMP_C,) * 4, pvds=NOMINAL_PVD_VOCS,
                    pixels=True):
    """pvds: the cells on the real perovskite board; pixels: whether the emulated board's pixels are lit."""
    tester.set_photodiodes(photodiodes)
    tester.set_thermistors([models.thermistor_volts(t) if t is not None else models.OPEN_VOLTS for t in temps_c])
    tester.set_pvds(pvds)
    for p in range(16):
        isc, voc = nominal_pixel(p) if pixels else (0.0, 0.0)
        tester.set_pixel(p, isc, voc)
    tester.arm()


def skip_unless_perovskite_board(tester, emulated):
    """Call after a boot (whose device checks address the perovskite board). Skips the test if the other perovskite
    board is the one wired to the flatsat: the emulated one sees I2C traffic (ACKed or, under a fault, NACKed), the
    real one leaves the emulator's counters at 0."""
    pmb = tester.pmb()
    wired = pmb["i2c_transactions"] + pmb["i2c_nacks"] > 0
    if wired != emulated:
        pytest.skip("the tester's emulated perovskite board is wired, not the real one" if wired
                    else "the real perovskite board is wired, not the tester's emulated one")


@pytest.fixture
def nominal(tester):
    set_environment(tester)
    return tester


def hackrf_available():
    if shutil.which("hackrf_info") is None:
        return False
    out = subprocess.run(["hackrf_info"], capture_output=True, text=True)
    return "Found HackRF" in out.stdout


def pytest_collection_modifyitems(config, items):
    skip_hackrf = pytest.mark.skip(reason="no HackRF found (hackrf_info)")
    have_hackrf = None
    for item in items:
        if "hackrf" in item.keywords:
            if have_hackrf is None:
                have_hackrf = hackrf_available()
            if not have_hackrf:
                item.add_marker(skip_hackrf)


__all__ = ["TesterError", "set_environment", "nominal_pixel", "skip_unless_perovskite_board", "DARK"]
