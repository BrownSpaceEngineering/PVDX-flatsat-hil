"""What a soak run leaves in its directory (soak/runs/<id>/):

    rtt-YYYYMMDD.log     every flatsat log line, "<UTC ISO time>\t<line>", one file per UTC day
    events.jsonl         every monitor event (monitors.Event), as it happens
    metrics.csv          one row a minute: environment and monitor counters
    daily/YYYY-MM-DD.md  per-day summary, written when the day ends (and at the end of the run)
    summary.md           the whole run so far, rewritten every few minutes
    state.json           the runner's checkpoint (run.py)
"""

import csv
import datetime
import json
import os


def utc(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc)


def iso(t):
    return utc(t).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class RttLog:
    """The flatsat's log, rotated at UTC midnight."""

    def __init__(self, run_dir):
        self.run_dir = run_dir
        self.day = None
        self.file = None

    def write(self, t, text):
        day = utc(t).strftime("%Y%m%d")
        if day != self.day:
            if self.file:
                self.file.close()
            self.file = open(os.path.join(self.run_dir, f"rtt-{day}.log"), "a", buffering=1)
            self.day = day
        self.file.write(f"{iso(t)}\t{text}\n")

    def close(self):
        if self.file:
            self.file.close()


def read_rtt_log(path):
    """(time, text) from a log RttLog wrote; a line without a timestamp (a plain RTT capture) gets None."""
    with open(path, errors="replace") as f:
        for raw in f:
            raw = raw.rstrip("\n")
            stamp, sep, text = raw.partition("\t")
            if sep:
                try:
                    yield datetime.datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp(), text
                    continue
                except ValueError:
                    pass
            yield None, raw


class Events:
    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "events.jsonl")
        self.recent = []  # This process's events, for the summaries

    def write(self, event):
        with open(self.path, "a") as f:
            f.write(json.dumps({"time": iso(event.t), **event.to_json()}) + "\n")
        self.recent.append(event)

    def all(self):
        """Every event of the run, including those from before a resume."""
        if not os.path.exists(self.path):
            return []
        with open(self.path) as f:
            return [json.loads(line) for line in f if line.strip()]


class Metrics:
    def __init__(self, run_dir):
        self.path = os.path.join(run_dir, "metrics.csv")
        self.fields = None

    def write(self, t, row):
        row = {"time": iso(t), **row}
        new = not os.path.exists(self.path)
        if self.fields is None:
            self.fields = list(row)
        with open(self.path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=self.fields, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow(row)


def _event_table(events):
    if not events:
        return "None.\n"
    lines = ["| Time (UTC) | Severity | Monitor | Message |", "|---|---|---|---|"]
    for e in events:
        lines.append(f"| {e['time']} | {e['severity']} | {e['monitor']} | {e['message'].replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def _counts(events):
    out = {}
    for e in events:
        out[e["severity"]] = out.get(e["severity"], 0) + 1
    return out


def summary(run_dir, info, metrics, events, now):
    """Rewrites summary.md: the run's status, latest counters, and every fail and warn event."""
    counts = _counts(events)
    status = "FAILING" if counts.get("fail") else "OK"
    hours = (now - info["start"]) / 3600
    lines = [
        f"# Soak run {info['id']}",
        "",
        f"- **Status:** {status} ({counts.get('fail', 0)} fail, {counts.get('warn', 0)} warn events)",
        f"- **Started:** {iso(info['start'])}, running {hours:.1f} h (updated {iso(now)})",
        f"- **Flatsat build:** {info.get('build') or 'not seen yet'}",
        f"- **Config:** {info['config']}",
        "",
        "## Latest counters",
        "",
        "| Counter | Value |",
        "|---|---|",
    ]
    lines += [f"| {k} | {v} |" for k, v in metrics.items()]
    lines += ["", "## Failures", "", _event_table([e for e in events if e["severity"] == "fail"])]
    lines += ["## Warnings", "", _event_table([e for e in events if e["severity"] == "warn"])]
    with open(os.path.join(run_dir, "summary.md"), "w") as f:
        f.write("\n".join(lines))


def daily(run_dir, day, metrics, events):
    """daily/<day>.md: that UTC day's events and the counters at its end."""
    os.makedirs(os.path.join(run_dir, "daily"), exist_ok=True)
    todays = [e for e in events if e["time"].startswith(day)]
    counts = _counts(todays)
    lines = [f"# Soak {day} (UTC)", "", f"{counts.get('fail', 0)} fail, {counts.get('warn', 0)} warn, "
             f"{counts.get('info', 0)} info events.", "", "## Counters at the end of the day", "",
             "| Counter | Value |", "|---|---|"]
    lines += [f"| {k} | {v} |" for k, v in metrics.items()]
    lines += ["", "## Events", "", _event_table(todays)]
    with open(os.path.join(run_dir, "daily", f"{day}.md"), "w") as f:
        f.write("\n".join(lines))
