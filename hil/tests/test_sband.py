"""S-band radio (STXG2) control against the emulator: power sequencing, ID and scratchpad, register traffic."""

import pytest


POWER_SETTLE_S = 0.100  # STXG2_POWER_SETTLE_MS: EN high to RST high
SCRATCH_PATTERN = 0xB5E0CAFE


def test_device_check_talks_to_the_radio(nominal, flatsat):
    mark = flatsat.boot()
    assert flatsat.device_checks(mark).get("SBAND_ID")
    state = nominal.stxg2()
    assert state["scratchpad"] == SCRATCH_PATTERN, "the scratchpad write didn't reach the radio"
    assert state["invalid_addr"] == 0, "the driver touched a register the radio doesn't have"
    assert state["unpowered_accesses"] == 0, "the driver talked to the radio while it was off or held in reset"
    assert state["rejected_writes"] == 0, "the driver wrote a register the radio doesn't accept in its current mode"
    assert not flatsat.find_all(r"stxg2: I2C status flags", mark)


def test_power_sequencing(nominal, flatsat, logic):
    """EN rises, RST is held low for STXG2_POWER_SETTLE_MS, then released; power-off drops both."""
    pending = logic.start(["sband_en", "sband_rst"], seconds=15.0)
    mark = flatsat.reset()
    capture = pending.wait()
    flatsat.expect(r"SBAND_ID\s*:", timeout=30, since=mark)
    en, rst = capture["sband_en"], capture["sband_rst"]
    en_rises, rst_rises = en.edges(True), rst.edges(True)
    assert en_rises, "SBAND_EN never went high"
    assert rst_rises, "SBAND_RST never released"
    first_en = en_rises[0]
    release = next((t for t in rst_rises if t > first_en), None)
    assert release is not None and release - first_en >= POWER_SETTLE_S * 0.95, \
        f"RST released {None if release is None else round((release - first_en) * 1e3, 1)} ms after EN"
    assert rst.level_at(first_en) == 0, "RST must be held low while power comes on"
    # stxg2_power_off() after the check: both low again
    assert en.levels[-1] == 0 and rst.levels[-1] == 0, "the radio was left powered after its check"


@pytest.mark.xfail(reason="nothing in PVDXos calls stxg2_store() yet, so no data reaches the radio", strict=False)
def test_data_reaches_the_radio_over_spi(nominal, flatsat):
    flatsat.boot()
    flatsat.collect(r".", 60.0)
    state = nominal.stxg2()
    assert state["spi_bytes"] > 0
    assert state["spi_overflows"] == 0
    assert state["mem_wr_bytes"] == state["spi_words_since_enable"] * 4
