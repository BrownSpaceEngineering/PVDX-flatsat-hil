"""Fixtures for the flatsat hardware-in-the-loop suite. See hil/README.md for the wiring and how to run it.

- tester:  the Grand Central tester over its own J-Link (hil/lib/tester.py); reset before every test
- flatsat: the flight computer over its J-Link: reset, RTT log (hil/lib/flatsat.py). With two J-Links plugged in,
           lib/rtt.py tells them apart (or set HIL_FLATSAT_JLINK / HIL_TESTER_JLINK to their serial numbers)
- logic:   the logic analyzer (hil/lib/logic.py); use require_signals() to skip when a signal isn't wired
- nominal: a benign environment (sunlit, 20 C, lit perovskite pixels), armed

A fixture whose hardware is missing skips the tests that use it, so `pytest hil -m selftest` runs with only the tester.
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
from tester import Tester, TesterError  # noqa: E402

NOMINAL_SUN = (1.0, 0.35, 0.25)
NOMINAL_TEMP_C = 20.0


def nominal_pixel(pixel):
    """Distinct, plausible IV parameters per pixel, so a mixed-up pixel shows in the currents."""
    return 40.0 + 8.0 * pixel, 850.0 + 10.0 * pixel  # Isc uA, Voc mV


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
def tester(tester_session):
    tester_session.reset()
    yield tester_session
    tester_session.reset()


@pytest.fixture(scope="session")
def flatsat():
    if not os.path.exists(ELF):
        pytest.skip(f"{ELF} not built")
    try:
        flatsat_serial, _ = rtt.probe_serials()
        f = Flatsat(serial_number=flatsat_serial)
    except Exception as e:  # No J-Link, or it can't reach the target
        pytest.skip(f"flatsat not reachable over J-Link: {e}")
    yield f
    f.close()


@pytest.fixture(scope="session")
def logic():
    return logic_lib.LogicAnalyzer()


def require_signals(logic, *signals):
    missing = logic.missing(*signals)
    if missing:
        pytest.skip(f"logic analyzer channels not mapped in hil/logic_channels.json: {', '.join(missing)}")
    if not logic.available():
        pytest.skip("no sigrok-cli or logic analyzer (close PulseView if it holds the device)")


def set_environment(tester, sun=NOMINAL_SUN, temps_c=(NOMINAL_TEMP_C,) * 4, pixels=True):
    tester.set_photodiodes(models.photodiode_volts(sun) if sun is not None else [0.0] * 18)
    tester.set_thermistors([models.thermistor_volts(t) if t is not None else models.OPEN_VOLTS for t in temps_c])
    for p in range(16):
        isc, voc = nominal_pixel(p) if pixels else (0.0, 0.0)
        tester.set_pixel(p, isc, voc)
    tester.arm()


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


__all__ = ["TesterError", "require_signals", "set_environment", "nominal_pixel"]
