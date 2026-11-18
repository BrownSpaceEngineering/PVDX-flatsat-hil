#!/bin/sh
# Starts the soak, or resumes the run that soak/runs/current points at, keeping the Mac awake while it runs.
# launchd (soak/com.pvdx.flatsat-soak.plist) runs this at login and again if it exits; `rm soak/runs/current` before
# starting a fresh run. Extra arguments go to run.py for a new run (e.g. --config my.toml --hours 336).
#
# PYTHON: the interpreter with hil/requirements.txt installed (default: python3)
# HIL_FLATSAT_JLINK, HIL_TESTER_JLINK: the probe serial numbers (recommended for unattended runs)

set -e
cd "$(dirname "$0")"
PYTHON=${PYTHON:-python3}
mkdir -p runs
if [ -e runs/current/state.json ]; then
    exec caffeinate -i "$PYTHON" run.py --resume runs/current
fi
run_dir="runs/$(date -u +%Y%m%dT%H%M%SZ)"
ln -sfn "$(basename "$run_dir")" runs/current
exec caffeinate -i "$PYTHON" run.py --run-dir "$run_dir" "$@"
