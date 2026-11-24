# What the soak's radio phase will fail on

As of 2026-11-23, for a soak with `[radio] enabled = true` and the HackRF ground station.

- **Firmware read:** PVDXos `main` (a221a4f) and `radio-task` (36df8d9).
- **Ground station:** flatsat-hil `soak-radio` (dbffae2).
- **Testing so far:** none of this has run on hardware yet. The offline tests (`soak/tests`) pass against both branches, but they can't show two of the problems below:
  - the HackRF's transmit-to-receive turnaround, because their radio pair has none;
  - the flatsat's RF hardware.

## Summary

| # | What fails | Firmware | Monitor event | Cause |
|---|---|---|---|---|
| 1 | The flatsat resets on the first ground packet | `main` | `resets` fail, `log` fail (fatal) | Firmware |
| 2 | No uplink ever finishes; every command check fails | both | `uplink` fail, every pass | Ground station (HackRF turnaround) |
| 3 | `ENABLE_DISABLE_DEVICE` is ignored | both | `uplink` warn (`radio-task`) | Firmware not implemented |
| 4 | The camera re-enable check fails even once (3) is implemented | both | `uplink` fail | Camera health never reported |
| 5 | A "not implemented" warning for every ground packet | `radio-task` | `log` warn; fails above 120 an hour | Firmware TODO |
| 6 | Finished PDU says "delivery incomplete" | both | `uplink` warn (once) | Firmware bug |
| 7 | RF link may not work at all | both | `radio` fail ("no beacon", "no SYN_ACK") | Unverified RF settings |

**Run the soak on `radio-task`**, or on a branch with (1) fixed. On `main` the flatsat resets every pass. Even on `radio-task`, (2) stops every uplink until it's fixed. Beacons, telemetry decoding and the PUSP handshakes should still work.

## What each rotation step will do on `radio-task`

Once (2) is fixed. On `main`, (1) resets the flatsat before any of them run.

