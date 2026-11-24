"""What the ground station uplinks in each pass ([radio.commands] in the config), as an uplink command file
(hil/lib/uplink.py) and the list of commands in it, for the uplink monitor to check in telemetry afterwards.

Every pass sets the satellite's clock (SET_UNIX_TIME), and adds the next step of a rotation: ADCS detumble-only, back
to pointing, the toggle device disabled, enabled again. The rotation follows the pass number, so a resumed run carries
on where it was. Values (command and device numbers, opmodes) come from PVDXos's headers through ccsds.py.
"""

import ccsds
import uplink


def rotation(config):
    """The commands the passes take turns at, each a (name, argument)."""
    r = config.get("radio", {}).get("commands", {})
    steps = []
    if r.get("adcs_opmode", True):
        steps += [("SET_ADCS_OP_MODE", "TEL_ADCS_OPMODE_DETUMBLE_ONLY"), ("SET_ADCS_OP_MODE", "TEL_ADCS_OPMODE_POINT")]
    device = r.get("toggle_device", "CAMERA_ID")
    if device:
        steps += [("ENABLE_DISABLE_DEVICE", (device, False)), ("ENABLE_DISABLE_DEVICE", (device, True))]
    return steps


def plan(config, pass_number, now):
    """(commands, uplink file, file timestamp) for a pass. Each command is a dict the uplink monitor knows:
    {"name": "SET_UNIX_TIME", "value": ...}, {"name": "SET_ADCS_OP_MODE", "value": opmode} or
    {"name": "ENABLE_DISABLE_DEVICE", "device_name": ..., "device": id, "enabled": bool}."""
    c = ccsds.constants()
    r = config.get("radio", {}).get("commands", {})
    commands, built = [], []
    if r.get("set_unix_time", True):
        commands.append({"name": "SET_UNIX_TIME", "value": int(now)})
        built.append(uplink.set_unix_time(int(now)))
    steps = rotation(config)
    if steps:
        name, arg = steps[pass_number % len(steps)]
        if name == "SET_ADCS_OP_MODE":
            commands.append({"name": name, "value": c[arg], "mode_name": arg})
            built.append(uplink.set_adcs_op_mode(c[arg]))
        else:
            device_name, enabled = arg
            commands.append({"name": name, "device_name": device_name, "device": c[device_name], "enabled": enabled})
            built.append(uplink.enable_disable_device(c[device_name], enabled))
    file_timestamp = int(now)
    return commands, uplink.build(built, file_timestamp), file_timestamp


def describe(data):
    """The command types in an uplink file, read back with the satellite's own parser (None if it rejects it)."""
    return ccsds.parse_uplink(data)

