#!/usr/bin/env python3
"""Multi-day soak of the flatsat: plays a simulated orbit to it through the tester, records everything it logs, and
runs the monitors (soak/monitors.py) until stopped or --hours have passed.

The runner never resets or halts the flatsat: it only reads RTT over the J-Link, which doesn't disturb the target, so
every reset it sees is one the satellite did. Flash the build under test first (`make dev DEFINES=...` then
`make flash_to_mram`, and wait for "golden copy written from flash"), with the tester and its wiring as for the HIL
suite (hil/README.md). Everything goes into soak/runs/<id>/ (see soak/report.py).

    python soak/run.py                       # new run with soak/config.toml
    python soak/run.py --resume runs/<id>    # carry on after the Mac or the runner restarted
    python soak/run.py --replay rtt.log      # run the monitors over a recorded log, offline

Phase 1 has no radio: the ground station and the uplink and downlink monitors come later (hil/README.md, Soak test).
"""

import argparse
import json
import os
import signal
import sys
import threading
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "hil", "lib"))

import models  # noqa: E402
import monitors  # noqa: E402
import report  # noqa: E402
import schedule  # noqa: E402

BUILD = monitors.re.compile(r"Built from commit: (\S+)")


class Runner:
    """Feeds log lines and ticks to the monitors and writes the run directory. Knows nothing about hardware, so the
    live loop and --replay share it."""

    def __init__(self, config, run_dir, info, stop_on_failure=False, echo=True):
        self.config = config
        self.run_dir = run_dir
        self.info = info
        self.stop_on_failure = stop_on_failure
        self.echo = echo
        self.stop = threading.Event()
        self.rtt = report.RttLog(run_dir)
        self.events = report.Events(run_dir)
        self.metrics_file = report.Metrics(run_dir)
        self.monitors = monitors.build(config, self.emit)
        self.extra_metrics = {}
        r = config["runner"]
        self.last = {"summary": 0.0, "metrics": 0.0, "checkpoint": 0.0}
        self.every = {"summary": r["summary_every_s"], "metrics": r["metrics_every_s"], "checkpoint": r["checkpoint_every_s"]}
        self.day = info.get("day")

    def emit(self, event):
        self.events.write(event)
        if self.echo:
            print(f"{report.iso(event.t)} {event.severity.upper():4} {event.monitor}: {event.message}", flush=True)
        if event.severity == "fail" and self.stop_on_failure:
            self.stop.set()

    def feed(self, t, text):
        self.rtt.write(t, text)
        match = BUILD.search(text)
        if match:
            self.info["build"] = match.group(1)
        for m in self.monitors:
            m.on_line(t, text)

    def metrics(self):
        row = {}
        for m in self.monitors:
            row.update(m.metrics())
        row.update(self.extra_metrics)
        return row

    def tick(self, t):
        for m in self.monitors:
            m.on_tick(t)
        day = report.utc(t).strftime("%Y-%m-%d")
        if self.day is not None and day != self.day:
            report.daily(self.run_dir, self.day, self.metrics(), self.events.all())
        self.day = day
        for what, write in (("metrics", self._write_metrics), ("summary", self._write_summary),
                            ("checkpoint", self.checkpoint)):
            if t - self.last[what] >= self.every[what]:
                self.last[what] = t
                write(t)

    def _write_metrics(self, t):
        self.metrics_file.write(t, self.metrics())

    def _write_summary(self, t):
        report.summary(self.run_dir, self.info, self.metrics(), self.events.all(), t)

    def checkpoint(self, t=None):
        state = {"info": {**self.info, "day": self.day}, "monitors": {m.name: m.state() for m in self.monitors}}
        path = os.path.join(self.run_dir, "state.json")
        with open(path + ".tmp", "w") as f:
            json.dump(state, f, indent=1)
        os.replace(path + ".tmp", path)

    def restore(self, state):
        for m in self.monitors:
            m.restore(state["monitors"].get(m.name, {}))

    def finish(self, t):
        report.daily(self.run_dir, self.day or report.utc(t).strftime("%Y-%m-%d"), self.metrics(), self.events.all())
        self._write_summary(t)
        self.checkpoint(t)
        self.rtt.close()


