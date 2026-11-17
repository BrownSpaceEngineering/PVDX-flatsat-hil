"""Photodiode inputs PHD_0, 5, 10 and 15 (tester.PHOTODIODE_CHANNELS): each channel reads its own voltage.

The tester drives them with PWM through an RC filter (hil/README.md): the flatsat averages only 4 instantaneous ADC
samples, so without the filter the readings jump in steps of about 25% of the PWM voltage.
"""

import pytest

from conftest import set_environment
from tester import PHOTODIODE_CHANNELS

VDD = 3.3
CHANNEL_TOLERANCE_V = 0.12


def logged_photodiode_volts(flatsat, since):
    codes = {int(m.group(1)): int(m.group(2)) for m in flatsat.find_all(r"photodiode check: PHD_(\d+) code (\d+)", since)}
    missing = [ch for ch in PHOTODIODE_CHANNELS if ch not in codes]
    assert not missing, f"no photodiode check code for PHD_{missing} in the boot log"
    return [codes[ch] / 4095.0 * VDD for ch in PHOTODIODE_CHANNELS]


@pytest.mark.parametrize("ramp", ["rising", "falling"])
def test_each_channel_reads_its_own_voltage(tester, flatsat, ramp):
    """A distinct voltage on every channel catches swapped wires and PHD_CFG table mistakes; two ramps catch stuck ones."""
    volts = [0.4 + 0.7 * i for i in range(len(PHOTODIODE_CHANNELS))]
    if ramp == "falling":
        volts.reverse()
    set_environment(tester, photodiodes=volts)
    mark = flatsat.boot()
    measured = logged_photodiode_volts(flatsat, mark)
    errors = {f"PHD_{ch}": round(m - v, 3) for ch, v, m in zip(PHOTODIODE_CHANNELS, volts, measured) if abs(m - v) > CHANNEL_TOLERANCE_V}
    assert not errors, f"channels off by more than {CHANNEL_TOLERANCE_V} V (measured - set): {errors}"
