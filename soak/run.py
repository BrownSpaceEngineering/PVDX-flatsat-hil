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

With [radio] enabled, a HackRF ground station listens for the satellite's beacons and runs a pass (opening the UHF
link and trading turns) in each pass window (GroundStation below, hil/lib/ground.py).
"""

import argparse
import json
import os
import queue
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
import numpy as np  # noqa: E402
import paths  # noqa: E402
import report  # noqa: E402
import schedule  # noqa: E402
import uplinks  # noqa: E402

BUILD = monitors.re.compile(r"Built from commit: (\S+)")


def pvdx_commit():
    """The commit PVDX_ROOT is at (the ground decodes and uplinks with its sources), or None if it isn't a git checkout."""
    import subprocess

    result = subprocess.run(["git", "-C", paths.PVDX, "rev-parse", "HEAD"], capture_output=True, text=True)
    return result.stdout.strip() or None


class Runner:
    """Feeds log lines and ticks to the monitors and writes the run directory. Knows nothing about hardware, so the
    live loop and --replay share it."""

    def __init__(self, config, run_dir, info, stop_on_failure=False, echo=True, radio_constants=None):
        self.config = config
        self.run_dir = run_dir
        self.info = info
        self.stop_on_failure = stop_on_failure
        self.echo = echo
        self.stop = threading.Event()
        self.rtt = report.RttLog(run_dir)
        self.events = report.Events(run_dir)
        self.metrics_file = report.Metrics(run_dir)
        self.monitors = monitors.build(config, self.emit, radio_constants)
        self.radio = next((m for m in self.monitors if m.name == "radio"), None)
        self.uplinks = next((m for m in self.monitors if m.name == "uplink"), None)
        self.telemetry = next((m for m in self.monitors if m.name == "telemetry"), None)
        self.pvdx_commit = pvdx_commit() if radio_constants is not None else None
        self.posted = queue.Queue()  # Work from other threads (the ground station), run on the main loop's thread
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
            built = match.group(1)  # The last 7 hex digits of the commit (src/Makefile, GIT_COMMIT_HASH)
            if self.pvdx_commit and built != "NONE" and not self.pvdx_commit.endswith(built):
                self.emit(monitors.Event(t, "warn", "runner", f"the flatsat runs {match.group(1)}, but the ground reads "
                                         f"PVDXos's radio code from {paths.PVDX} at {self.pvdx_commit[:10]}: set "
                                         f"PVDX_ROOT to the build's checkout", [text]))
        for m in self.monitors:
            m.on_line(t, text)

    def on_telemetry(self, t, tel):
        for m in (self.uplinks, self.telemetry):
            if m is not None:
                m.on_telemetry(t, tel)

    def metrics(self):
        row = {}
        for m in self.monitors:
            row.update(m.metrics())
        row.update(self.extra_metrics)
        return row

    def post(self, work):
        """Runs work() on the main loop's thread, which owns the monitors."""
        self.posted.put(work)

    def drain(self):
        while True:
            try:
                work = self.posted.get_nowait()
            except queue.Empty:
                return
            work()

    def tick(self, t):
        self.drain()
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
        self.drain()
        for m in self.monitors:
            m.finish(t)
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