# ---------- Hardware ----------


class Bench:
    """The flatsat's log and the tester, reopened when a session drops. Never resets either board."""

    def __init__(self, runner, config):
        self.runner = runner
        self.config = config
        self.flatsat = None
        self.tester = None
        self.logic = None
        self.mark = 0
        self.last_line = time.monotonic()
        self.last_open_try = 0.0
        self.sunlit = None

    def needs_open(self):
        return (self.flatsat is None or self.tester is None) and time.monotonic() - self.last_open_try > 30

    def open(self):
        """Opens whichever session isn't open. The probes are told apart once (rtt.probe_serials() caches it, and
        identifying them connects to each, which must not happen while a session is open): a tester that isn't
        connected when the runner starts stays absent until it restarts."""
        import rtt
        from flatsat import Flatsat
        from logic import LogicRecorder
        from tester import RttTransport, Tester

        self.last_open_try = time.monotonic()
        flatsat_serial, tester_serial = rtt.probe_serials()
        if self.flatsat is None:
            if flatsat_serial is None:
                raise rtt.ProbeError("no J-Link for the flatsat")
            self.flatsat = Flatsat(serial_number=flatsat_serial, max_lines=self.config["runner"]["keep_log_lines"])
            self.mark = self.flatsat.mark()
            self.last_line = time.monotonic()
        if self.tester is None and tester_serial is not None:
            self.tester = Tester(RttTransport(tester_serial))
            self.logic = LogicRecorder(self.tester)
            full_scale = self.tester.vdd() * models.VDD / self.flatsat.vdd()
            self.tester.set_full_scale(full_scale)
            self.sunlit = None  # Re-send the whole environment
            self.tester.arm()

    def close(self):
        for thing in (self.flatsat, self.tester):
            try:
                if thing:
                    thing.close()
            except Exception:
                pass
        self.flatsat = self.tester = self.logic = None

    def read(self):
        """New log lines with wall-clock times."""
        lines, self.mark = self.flatsat.read_new(self.mark)
        if lines:
            self.last_line = time.monotonic()
        offset = time.time() - time.monotonic()  # LogLine.time is monotonic
        return [(line.time + offset, line.text) for line in lines]

    def stale(self):
        """The log has been silent long enough to try a fresh J-Link session (the liveness monitor reports the
        silence itself)."""
        mono = time.monotonic()
        limit = self.config["runner"]["reconnect_after_s"]
        return mono - self.last_line > limit and mono - self.last_open_try > limit

    def environment(self, orbit, t):
        """Sets the tester's inputs for this point of the orbit."""
        env = self.config["environment"]
        sunlit = orbit.sunlit(t)
        temp = orbit.temperature_c(t)
        self.tester.set_thermistors([models.thermistor_volts(temp)] * 4)
        if sunlit != self.sunlit:
            self.tester.set_photodiodes(env["sunlit_photodiode_volts"] if sunlit else env["eclipse_photodiode_volts"])
            self.tester.set_pvds(env["pvd_vocs"] if sunlit else [0.0] * 4)
            for p in range(16):
                lit = sunlit and env["emulated_pixels"]
                isc, voc = models.nominal_pixel(p) if lit else (0.0, 0.0)
                self.tester.set_pixel(p, isc, voc)
            self.sunlit = sunlit
        return {"sunlit": int(sunlit), "temp_c": round(temp, 1), "orbit": orbit.number(t)}


