"""State machine timing in a normal DEVBUILD (src/states.c): IDLE waits FAKE_IDLE_PERIOD_MS (25 s), FAKE_HEARTBEAT lasts
FAKE_HEARTBEAT_PERIOD_MS (5 s) and mocks sunlight, so IDLE then goes to PEROVSKITE_CAPTURE. Runs in eclipse: ADCS reports
real sunlight from the photodiodes, which would start a perovskite capture before the heartbeat."""

import re

import firmware
from conftest import DARK, set_environment

IDLE_S = 25.0
HEARTBEAT_S = 5.0
TOLERANCE_S = 1.5


def test_idle_heartbeat_cycle(tester, flatsat):
    set_environment(tester, photodiodes=DARK)
    states = firmware.state_ids()
    idle, heartbeat, perovskite = states["STATE_IDLE"], states["STATE_FAKE_HEARTBEAT"], states["STATE_PEROVSKITE_CAPTURE"]
    mark = flatsat.boot()

    def next_transition(since, timeout):
        flatsat.expect(firmware.TRANSITION, timeout=timeout, since=since)
        for index, line in enumerate(flatsat.log(since)):
            match = re.search(firmware.TRANSITION, line.text)
            if match:
                return int(match.group(1)), int(match.group(2)), line.time, since + index + 1
        raise AssertionError("transition vanished from the log")

    _, to, t_idle, position = next_transition(mark, 60)
    assert to == idle, "the state machine should start in IDLE"
    frm, to, t_heartbeat, position = next_transition(position, IDLE_S + 10)
    assert (frm, to) == (idle, heartbeat)
    assert abs(t_heartbeat - t_idle - IDLE_S) < TOLERANCE_S, f"IDLE lasted {t_heartbeat - t_idle:.1f} s"
    frm, to, t_back, position = next_transition(position, HEARTBEAT_S + 10)
    assert (frm, to) == (heartbeat, idle)
    assert abs(t_back - t_heartbeat - HEARTBEAT_S) < TOLERANCE_S, f"FAKE_HEARTBEAT lasted {t_back - t_heartbeat:.1f} s"
    frm, to, _, _ = next_transition(position, 10)
    assert (frm, to) == (idle, perovskite), "mocked sunlight should start a perovskite sweep"