| Step | Expected |
|---|---|
| `adcs_detumble`, `adcs_point` | Pass. |
| `device_off`, `device_on` | Warn: not implemented (3). With the camera as the toggle device, `device_on` would fail even once implemented (4). |
| `transmit_off`, `transmit_on` | Should pass. Checks done by beacon (the file's timestamp, UTC) wait for beacons after the next pass. If the next SYN's `PUSP_NOTIF_ENABLE_TX` didn't reach the satellite, it would stay silent: `radio` fail "no beacon". |
| `update_display` | Should pass. It's the only multi-PDU file (17 File Data PDUs). Each one the HackRF sends restarts its receiver, so the ground's turn takes about 10 s. |
| `take_iv_curve` | Pass (the perovskite task logs the resolution when the pass ends). |
| `send_iv_curve`, `send_photo` | The file check passes. The downlink itself is a class 2 CFDP send over S-band (the tester's emulated S-band on the bench). The ground has no CFDP receiver and doesn't hear S-band, so no ACK(EOF) or Finished ever comes. The satellite yields its turns while it waits, re-sends its EOF each turn, and drops the transaction when the pass ends ("cfdp: pass ended, dropping txn"). |
| `take_photo` | The log check passes. The capture itself can't be checked (4). |
| `set_kepler`, `adcs_params` | Warn: ADCS logs "operation N not implemented" (`OPERATION_SET_KEPLER_COEFF`, `OPERATION_ADCS_UPDATE_PARAMS`: "TODO: load from MRAM_REGION_ADCS_UPLINK and apply"). |
| `set_power_mode` | Warn: not implemented ("power mode will be set through PUSP"). |
| `reboot` | Should pass: the `resets` monitor is told to expect it. The UTC check after the reboot relies on ADCS restoring UTC from MRAM. |

## 1. `main`: the flatsat resets on the first ground packet

**What happens:**
1. Every ground packet carries a timestamp in its secondary header.
2. The radio forwards that timestamp to ADCS as `OPERATION_SET_UNIX_TIME` (`uhf_rx_classify()`, `src/tasks/uhf_radio/uhf_radio_task.c:208`). This only happens for packets with the ground callsign, which the soak's ground uses.
3. `main`'s ADCS doesn't handle that operation. Its `default:` case calls `fatal("adcs: Invalid operation! ...")` (`src/tasks/adcs/adcs_task.c`).
4. ADCS runs in every state, so the first SYN of the first pass resets the flatsat.

The uplinked `SET_UNIX_TIME` and `SET_ADCS_OP_MODE` commands go the same way. `main` dispatches them as `OPERATION_SET_UNIX_TIME` and `OPERATION_SET_ADCS_OP_MODE`, which ADCS doesn't handle either.

**Soak events:**
- `log`: fail, "fatal error".
- `resets`: fail, "the flatsat reset on its own".
- `radio`: fail, the pass isn't answered.

**`radio-task`** has already turned `OPERATION_SET_UNIX_TIME` into a warning (see 5). It also maps the uplink commands to `OPERATION_ADCS_SET_UTC` and `OPERATION_ADCS_DETUMBLE_ONLY` / `OPERATION_ADCS_DO_POINT`.

## 2. The HackRF misses everything the satellite sends at the start of its turn

The HackRF is half duplex, and `hil/lib/hackrf.py` switches between transmit and receive by stopping one `hackrf_transfer` process and starting another. Each switch is a few hundred milliseconds of USB setup. The time isn't measured yet; the ground records it in `HackRF.turnarounds`. The satellite answers within milliseconds of the ground's last frame, so its first replies arrive while the HackRF can't receive.

**PUSP directives survive, slowly.** The satellite re-sends a reply it gets no answer to every `UHF_RETRY_MS` (1 s): SYN_ACK, YLD_ACK and YLD. So each handshake step costs about one retry. Passes still open and hand turns over, with high re-send counts, but no failure.

**CFDP replies don't survive:**
- CFDP queues its ACK(EOF) and Finished PDUs while the ground has the turn.
- When the ground hands over with SWC, `establish_send_proc()` (`uhf_process.c`) sends everything queued back to back, at once, while the HackRF is still turning around.
- CFDP re-sends them only at the start of the satellite's next turn (`cfdp_turn_retransmit()`), which is the same moment, so they're lost again.

**Consequences:**
- The ground never hears the Finished, so it never sends ACK(Finished).
- The satellite's transaction never completes, and it's dropped when the pass ends ("cfdp: pass ended, dropping txn").
- `uplink_commands_dispatch()` only runs for a completed transaction, so **no uplinked command is ever carried out**.

**Soak events, every pass:**
- `uplink`: fail, "uplink … not finished: the uplink didn't finish".
- `radio`: fail, "pass failed: the uplink didn't finish".
- Because no file is delivered, none of its command checks are queued.

**Fixes, best first:**
- **A turnaround guard in the firmware.** The satellite would wait a configurable time (e.g. `UHF_TURNAROUND_MS`) after receiving a directive before it transmits. Any half-duplex ground station needs this, including the real one. Proposal below.
- **A second receiver on the ground**, e.g. an RTL-SDR on a splitter with the HackRF, that never stops listening.
- **Switching in-process with libhackrf** instead of restarting `hackrf_transfer`. That shortens the gap but won't get it under the satellite's few milliseconds, so on its own it isn't enough.

### Proposal: a turnaround guard in the radio task

Not implemented: this is a proposal for the radio code on `radio-task`.

**The rule.** The satellite never starts a transmission sooner than `UHF_TURNAROUND_MS` after the end of the last frame it received from the ground. That gives the ground time to get from transmitting back to receiving.

**Where.** In `uhf_send_frame_blocking()` (`src/tasks/uhf_radio/uhf_radio_task.c`), the one function every radio-task frame goes through: beacons, PUSP directives and CFDP downlink. Before TXPREP:

```c
// Set by uhf_rx_poll() when a frame from the ground arrives (RXFE)
static TickType_t last_ground_rx_tick;
static bool ground_rx_seen;

// In uhf_send_frame_blocking(), before uhf_go_txprep():
if (ground_rx_seen) {
    const TickType_t ready = last_ground_rx_tick + pdMS_TO_TICKS(UHF_TURNAROUND_MS);
    if ((int32_t)(ready - xTaskGetTickCount()) > 0) {
        vTaskDelay(ready - xTaskGetTickCount()); // The radio stays in RX meanwhile
    }
}
```

**Covering every reply.** Doing it at the send function, rather than in each PUSP handler:
- covers the CFDP frames sent right after SWC, and every reply directive (SYN_ACK, YLD_ACK, YLD, SWC);
- leaves beacons untouched, unless a ground frame arrived just before one;
- means no state handler has to know about it.

**The value.**

```c
// How long the satellite waits after a frame from the ground before transmitting, so a half-duplex ground station can
// switch from transmitting to receiving. Must be agreed with the ground station
#ifndef UHF_TURNAROUND_MS
#define UHF_TURNAROUND_MS 0
#endif
_Static_assert(UHF_TURNAROUND_MS < UHF_RETRY_MS, "a reply must go out before the ground's re-send of what it answers");
```

- The default of 0 keeps today's behaviour.
- The soak builds with `make dev DEFINES="UHF_TURNAROUND_MS=..."`, set to the HackRF's measured turnaround plus a margin. The ground records the turnaround in `HackRF.turnarounds`, and the soak should log it.
- The real ground station needs its own value. Whoever builds it has to agree that value with the flight software.

**Timing it affects:**
- **`UHF_RETRY_MS`** (1 s) must stay longer than the guard plus a reply's airtime, or the ground re-sends a directive before the satellite's answer to it has gone out. The `_Static_assert` covers the guard part.
- **`UHF_TX_WINDOW_MS`** (10 s) is counted from the start of the satellite's turn, so the guard comes out of the window once per turn. That's fine for any guard under about a second.
- **Beacon timing** is unchanged outside passes.

**Testing.**
- **Offline:** give `hackrf.LoopbackRadio` a configurable turnaround, during which the ground's end drops what it receives. Run the CFDP tests with the turnaround on, with `FakeSatellite` both with and without the guard. Without the guard (2) reproduces: no Finished is heard. With it, every uplink finishes.
- **On the bench:** with the guard built in, the soak's uplinks should finish in 2 turns, and the `uplink` monitor should confirm their commands.

## 3. `ENABLE_DISABLE_DEVICE` is ignored

- **`radio-task`:** logs "uplink: command type 0 not implemented yet, ignored" (`uplink_commands.c`, "TODO: to be done through the device mock system"). The `uplink` monitor turns that into one warn event and drops the check.
- **`main`:** sends it to no task (`target = NULL`) and logs that only at debug level. With `DEFAULT_LOG_LEVEL=INFO` the monitor never sees it, so the check would fail, if the satellite got that far (see 1).

This is the command `device-failure-modes`' gate (`device_disabled()`) could carry out.

## 4. Camera health is never reported

`telemetry_update_camera_data()` exists, but nothing calls it on either branch, and `camera.health` starts as `TEL_DEVICE_STATUS_DISABLED`. Gyro and S-band health are the same.

So once (3) is implemented and (2) fixed:
- the "camera disabled" check passes, but only because the field never changed;
- the "camera enabled" check fails: `uplink` fail, "never seen in telemetry … CAMERA_ID enabled (camera.health)".

Until the camera reports its health, set `toggle_device = ""` in `[radio.commands]`, or use `"UHF_ID"`, whose health is reported.

## 5. `radio-task`: a warning for every ground packet

`radio-task`'s ADCS answers `OPERATION_SET_UNIX_TIME`, sent for every ground packet (see 1), with "adcs: operation N not implemented, ignoring" ("TODO: decide whether the UHF task's per-packet ground timestamp should set UTC").

- The `log` monitor gives one warn event for the new message.
- It fails if there are more than 120 non-allowlisted warnings in an hour.
- A pass is 10–30 ground frames, more with the re-sends from (2). The default schedule (a pass every 6 h) stays well under the limit. A bench schedule with passes every few minutes can cross it.
- If it gets in the way, add `"adcs: operation \\d+ not implemented"` to `warning_allowlist`.

## 6. The Finished PDU says "delivery incomplete"

`cfdp_send_fin()` sends `delivery_complete`, which only a send transaction sets (`handle_finished_pdu()`). So every uplink's Finished reports the file as incomplete, even when it arrived whole and its checksum matched. The ground judges the transfer by the condition code instead, and the `uplink` monitor warns once. This only shows once (2) is fixed.

## 7. The RF link itself is unverified

These can stop the link outright. That would show as `radio` fail "no beacon for 175 s" and "pass failed: no SYN_ACK", or as `radio` warn "unreadable frame":

- **Front-end mode.** `at86_init_pvdx()`: "TODO(hardware): which front-end mode RF09 needs depends on how the board's PA/LNA switch is wired".
- **Crystal trim.** `xo_fs` and `xo_trim` are untrimmed, so the carrier may be off frequency. The modem tolerates about ±15 kHz.
- **Placeholder RF settings.** `UHF_FREQ_HZ`, `UHF_TX_POWER` and `UHF_PHY_CONFIG` are still marked "TODO(temp)". The ground reads the same constants, so the two sides agree with each other, but nothing has confirmed they suit the hardware.
- **Bits the datasheet leaves to 802.15.4g.** `hil/lib/mrfsk.py` guesses these:
  - PSDU and SFD bit order;
  - PHR layout;
  - the FCS.

  A wrong guess shows as frames with a bad FCS. "bad FCS but a good USLP frame" means only the FCS guess is wrong.
- **The RSC code.** It isn't confirmed. Coded frames are read from their systematic bits until `soak/tools/fit_rsc.py` pins the code, which is a `radio` info event, not a failure. Error correction only starts after that, which doesn't matter on a cabled link.

## Not expected to fail

- **Beacons and telemetry decoding**, assuming the RF settings in (7) work.
- **The `telemetry` monitor's checks:**
  - fuel gauges against the log;
  - state against the log;
  - boot time;
  - UHF health, which both branches report from the device check.
- **PUSP handshakes**, with the extra re-sends from (2).
