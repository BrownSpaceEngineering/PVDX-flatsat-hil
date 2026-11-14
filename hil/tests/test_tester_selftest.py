"""The tester checking itself, with the flatsat disconnected (`pytest hil -m selftest`).

Jumpers (tester/src/selftest.c):
- i2c: D37 to D20 and D25 (SDA), D36 to D21 and D24 (SCL)
- spi: D1 to D50 (MOSI), D0 to D52 (SCK), D12 to D53 (CS)
- analog: D9 to A7 (a PWM output into the PVD_CRNT input)

"""

import zlib

import pytest

from tester import TesterError

pytestmark = pytest.mark.selftest


def selftest(tester, which):
    try:
        return tester.selftest(which)
    except TesterError as e:
        if "not available" in str(e):
            pytest.skip(f"{tester.firmware} has no {which} self-test")
        raise


def test_ping(tester):
    reply = tester.ping()
    assert reply["firmware"].startswith("pvdx-flatsat-tester")
    assert reply["protocol"] == 1


def test_analog_outputs(tester):
    reply = selftest(tester, "analog")
    assert reply["pass"], f"worst error {reply['worst_error_mv']} mV: {reply['readings']}"


def test_i2c_emulators(tester):
    reply = selftest(tester, "i2c")
    assert reply["stxg2_id_ok"], f"STXG2 ID read 0x{reply['stxg2_id']:08x}"
    assert reply["stxg2_scratchpad_ok"]
    assert reply["ina226_id_ok"]
    assert reply["mcp23017_ok"]


def test_spi_slave(tester):
    reply = selftest(tester, "spi")
    block = bytes((i * 37 + 11) & 0xFF for i in range(64))
    assert reply["expected_crc"] == zlib.crc32(block)
    stxg2 = reply["stxg2"]
    assert stxg2["spi_bytes"] == 64
    assert stxg2["spi_crc"] == reply["expected_crc"]
    assert stxg2["spi_transactions"] == 1
    assert stxg2["spi_overflows"] == 0


def test_outputs_disconnect_when_disarmed(tester):
    tester.set_output("phd0", 1.0)
    tester.arm()
    assert tester.outputs()["armed"]
    tester.disarm()
    assert not tester.outputs()["armed"]
