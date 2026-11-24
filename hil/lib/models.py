"""Physical models for the inputs the tester simulates, using the constants from the firmware sources themselves.

- Thermistors: temperature to ADC node voltage, as therm_volts_from_celsius() in src/drivers/thermistor/thermistor_driver.c.
- Perovskite pixels: the single-diode model the tester's INA226 emulator serves (tester/common/pixel_model.c).
- Fuel gauges: a pack's state as the MAX17205 registers the tester serves (tester/common/max17205_model.c), with the
  register addresses and sense resistor from src/drivers/fuel_gauge/fuel_gauge_driver.h.
"""

import math
import os
import re

from paths import PVDX

THERMISTOR_HEADER = os.path.join(PVDX, "src", "drivers", "thermistor", "thermistor_driver.h")
FUEL_GAUGE_HEADER = os.path.join(PVDX, "src", "drivers", "fuel_gauge", "fuel_gauge_driver.h")
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
CURRENT_LIMIT_A = 0.008  # The INA226 shunt register's range at 10 ohm


def nominal_pixel(pixel):
    """Distinct, plausible IV parameters per emulated pixel, so a mixed-up pixel shows in the currents."""
    return 40.0 + 8.0 * pixel, 850.0 + 10.0 * pixel  # Isc uA, Voc mV


def pixel_current_na(isc_ua, voc_mv, follow_mv):
    """Pixel current at a follower voltage, as tester/common/pixel_model.c computes it: the I that solves
    I = I(V_follow + I * R) by bisection, clamped to +-8 mA."""
    if isc_ua <= 0 or voc_mv <= 0:
        return 0.0
    isc, voc, nvt = isc_ua * 1e-6, voc_mv * 1e-3, IDEALITY * THERMAL_VOLTAGE
    i0 = isc / (math.exp(voc / nvt) - 1.0)
    follow = follow_mv * 1e-3

    def excess(i):  # Rises with i; zero at the solution
        return i - max(-CURRENT_LIMIT_A, min(CURRENT_LIMIT_A, isc - i0 * (math.exp((follow + i * SHUNT_OHMS) / nvt) - 1.0)))

    lo, hi = -CURRENT_LIMIT_A, CURRENT_LIMIT_A
    if excess(lo) >= 0:
        return lo * 1e9
    if excess(hi) <= 0:
        return hi * 1e9
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if excess(mid) < 0 else (lo, mid)
    return (lo + hi) / 2 * 1e9


# ---------- Fuel gauges ----------

with open(FUEL_GAUGE_HEADER) as _f:
    _FG_REGS = {m.group(1): int(m.group(2), 16) for m in re.finditer(r"#define\s+MAX17205_REG_(\w+)\s+0x([0-9A-Fa-f]+)", _f.read())}
FG_RSENSE_OHMS = _defines(FUEL_GAUGE_HEADER)["FG_RSENSE_OHMS"]
FG_PACKS = 4
# MAX17205 register scales (datasheet; the same as the flatsat driver's conversions)
SOC_LSB_PCT = 1 / 256
TEMP_LSB_C = 1 / 256
BATT_LSB_V = 1.25e-3
CELL_LSB_V = 78.125e-6
CURRENT_LSB_A = 1.5625e-6 / FG_RSENSE_OHMS


def _word(value, signed=False):
    lo, hi = (-0x8000, 0x7FFF) if signed else (0, 0xFFFF)
    return max(lo, min(hi, round(value))) & 0xFFFF


def fuel_gauge_registers(soc_pct, pack_volts, current_a, temp_c):
    """{register: value} for a 2S pack in that state, as the flatsat's driver reads them."""
    cell = _word(pack_volts / 2 / CELL_LSB_V)
    return {
        _FG_REGS["REPSOC"]: _word(soc_pct / SOC_LSB_PCT),
        _FG_REGS["BATT"]: _word(pack_volts / BATT_LSB_V),
        _FG_REGS["CURRENT"]: _word(current_a / CURRENT_LSB_A, signed=True),
        _FG_REGS["TEMP"]: _word(temp_c / TEMP_LSB_C, signed=True),
        _FG_REGS["VCELL"]: cell,
        _FG_REGS["CELL1"]: cell,
        _FG_REGS["CELL2"]: cell,
    }


def fuel_gauge_tolerances():
    """What the flatsat's log can be off by, per quantity, in its units (milli-percent, mV, mA, mC): a register LSB,
    plus 1 for its truncation to an integer."""
    return {"soc": SOC_LSB_PCT * 1000 + 1, "volts": BATT_LSB_V * 1000 + 1, "amps": CURRENT_LSB_A * 1000 + 1,
            "temp": TEMP_LSB_C * 1000 + 1}