class GroundStation(threading.Thread):
    """The HackRF ground station: listens all the time (beacons), and runs a pass in each pass window of the schedule:
    it opens the PUSP link, uplinks that pass's command file over CFDP (soak/uplinks.py) and trades turns until the
    transfer is done. Every frame heard goes to frames.jsonl, every decoded beacon to telemetry.jsonl, every pass to
    passes.jsonl, and coded frames read without the Viterbi decoder to coded.jsonl (for soak/tools/fit_rsc.py). The
    monitors get all of it through runner.post()."""

    MAX_CODED_CAPTURES = 200

    def __init__(self, runner, config, passes, radio=None):
        """radio: a hackrf.Radio to use instead of the HackRF (tests)."""
        super().__init__(daemon=True)
        import ccsds
        import ground
        import hackrf
        import mrfsk

        r = config["radio"]
        self.runner = runner
        self.config = config
        self.passes = passes
        self.turns = r["turns_per_pass"]
        if radio is None:
            constants = ccsds.constants()
            code, interleaved, _ = mrfsk.check_phy(ccsds.phy_config())
            # The satellite receives uncoded frames too, so the ground codes its own only once the code is confirmed
            tx_code = code if r.get("tx_coded", True) and code.confirmed else None
            radio = hackrf.HackRF(constants["UHF_FREQ_HZ"], r["lna_db"], r["vga_db"], r["tx_vga_db"], r["serial"] or None,
                                  code=code, interleaved=interleaved, tx_code=tx_code)
        self.ground = ground.Ground(radio, on_receive=self._heard)
        self.frames = open(os.path.join(runner.run_dir, "frames.jsonl"), "a", buffering=1)
        self.telemetry_log = open(os.path.join(runner.run_dir, "telemetry.jsonl"), "a", buffering=1)
        self.coded_log = open(os.path.join(runner.run_dir, "coded.jsonl"), "a", buffering=1)
        self.coded_captures = 0
        self.pass_log = os.path.join(runner.run_dir, "passes.jsonl")

    def _heard(self, rx):
        import telemetry

        f = rx.frame
        beacon = self.ground.is_beacon(rx)
        record = {"time": report.iso(rx.t), "dir": "down", "beacon": beacon, "error": rx.error,
                  "power_db": round(rx.power_db, 1), "offset_hz": round(rx.freq_offset_hz)}
        if rx.coded:
            record.update(fec=rx.fec, corrected_bits=rx.corrected_bits)
        if f is not None:
            record.update(vc=f.vcid, count=f.vc_frame_count, apid=f.apid, seq=f.sequence_count, len=len(f.data),
                          data=f.data.hex() if not beacon else None)
        tel = None
        if beacon:
            try:
                tel = telemetry.decode(f.data)
                record["telemetry"] = telemetry.summary(tel)
                self.telemetry_log.write(json.dumps({"time": report.iso(rx.t), **tel}) + "\n")
            except ValueError as e:
                record["error"] = f"telemetry: {e}"
        self.frames.write(json.dumps(record) + "\n")
        if rx.coded and rx.fec.startswith("systematic") and rx.coded_bits is not None \
                and self.coded_captures < self.MAX_CODED_CAPTURES:
            self.coded_captures += 1
            self.coded_log.write(json.dumps({"time": report.iso(rx.t), "fec": rx.fec, "n": len(rx.coded_bits),
                                             "coded": bytes(np.packbits(rx.coded_bits)).hex()}) + "\n")
        self.runner.post(lambda: self.runner.radio.on_frame(rx.t, rx, beacon))
        if tel is not None:
            self.runner.post(lambda: self.runner.on_telemetry(rx.t, tel))

    def run_one_pass(self, number, duration_s):
        import cfdp

        now = time.time()
        commands, data, file_timestamp = uplinks.plan(self.config, number, now)
        transfer = cfdp.Uplink(data, seq=int(now)) if data else None
        self.runner.post(lambda t=now, d=duration_s: self.runner.radio.pass_started(t, d))
        result = self.ground.run_pass(duration_s, uplink=transfer, turns=self.turns)
        record = {"time": report.iso(result.start), "established": result.established,
                  "establish_s": result.establish_s, "turns_given": result.turns_given,
                  "turns_taken": result.turns_taken, "sent": result.sent, "resent": result.resent,
                  "received": len(result.received), "failure": result.failure, "commands": commands}
        if transfer is not None:
            record["uplink"] = {"seq": transfer.seq, "bytes": len(data), "succeeded": transfer.succeeded,
                                "finished": transfer.finished is not None, "turns": transfer.turns,
                                "resent_pdus": transfer.resent_pdus, "events": transfer.events}
        with open(self.pass_log, "a") as f:
            f.write(json.dumps(record) + "\n")
        end = time.time()
        self.runner.post(lambda r=result: self.runner.radio.on_pass(end, r))
        if self.runner.uplinks is not None:
            self.runner.post(lambda r=result: self.runner.uplinks.on_pass(end, r, commands, file_timestamp))
        return result

    def run(self):
        done = set()
        p = self.passes
        while not self.runner.stop.is_set():
            now = time.time()
            if p.in_pass(now) and p.number(now) not in done:
                done.add(p.number(now))
                remaining = p.pass_s - ((now - p.start - p.offset_s) % p.interval_s)
                self.run_one_pass(p.number(now), remaining)
            else:
                self.ground.receive(0.2)

    def close(self):
        self.ground.radio.close()
        for f in (self.frames, self.telemetry_log, self.coded_log):
            f.close()


def live(runner, config, hours):
    orbit, passes = schedule.from_config(config, runner.info["start"])
    bench = Bench(runner, config)
    station = None
    try:
        if runner.radio is not None:
            station = GroundStation(runner, config, passes)
            station.start()
        _live_loop(runner, config, hours, orbit, bench)
    finally:
        runner.stop.set()
        if station is not None:
            station.join(timeout=60)  # A pass in progress runs to the end of its window; closing the radio cuts it short
            station.close()
        bench.close()


def _live_loop(runner, config, hours, orbit, bench):
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

    radio_constants = None
    if config.get("radio", {}).get("enabled") and not args.replay:
        import ccsds

        import mrfsk
        import telemetry

        # Build PVDXos's radio code, and check the satellite's PHY and telemetry layout: fails here, not mid-run
        radio_constants = ccsds.constants()
        code, _, _ = mrfsk.check_phy(ccsds.phy_config())
        telemetry.layout()
        print(f"ground station: PVDXos radio code from {paths.PVDX}; coded frames use {code.name}"
              f"{'' if code.confirmed else ' (unconfirmed: read from the systematic bits until fit_rsc.py pins it)'}")
    runner = Runner(config, run_dir, info, stop_on_failure=args.stop_on_failure, radio_constants=radio_constants)
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
