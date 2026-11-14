"""UHF radio against a HackRF ground station (skipped unless `hackrf_info` finds one).

Beacons: build with
    make dev DEFINES="BURN_SEQUENCE BURN_SEQUENCE_UHF_MODE=BURN_UHF_MODE_BEACON POST_BURN_STEP_MINUTES=1"
and flash with `make flash_to_mram`. After boot the flatsat sends UHF_BEACON_COUNT (10) beacons, UHF_BEACON_PERIOD_MS
(1 s) apart, at UHF_FREQ_HZ (437.765 MHz); the HackRF records the band and the test counts the bursts of energy. Use
attenuators on a cabled link: never connect the radios directly.
"""

import os
import subprocess
import tempfile

import pytest

pytestmark = pytest.mark.hackrf

UHF_FREQ_HZ = 437_765_000
SAMPLE_RATE = 2_000_000
BEACON_COUNT = 10
RECORD_S = 15.0
BURST_THRESHOLD_DB = 10.0


def record(seconds, path):
    subprocess.run(["hackrf_transfer", "-r", path, "-f", str(UHF_FREQ_HZ), "-s", str(SAMPLE_RATE),
                    "-n", str(int(seconds * SAMPLE_RATE)), "-l", "24", "-g", "20"], check=True, capture_output=True)


def burst_count(path):
    import numpy as np

    raw = np.fromfile(path, dtype=np.int8).astype(np.float32)
    iq = raw[0::2] + 1j * raw[1::2]
    block = SAMPLE_RATE // 1000  # 1 ms power bins
    power = np.abs(iq[: len(iq) // block * block].reshape(-1, block)) ** 2
    db = 10 * np.log10(power.mean(axis=1) + 1e-9)
    above = db > np.median(db) + BURST_THRESHOLD_DB
    return int(np.count_nonzero(above[1:] & ~above[:-1]))


def test_beacons_are_received(flatsat):
    mark = flatsat.reset()
    try:
        wait = flatsat.expect(r"Waiting (\d+) minutes before UHF beacons", timeout=60, since=mark)
    except TimeoutError:
        pytest.skip("this build doesn't send UHF beacons; see this module's docstring")
    minutes = int(wait.group(1))
    # The beacons start as soon as the last minute of the wait has passed
    flatsat.expect(rf"{minutes}/{minutes} minutes elapsed", timeout=minutes * 60 + 30, since=mark)
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "uhf.iq")
        record(RECORD_S, path)
        bursts = burst_count(path)
    sent = flatsat.expect(r"uhf: sent (\d+)/\d+ beacons", timeout=30, since=mark)
    assert int(sent.group(1)) == BEACON_COUNT, sent.group(0)
    assert bursts >= BEACON_COUNT - 2, f"heard {bursts} bursts at {UHF_FREQ_HZ / 1e6} MHz, expected {BEACON_COUNT}"


@pytest.mark.skip(reason="needs a ground-station modem for the AT86RF215's modulation (HackRF side of burn_uhf_link)")
def test_link_exchange():
    """burn_uhf_link() exchanges DATA/ACK packets (src/burn/uhf_link_packet.h, HackRF SSID 2) with the ground station."""
