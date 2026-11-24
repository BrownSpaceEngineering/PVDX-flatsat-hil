"""What the ground station uplinks in each pass ([radio.commands] in the config), as an uplink command file
(hil/lib/uplink.py), and how the uplink monitor (monitors.Uplinks) checks each command was carried out.

Every pass sets the satellite's clock (SET_UNIX_TIME, if set_unix_time), and adds the next step of `rotation`, a list
of the step names in STEPS (all of them by default, in that order); leave out the ones you don't want. The rotation
follows the pass number, so a resumed run carries on where it was. Values (command, device and operation numbers,
opmodes, sizes) come from PVDXos's headers through ccsds.py.

Each step is one command and the checks that show the satellite carried it out:

    adcs_detumble, adcs_point  SET_ADCS_OP_MODE    adcs.opmode in telemetry
    device_off, device_on      ENABLE_DISABLE_DEVICE  the toggle device's health field (DISABLED, then OK)
    transmit_off               TRANSMIT_ON_OFF 0   no beacon until the next pass (whose SYN turns transmit back on)
    transmit_on                TRANSMIT_ON_OFF 1   beacons after the pass
    update_display             UPDATE_DISPLAY      the satellite logs the image as saved (an 8 KB file: 17 File Data PDUs)
    take_iv_curve              TAKE_IV_CURVE       the perovskite task logs the new sampling resolution
    send_iv_curve, send_photo  SEND_IV_CURVE, SEND_PHOTO  the satellite queues the file (or logs there is none)
    take_photo                 TAKE_PHOTO          the satellite logs the photo override
    set_kepler                 SET_KEPLER_COEFF    received (nothing in telemetry shows the coefficients)
    adcs_params                ADCS_UPDATE_PARAMS  adcs.tle_epoch_unix_time in telemetry is the TLE's epoch
    set_power_mode             SET_PWR_MODE        received (power modes aren't defined yet)
    reboot                     REBOOT              the flatsat boots after the pass (a commanded reboot, not a failure)

Every file is also checked for in command_timestamps (except a reboot's, which the reboot clears). A command the
satellite logs as not implemented turns its checks into a warn event (see Uplinks).
"""

import time

import ccsds
import uplink

STEPS = ("adcs_detumble", "adcs_point", "device_off", "device_on", "transmit_off", "transmit_on", "update_display",
         "take_iv_curve", "send_iv_curve", "take_photo", "send_photo", "set_kepler", "adcs_params", "set_power_mode",
         "reboot")

# A low Earth orbit TLE's mean elements, for ADCS_UPDATE_PARAMS (epoch: the pass's time)
TLE = {"inclination": 51.64, "raan": 120.0, "eccentricity": 0.0005, "argument_of_perigee": 90.0, "mean_anomaly": 270.0,
       "mean_motion": 15.5, "bstar": 3.0e-5}
# SET_KEPLER_COEFF's six values. Their meaning isn't defined yet; these are a semi-major axis (m), eccentricity and
# four angles (degrees) of the same orbit
KEPLER = (6_778_000.0, 0.0005, 51.64, 120.0, 90.0, 270.0)
PHOTO_DELAY_S = 120  # TAKE_PHOTO takes the picture this long after the pass


def rotation(config):
    """The step names the passes take turns at, checked against STEPS."""
    r = config.get("radio", {}).get("commands", {})
    steps = list(r.get("rotation", STEPS))
    unknown = [s for s in steps if s not in STEPS]
    if unknown:
        raise ValueError(f"unknown [radio.commands] rotation steps {unknown}; the steps are {list(STEPS)}")
    return steps


