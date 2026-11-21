"""Facts about the flatsat firmware the tests need, read from its sources so they stay in step."""

import os
import re

from paths import PVDX


def state_ids():
    """{state name: enum value} from src/states.h, e.g. {"STATE_IDLE": 2, ...}."""
    with open(os.path.join(PVDX, "src", "states.h")) as f:
        text = f.read()
    body = re.search(r"typedef enum\s*\{(.*?)\}\s*state_id_t", text, re.S).group(1)
    names = re.findall(r"^\s*(STATE_\w+)", body, re.M)
    return {name: i for i, name in enumerate(names)}


def battery_thresholds():
    """(LOW_BATTERY_LEVEL, RECOVERED_BATTERY_LEVEL) from src/states.c, as 0-1 fractions of a full battery."""
    with open(os.path.join(PVDX, "src", "states.c")) as f:
        text = f.read()
    return tuple(float(re.search(rf"#define\s+{name}\s+([\d.]+)", text).group(1))
                 for name in ("LOW_BATTERY_LEVEL", "RECOVERED_BATTERY_LEVEL"))


TRANSITION = r"state_machine: state (\d+) -> state (\d+)"
DEVICE_CHECK_TIMEOUT_S = 60
