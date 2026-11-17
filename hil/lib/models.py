"""Physical models for the inputs the tester simulates, using the constants from the firmware sources themselves.

- Thermistors: temperature to ADC node voltage, as therm_volts_from_celsius() in src/drivers/thermistor/thermistor_driver.c.
- Perovskite pixels: the single-diode model the tester's INA226 emulator serves (tester/common/pixel_model.c).
"""

import math
import os
import re

from paths import PVDX

THERMISTOR_HEADER = os.path.join(PVDX, "src", "drivers", "thermistor", "thermistor_driver.h")
VDD = 3.3


def _defines(path):
    with open(path) as f:
        text = f.read()
    return {m.group(1): float(m.group(2)) for m in re.finditer(r"#define\s+(\w+)\s+([-\d.]+)f?\b", text)}


# ---------- Thermistors ----------

_T = _defines(THERMISTOR_HEADER)


def thermistor_volts(temp_c):
    r = _T["THERM_R0_OHMS"] * math.exp(_T["THERM_BETA"] * (1.0 / (temp_c + 273.15) - 1.0 / _T["THERM_T0_K"]))
    return _T["THERM_VREF_V"] * (_T["THERM_R_T2_OHMS"] + r) / (_T["THERM_R_T1_OHMS"] + _T["THERM_R_T2_OHMS"] + r)


def thermistor_celsius(volts):
    k = volts / _T["THERM_VREF_V"]
    r = k * _T["THERM_R_T1_OHMS"] / (1.0 - k) - _T["THERM_R_T2_OHMS"]
    return 1.0 / (1.0 / _T["THERM_T0_K"] + math.log(r / _T["THERM_R0_OHMS"]) / _T["THERM_BETA"]) - 273.15


HEATER_ON_C = _T["HEATER_ON_C"]
HEATER_OFF_C = _T["HEATER_OFF_C"]
OPEN_VOLTS = VDD  # Railed divider: above THERM_K_OPEN of full scale


# ---------- Perovskite pixels ----------

IDEALITY = 1.5
THERMAL_VOLTAGE = 0.025852
SHUNT_OHMS = 10.0


def pixel_current_na(isc_ua, voc_mv, follow_mv):
    """Pixel current at a follower voltage, as tester/common/pixel_model.c computes it."""
    if isc_ua <= 0 or voc_mv <= 0:
        return 0.0
    isc, voc, nvt = isc_ua * 1e-6, voc_mv * 1e-3, IDEALITY * THERMAL_VOLTAGE
    i0 = isc / (math.exp(voc / nvt) - 1.0)

    def at(v):
        return max(-0.008, min(0.008, isc - i0 * (math.exp(v / nvt) - 1.0)))

    i = at(follow_mv * 1e-3)
    for _ in range(4):
        i = at(follow_mv * 1e-3 + i * SHUNT_OHMS)
    return i * 1e9
