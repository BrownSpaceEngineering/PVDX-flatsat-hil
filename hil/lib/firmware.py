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


TRANSITION = r"state_machine: state (\d+) -> state (\d+)"
DEVICE_CHECK_TIMEOUT_S = 60