def display_pattern(pass_number):
    """A 256 x 64, 4-bit image: a horizontal gradient with a bar that moves with the pass number."""
    size = ccsds.constants()["UPLINK_DISPLAY_BITMAP_SIZE"]
    width, height = 256, 64
    bar = (pass_number * 16) % width
    out = bytearray()
    for y in range(height):
        for x in range(0, width, 2):
            levels = [15 if bar <= px < bar + 16 else px // 16 for px in (x, x + 1)]
            out.append((levels[0] << 4) | levels[1])
    assert len(out) == size, "the display bitmap isn't 256 x 64 pixels at 4 bits each any more"
    return bytes(out)


def _ignored_by_adcs(operation):
    """A regex for ADCS's log line when it doesn't implement an operation (radio-task's ADCS task)."""
    return rf"adcs: operation {ccsds.constants()[operation]} not implemented"


def _step(name, config, pass_number, now):
    """(command for the file, description for passes.jsonl, checks) for a rotation step."""
    c = ccsds.constants()
    if name in ("adcs_detumble", "adcs_point"):
        mode = "TEL_ADCS_OPMODE_DETUMBLE_ONLY" if name == "adcs_detumble" else "TEL_ADCS_OPMODE_POINT"
        return (uplink.set_adcs_op_mode(c[mode]), {"name": "SET_ADCS_OP_MODE", "value": c[mode], "mode_name": mode},
                [{"what": "field", "field": "adcs.opmode", "value": c[mode], "label": f"ADCS opmode {mode}"}])
    if name in ("device_off", "device_on"):
        device = config.get("radio", {}).get("commands", {}).get("toggle_device", "CAMERA_ID")
        enabled = name == "device_on"
        field = HEALTH_FIELD.get(device)
        want = c["TEL_DEVICE_STATUS_OK" if enabled else "TEL_DEVICE_STATUS_DISABLED"]
        checks = [{"what": "field", "field": field, "value": want,
                   "label": f"{device} {'enabled' if enabled else 'disabled'} ({field})"}] if field else []
        return (uplink.enable_disable_device(c[device], enabled),
                {"name": "ENABLE_DISABLE_DEVICE", "device_name": device, "device": c[device], "enabled": enabled}, checks)
    if name == "transmit_off":
        return (uplink.transmit_on_off(False), {"name": "TRANSMIT_ON_OFF", "value": 0},
                [{"what": "silent", "label": "no beacon after TRANSMIT_ON_OFF off"}])
    if name == "transmit_on":
        return (uplink.transmit_on_off(True), {"name": "TRANSMIT_ON_OFF", "value": 1},
                [{"what": "beacon", "label": "beacons after TRANSMIT_ON_OFF on"}])
    if name == "update_display":
        return (uplink.update_display(display_pattern(pass_number)), {"name": "UPDATE_DISPLAY"},
                [{"what": "log", "pattern": r"uplink: display image saved", "from_pass_start": True,
                  "label": "display image saved"}])
    if name == "take_iv_curve":
        steps = ccsds.header_int("src/tasks/perovskite/perovskite_task.h", "PEROVSKITE_STEP_COUNT")
        return (uplink.take_iv_curve(steps), {"name": "TAKE_IV_CURVE", "value": steps},
                [{"what": "log", "pattern": rf"perovskite: Sampling resolution set to {steps} steps",
                  "label": f"perovskite sampling resolution {steps}"}])
    if name == "send_iv_curve":
        return (uplink.send_iv_curve(), {"name": "SEND_IV_CURVE"},
                [{"what": "log", "pattern": r"uplink: (queued put of file|no IV curve in MRAM to send)",
                  "from_pass_start": True, "label": "IV curve queued for downlink (or none to send)"}])
    if name == "take_photo":
        at = int(now) + PHOTO_DELAY_S
        return (uplink.take_photo(at), {"name": "TAKE_PHOTO", "value": at},
                [{"what": "log", "pattern": rf"uplink: photo override at {at}\b", "from_pass_start": True,
                  "label": "photo override set"}])
    if name == "send_photo":
        return (uplink.send_photo(1), {"name": "SEND_PHOTO", "value": 1},
                [{"what": "log", "pattern": r"uplink: (queued put of file|no file \d+ \(index \d+\) in MRAM to send)",
                  "from_pass_start": True, "label": "photo queued for downlink (or none to send)"}])
    if name == "set_kepler":
        return (uplink.set_kepler_coeff(KEPLER), {"name": "SET_KEPLER_COEFF", "value": list(KEPLER)},
                [{"what": "none", "label": "Kepler coefficients applied", "adcs_op": "OPERATION_SET_KEPLER_COEFF"}])
    if name == "adcs_params":
        epoch_jd = now / 86400.0 + uplink.JD_UNIX_EPOCH
        return (uplink.adcs_update_params(epoch_jd, **TLE), {"name": "ADCS_UPDATE_PARAMS", "epoch_jd": epoch_jd, **TLE},
                # The satellite truncates the epoch to whole seconds, so it may be a second off either way
                [{"what": "field", "field": "adcs.tle_epoch_unix_time", "value": int(now), "tolerance": 1,
                  "label": "TLE loaded (adcs.tle_epoch_unix_time)", "adcs_op": "OPERATION_ADCS_UPDATE_PARAMS"}])
    if name == "set_power_mode":
        return (uplink.set_power_mode(0), {"name": "SET_PWR_MODE", "value": 0},
                [{"what": "none", "label": "power mode applied"}])
    if name == "reboot":
        return (uplink.reboot(1), {"name": "REBOOT", "value": 1},
                [{"what": "boot", "label": "commanded reboot"}])
    raise ValueError(f"unknown step {name}")


# Telemetry health field of each device that has one (device_id_t name -> tel_downlink_t field)
HEALTH_FIELD = {"GYROSCOPE_ID": "gyro.health", "CAMERA_ID": "camera.health", "DISPLAY_ID": "display_health",
                "UHF_ID": "uhf_health", "SBAND_ID": "sband.health"}


def plan(config, pass_number, now=None):
    """What a pass uplinks: {"commands": [description, ...], "data": the file, "file_timestamp": ...,
    "checks": [check, ...], "reboot": bool, "transmit_off": bool}. Each check names its command's type (and, for one
    ADCS carries out, its ADCS operation), so a "not implemented" log line can be matched to it."""
    now = time.time() if now is None else now
    c = ccsds.constants()
    r = config.get("radio", {}).get("commands", {})
    commands, built, checks = [], [], []
    if r.get("set_unix_time", True):
        commands.append({"name": "SET_UNIX_TIME", "value": int(now)})
        built.append(uplink.set_unix_time(int(now)))
        checks.append({"what": "utc", "label": "UTC set (UTC-valid flag and the ground's time)",
                       "type": c["SET_UNIX_TIME"], "command": "SET_UNIX_TIME"})
    steps = rotation(config)
    step = steps[pass_number % len(steps)] if steps else None
    if step is not None:
        cmd, description, step_checks = _step(step, config, pass_number, now)
        commands.append(dict(description, step=step))
        built.append(cmd)
        for check in step_checks:
            check["type"] = c[description["name"]]
            check["command"] = description["name"]
            if "adcs_op" in check:
                check["ignored"] = _ignored_by_adcs(check.pop("adcs_op"))
            checks.append(check)
    file_timestamp = int(now)
    return {"commands": commands, "data": uplink.build(built, file_timestamp) if built else b"",
            "file_timestamp": file_timestamp, "checks": checks, "reboot": step == "reboot",
            "transmit_off": step == "transmit_off", "step": step}


def describe(data):
    """The command types in an uplink file, read back with the satellite's own parser (None if it rejects it)."""
    return ccsds.parse_uplink(data)

