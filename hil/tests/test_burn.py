"""Burn wire sequence in main.c, with the burn pin recorded by the tester.

The current firmware forces burn_record.burned = true and has the BURN pin writes commented out (both marked
BEFORE-FLIGHT), so in an ordinary build the pin must stay low. To test the sequence itself, undo those two and build
with a short wait:

    make dev DEFINES="BURN_SEQUENCE_FIRE_BURN_WIRE BURN_WAIT_MINUTES=2 BURN_ON_FOR_SECONDS=5"

then flash it with `make flash_to_mram`. The wait is saved to MRAM every 60 s, so a reset partway resumes it.
"""

import re
import time

import pytest


pytestmark = pytest.mark.slow

BOOT_LINE = r"Boot (\d+); burn wire (burned|not burned) \((\d+) attempts\), (\d+) s of the burn wait done"


def burn_settings(flatsat, since):
    match = flatsat.expect(BOOT_LINE, timeout=30, since=since)
    return match.group(2) == "burned", int(match.group(4))


def test_burn_pin_stays_low_once_burned(nominal, flatsat, logic):
    pending = logic.start(["burn"], seconds=20.0)
    mark = flatsat.reset()
    capture = pending.wait()
    burned, _ = burn_settings(flatsat, mark)
    if not burned:
        pytest.skip("this build hasn't burned yet; see test_burn_fires_once")
    assert capture["burn"].high_time() == 0.0, "the burn pin went high on a board whose burn is recorded as done"


def test_burn_fires_once(nominal, flatsat, logic):
    mark = flatsat.reset()
    burned, _ = burn_settings(flatsat, mark)
    if burned:
        pytest.skip("the burn record says burned (main.c forces it); build as this module's docstring says")
    wait = flatsat.expect(r"Burn wait: (\d+)/(\d+) s", timeout=90, since=mark)
    remaining_s = int(wait.group(2)) - int(wait.group(1))

    # Recording from before the burn, so the whole pulse (and any second one) is seen; stopped once the burn is done
    pending = logic.start(["burn"], seconds=remaining_s + 180)
    on_line = flatsat.expect(r"Burn pin \(PB29\) on for (\d+) s", timeout=remaining_s + 90, since=mark)
    on_s = int(on_line.group(1))
    flatsat.expect(r"Burn pin \(PB29\) off; burn recorded in MRAM", timeout=on_s + 10, since=mark)
    time.sleep(2.0)  # A second pulse right after the first would show here
    capture = pending.finish()

    pulses = capture["burn"].pulses()
    assert capture["burn"].high_time() > 0, \
        "the log says the burn pin is on, but it never went high (BURN writes commented out in main.c?)"
    assert len(pulses) == 1, f"{len(pulses)} burn pulses: {[(round(t, 2), round(w, 2)) for t, w in pulses]}"
    assert abs(pulses[0][1] - on_s) <= 0.5, f"the burn pin was high for {pulses[0][1]:.2f} s, not {on_s} s"
    assert capture["burn"].levels[-1] == 0, "the burn pin was left high"


def test_reset_during_the_wait_resumes_it(nominal, flatsat):
    mark = flatsat.reset()
    burned, done_before = burn_settings(flatsat, mark)
    if burned:
        pytest.skip("the burn record says burned (main.c forces it); build as this module's docstring says")
    progress = flatsat.expect(r"Burn wait: (\d+)/(\d+) s", timeout=90, since=mark)
    saved = int(progress.group(1))
    if saved >= int(progress.group(2)):
        pytest.skip("the wait finished at its first save; build with BURN_WAIT_MINUTES of at least 2")
    mark = flatsat.reset()
    _, done_after = burn_settings(flatsat, mark)
    assert done_after >= saved, f"the wait restarted: {done_after} s done after a reset at {saved} s"


def test_boot_line_format():
    assert re.search(BOOT_LINE, "[+] Boot 3; burn wire not burned (0 attempts), 60 s of the burn wait done")
