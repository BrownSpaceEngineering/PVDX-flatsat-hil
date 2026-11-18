"""The simulated orbit the tester plays to the flatsat, and the ground passes.

Time is wall-clock seconds; phases count from the run's start, so a resumed run keeps its orbit. An orbit is sunlit
then eclipsed (eclipse_fraction of the period at the end). Temperatures follow the sun with a lag: a sinusoid between
the configured coldest and hottest, its peak a quarter orbit after the middle of the sunlit part.
"""

import math
from dataclasses import dataclass


@dataclass
class Orbit:
    start: float
    period_s: float
    eclipse_fraction: float
    temp_min_c: float
    temp_max_c: float

    def phase(self, t):
        """0..1 through the current orbit."""
        return ((t - self.start) / self.period_s) % 1.0

    def number(self, t):
        return int((t - self.start) // self.period_s)

    def sunlit(self, t):
        return self.phase(t) < 1.0 - self.eclipse_fraction

    def temperature_c(self, t):
        sun_middle = (1.0 - self.eclipse_fraction) / 2
        peak = sun_middle + 0.25
        swing = (self.temp_max_c - self.temp_min_c) / 2
        return self.temp_min_c + swing + swing * math.cos(2 * math.pi * (self.phase(t) - peak))


@dataclass
class Passes:
    """Ground passes: pass_s long, one every interval_s, the first at offset_s into the run."""

    start: float
    interval_s: float
    pass_s: float
    offset_s: float = 0.0

    def number(self, t):
        return int((t - self.start - self.offset_s) // self.interval_s)

    def in_pass(self, t):
        since = (t - self.start - self.offset_s) % self.interval_s
        return t - self.start >= self.offset_s and since < self.pass_s


def from_config(config, start):
    o, p = config["orbit"], config["passes"]
    orbit = Orbit(start, o["period_s"], o["eclipse_fraction"], o["temp_min_c"], o["temp_max_c"])
    passes = Passes(start, p["interval_s"], p["pass_s"], p.get("offset_s", 0.0))
    return orbit, passes
