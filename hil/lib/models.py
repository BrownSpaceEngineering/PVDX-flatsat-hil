"""Physical models for the inputs the tester simulates, using the constants from the firmware sources themselves.

- Thermistors: temperature to ADC node voltage, as therm_volts_from_celsius() in src/drivers/thermistor/thermistor_driver.c.
- Photodiodes: a sun direction (body frame) to the 18 photodiode voltages, using the face normals and full-scale reading
  that src/libraries/adcs/src/photodiode_determination.c estimates the sun vector from.
- Perovskite pixels: the single-diode model the tester's INA226 emulator serves (tester/src/pixel_model.c).
"""

import math
import os
import re

from paths import PVDX

THERMISTOR_HEADER = os.path.join(PVDX, "src", "drivers", "thermistor", "thermistor_driver.h")
PHOTODIODE_SOURCE = os.path.join(PVDX, "src", "libraries", "adcs", "src", "photodiode_determination.c")
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


# ---------- Photodiodes ----------

def _photodiode_constants():
    with open(PHOTODIODE_SOURCE) as f:
        text = f.read()
    max_reading = float(re.search(r"MAX_READING\s*=\s*([\d.]+)f", text).group(1))
    min_reading = float(re.search(r"MIN_READING\s*=\s*([\d.]+)f", text).group(1))
    table = text[text.index("PHOTODIODES[18][3]"):]
    table = table[table.index("{") + 1:table.index("};")]
    rows = re.findall(r"\{\s*([-\d.]+)f?\s*,\s*([-\d.]+)f?\s*,\s*([-\d.]+)f?\s*\}", table)
    normals = [tuple(float(x) for x in row) for row in rows]
    assert len(normals) == 18, f"expected 18 photodiode normals, parsed {len(normals)}"
    return max_reading, min_reading, normals


PHOTODIODE_MAX_READING, PHOTODIODE_MIN_READING, PHOTODIODE_NORMALS = _photodiode_constants()


def normalize(v):
    n = math.sqrt(sum(x * x for x in v))
    return tuple(x / n for x in v)


def photodiode_volts(sun, dark_volts=0.0):
    """Voltages for sunlight from direction `sun` (body frame): full reading times the cosine of the incidence angle."""
    sun = normalize(sun)
    return [max(0.0, sum(n * s for n, s in zip(normal, sun))) * PHOTODIODE_MAX_READING + dark_volts
            for normal in PHOTODIODE_NORMALS]


def estimate_sun_vector(readings):
    """get_vec_from_photodiode_readings() in Python: the brighter diode of each opposing pair, the 5 brightest of those,
    least squares for the sun vector. Returns (in_sun, unit vector or None)."""
    import numpy as np

    winners = []
    valid = 0
    for pair in range(9):
        i0, i1 = 2 * pair, 2 * pair + 1
        index, reading = (i0, readings[i0]) if readings[i0] >= readings[i1] else (i1, readings[i1])
        if reading > PHOTODIODE_MIN_READING:
            valid += 1
        winners.append((reading, index))
    if valid < 3:
        return False, None
    # The C code keeps the 5 brightest by replacing the dimmest slot; ties don't matter for a least-squares fit
    selected = sorted(winners, key=lambda w: w[0], reverse=True)[:5]
    a = np.array([PHOTODIODE_NORMALS[i] for _, i in selected])
    b = np.array([r / PHOTODIODE_MAX_READING for r, _ in selected])
    try:
        x = np.linalg.solve(a.T @ a, a.T @ b)
    except np.linalg.LinAlgError:
        return False, None
    return True, tuple(x / np.linalg.norm(x))


def angle_deg(a, b):
    a, b = normalize(a), normalize(b)
    return math.degrees(math.acos(max(-1.0, min(1.0, sum(x * y for x, y in zip(a, b))))))


# ---------- Perovskite pixels ----------

IDEALITY = 1.5
THERMAL_VOLTAGE = 0.025852
SHUNT_OHMS = 10.0


def pixel_current_na(isc_ua, voc_mv, follow_mv):
    """Pixel current at a follower voltage, as tester/src/pixel_model.c computes it."""
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