def live(runner, config, hours):
    orbit, _ = schedule.from_config(config, runner.info["start"])
    bench = Bench(runner, config)
    end = time.time() + hours * 3600 if hours else None
    last_env = last_capture = 0.0
    cap = config["capture"]
    while not runner.stop.is_set() and (end is None or time.time() < end):
        now = time.time()
        try:
            if bench.needs_open():
                bench.open()
            if bench.flatsat is not None:
                for t, text in bench.read():
                    runner.feed(t, text)
                if bench.stale():
                    runner.emit(monitors.Event(now, "warn", "runner", "reopening the J-Link sessions (log silent)"))
                    bench.close()
            if bench.tester is not None and now - last_env >= config["environment"]["update_s"]:
                runner.extra_metrics.update(bench.environment(orbit, now))
                last_env = now
            if bench.logic is not None and now - last_capture >= cap["interval_s"]:
                last_capture = now
                capture = bench.logic.capture(monitors.OUTPUT_SIGNALS, cap["seconds"])
                monitors.check_outputs(time.time(), capture, runner.emit)
        except Exception as e:  # A dropped probe or tester: closed, and reopened after a pause
            runner.emit(monitors.Event(now, "warn", "runner", f"{type(e).__name__}: {e}"))
            bench.close()
        runner.tick(time.time())
        runner.stop.wait(1.0)
    bench.close()


def replay(runner, path, tick_s=1.0):
    """Runs the monitors over a recorded log. Lines without timestamps get one every 10 ms from the run's start."""
    t = runner.info["start"]
    next_tick = t
    for stamp, text in report.read_rtt_log(path):
        t = stamp if stamp is not None else t + 0.01
        while next_tick <= t:
            runner.tick(next_tick)
            next_tick += tick_s
        runner.feed(t, text)
    runner.tick(t)
    return t


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", default=os.path.join(HERE, "config.toml"))
    parser.add_argument("--run-dir", help="default: soak/runs/<UTC start time>")
    parser.add_argument("--resume", metavar="RUN_DIR", help="continue the run in RUN_DIR (its own config.toml)")
    parser.add_argument("--hours", type=float, help="stop after this long (default: until interrupted)")
    parser.add_argument("--stop-on-failure", action="store_true", help="stop at the first fail event")
    parser.add_argument("--replay", metavar="LOG", help="run the monitors over a recorded log instead of the bench")
    args = parser.parse_args()

    if args.resume:
        run_dir = args.resume
        with open(os.path.join(run_dir, "state.json")) as f:
            state = json.load(f)
        config_path = os.path.join(run_dir, "config.toml")
        info = state["info"]
    else:
        start = time.time()
        if args.replay:
            first = next((s for s, _ in report.read_rtt_log(args.replay) if s is not None), None)
            start = first if first is not None else start
        run_id = report.utc(time.time()).strftime("%Y%m%dT%H%M%SZ")
        run_dir = args.run_dir or os.path.join(HERE, "runs", run_id)
        os.makedirs(run_dir, exist_ok=True)
        with open(args.config, "rb") as src, open(os.path.join(run_dir, "config.toml"), "wb") as dst:
            dst.write(src.read())  # The run keeps the settings it started with
        config_path = os.path.join(run_dir, "config.toml")
        info = {"id": os.path.basename(run_dir), "start": start, "config": args.config,
                "mode": "replay" if args.replay else "live"}
        state = None
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    runner = Runner(config, run_dir, info, stop_on_failure=args.stop_on_failure)
    if state:
        runner.restore(state)
    signal.signal(signal.SIGTERM, lambda *_: runner.stop.set())
    print(f"soak run {info['id']} in {run_dir}", flush=True)
    try:
        if args.replay:
            end = replay(runner, args.replay)
        else:
            live(runner, config, args.hours)
            end = time.time()
    except KeyboardInterrupt:
        end = time.time()
    runner.finish(end)
    fails = sum(1 for e in runner.events.all() if e["severity"] == "fail")
    print(f"{fails} fail events; summary in {os.path.join(run_dir, 'summary.md')}")
    # A live run that ended as asked exits 0 whatever it found (launchd restarts only a crashed runner); a replay
    # reports its verdict
    return 1 if args.replay and fails else 0


if __name__ == "__main__":
    sys.exit(main())
