# Flatsat hardware-in-the-loop tests

A second Metro M4 Grand Central (the **tester**, firmware in `tester/`) stands in for the S-band radio, which the flatsat
doesn't have, drives its analog inputs and the perovskite board's pixels, and records its digital outputs:

This is the PVDX-flatsat-hil repo, a submodule of PVDX mounted at `flatsat-hil/`. Paths starting `tester/` or `hil/`
are in this repo; `src/...` paths, `make dev` and the other `make` targets are PVDX's, run from the PVDX root. The suite
reads the flatsat's sources and `src/PVDXos.elf` from the PVDX checkout around it (or from `$PVDX_ROOT`).

- **Emulated devices:** the S-band radio (STXG2): control over I2C, data over SPI. The perovskite measurement board
  (MCP23017 and INA226 over I2C, with a model of its 16 pixels), wired in place of the real one. The four battery fuel
  gauges (MAX17205), one on each channel of the flatsat's own TCA9546A mux, wired in place of the EPS's.
- **Driven inputs:** 4 photodiode voltages, 4 thermistor voltages, and 4 perovskite cells on the flatsat's real
  perovskite measurement board (PVDX-ADCS-PMB).

- **Recorded outputs:** the magnetorquer H-bridges (IN1, IN2, nSLEEP per axis), heaters, burn wire and S-band EN/RST,
  all 14 at once, each edge timed to about 1 us (`tester/src/watch.c`).

The real gyro, magnetometer, MRAM, camera, display, UHF radio and batteries stay connected, and the EPS's fuel gauges
unless the emulated ones are wired. The suite checks that they read plausibly.

The host suite (`pytest`) talks to the tester over SEGGER RTT through a second J-Link on the tester's SWD header
(`lib/tester.py`), and reads the PVDXos RTT log through the flatsat's J-Link (`lib/flatsat.py`). It asserts on both,
and on the tester's recordings of the flatsat's outputs (`lib/logic.py`).

## Setup

```bash
git submodule update --init flatsat-hil           # from the PVDX root
python3 -m venv .venv && .venv/bin/pip install -r flatsat-hil/hil/requirements.txt
make tester                                      # builds tester/tester.elf (from src/, common/ and the generated ASF/)
make tester_flash JLINK_SERIAL=<tester J-Link>   # through the tester's J-Link (its UF2 bootloader is kept)
make dev && make flash_to_mram                   # the flatsat build under test (DEVBUILD: the tests read debug logs)
```

**Two J-Links.** One goes on the flatsat as usual, the other on the Grand Central's 10-pin SWD header. Each probe's
serial number is printed by `JLinkExe` (`ShowEmuList`) and on its label. The suite tells the probes apart by looking
for the tester's RTT channel, so the tester must be running a firmware that matches `tester/tester.elf`. Setting
`HIL_FLATSAT_JLINK` and `HIL_TESTER_JLINK` to the serial numbers skips that.

`make flash_to_mram`, `make reset_mcu` and the `tools/` wrappers don't choose a probe. With both plugged in, the
J-Link software asks which one to use (or unplug the tester's while using them).

`hil/lib/flatsat.py` reads `src/PVDXos.elf` and `hil/lib/tester.py` reads `tester/tester.elf` to find the RTT buffers.
Run the suite from the tree whose builds are on the boards.

The tester disconnects its analog outputs 10 s after the last command; while the suite runs it pings every 3 s.

## Wiring: tester to flatsat

The tester drives 4 photodiodes (phd0-3, on PHD_0, 5, 10 and 15), 4 perovskite cells (pvd0-3) on the flatsat's real
perovskite measurement board (PVDX-ADCS-PMB, schematics in PVDXdevref `boards/adcs-pmb/`) and 4 thermistors (therm0-3),
and emulates the S-band radio. The pins are in the tester's Atmel START project (`tester/atmel_start_config.atstart`,
generated into `tester/ASF/`), `tester/src/board.h` and `tester/src/analog_out.c`; the pixels each cell is on are
`PVD_PIXELS` in `hil/lib/tester.py`, and the photodiodes each phd output drives are `PHOTODIODE_CHANNELS`. Every analog
output stays high impedance until the suite arms it, and disconnects when the suite stops talking to the tester.

Every PWM line into a flatsat ADC needs an RC filter (10 kΩ in series, then 1 µF to ground on the flatsat side): the
flatsat averages only 4 instantaneous samples, so raw PWM reads as 0, 1023, 2047, 3071 or 4095. Nothing else may load
those pins: take the flatsat's own thermistors and their divider resistors, and anything else on PHD_x, off the pins the
tester drives (the expected voltages are the divider node's, `models.thermistor_volts`). Tie the other 14 photodiode
inputs to ground: left floating they read as lit, so the flatsat never sees eclipse, which `test_boot.py`'s dark start
and `test_state_machine.py` need. The cells need only the series resistor, because the perovskite board's INA226
integrates over each conversion.

| From | To | Through | Signal |
|---|---|---|---|
| Tester D45 (PC10), phd0 | Flatsat PHD_0 (PC00) | RC | Photodiode 0 |
| Tester D44 (PC11), phd1 | Flatsat PHD_5 (PA05) | RC | Photodiode 5 |
| Tester D41 (PC12), phd2 | Flatsat PHD_10 (PA10) | RC | Photodiode 10 |
| Tester D40 (PC13), phd3 | Flatsat PHD_15 (PB03) | RC | Photodiode 15 |
| Tester D43 (PC14), pvd0 | PMB J5-1, PIXEL1+ (pixel 0) | 10 kΩ | Cell, Voc 0.9 V |
| Tester D42 (PC15), pvd1 | PMB J5-6, PIXEL6+ (pixel 5) | 10 kΩ | Cell, Voc 0.6 V |
| Tester D6 (PD20), pvd2 | PMB J5-12, PIXEL11+ (pixel 10) | 10 kΩ | Cell, Voc 0.3 V |
| Tester D7 (PD21), pvd3 | PMB J5-17, PIXEL16+ (pixel 15) | 10 kΩ | Cell, 0 V (dark: only sinks current) |
| Tester D35 (PA18), therm0 | Flatsat THERM_0 (PB04) | RC | Thermistor 0 |
| Tester D34 (PA19), therm1 | Flatsat THERM_1 (PB05) | RC | Thermistor 1 |
| Tester D15 (PB17), therm2 | Flatsat THERM_2 (PB06) | RC | Thermistor 2 |
| Tester D11 (PB23), therm3 | Flatsat THERM_3 (PB07) | RC | Thermistor 3 |
| Tester D25/SDA1 (PC16) | Flatsat SBAND_SDA (PA17) | | S-band I2C target, 0x26 (the flatsat's internal pull-ups) |
| Tester D24/SCL1 (PC17) | Flatsat SBAND_SCL (PA16) | | |
| Tester D50 (PD11) | Flatsat SBAND_MOSI (PB16) | | S-band SPI slave, data in |
| Tester D52 (PD09) | Flatsat SBAND_SCK (PB17) | | |
| Tester D53 (PD10) | Flatsat SBAND_CS (PB19) | | |
| Tester GND | Flatsat GND, PMB J5-9 | | |
| Flatsat PVD DAC (PA02) | PMB J6-1, VDAC | | Pixel voltage: VDAC x 12/33 (R1 21k, R2 12k to ground), 0-1.2 V, through a follower |
| Flatsat I2C_SENSORS SDA (PD09) | PMB J6-2, SDA | | MCP23017 0x20, INA226 0x40 |
| Flatsat I2C_SENSORS SCL (PD08) | PMB J6-3, SCK | | |
| Flatsat 3V3, GND | PMB J4-2, J4-1 | | PMB power |

**Emulated perovskite board**, instead of the real one (disconnect the real board's J6: both answer at 0x20 and 0x40).
The pvd outputs then do nothing. The tests that need it are `test_perovskite_emulated.py` and the perovskite faults in
`test_boot.py`; `test_perovskite.py` needs the real board. Each of them checks after boot which board answered, and
skips if it's the other one.

The tester measures PVD_CRNT against its own supply scaled to the flatsat's (see the full scale below), so the
follower voltage it serves is right relative to what the flatsat commanded, not in absolute volts (about 1% off when
the flatsat's 3V3 is).

| From | To | Signal |
|---|---|---|
| Flatsat I2C_SENSORS SDA (PD09) | Tester D20/SDA (PB20) | MCP23017 0x20, INA226 0x40 (the flatsat's pull-ups) |
| Flatsat I2C_SENSORS SCL (PD08) | Tester D21/SCL (PB21) | |
| Flatsat PVD DAC (PA02) | Tester A7 (PB04) | Pixel voltage: the tester applies the PMB's divider, x 12/33 |

**Emulated fuel gauges**, instead of the EPS's. The flatsat's TCA9546A mux (0x70, on the compute board) stays in
circuit; the tester answers as the gauge (0x36 and 0x0B) on each of its four downstream channels, SDn/SCn on the
compute board's EPS header. Disconnect the EPS's gauges from those eight lines (both would answer), and keep the
batteries' power wiring as it is. The channels are pulled up on the compute board; the tester also turns on its weak
internal pull-ups, which only matter if a channel is left unwired. The tests that need these are in
`test_fuel_gauges.py`; the fuel gauge tests in `test_real_devices.py` need the EPS's. Each checks after boot which
gauges answered, and skips if it's the other ones.

| From | To | Signal |
|---|---|---|
| Flatsat SD0 / SC0 (mux channel 0) | Tester D37 (PA16) / D36 (PA17) | Pack 0's gauge, SERCOM1 |
| Flatsat SD1 / SC1 (mux channel 1) | Tester D30 (PA23) / D31 (PA22) | Pack 1's gauge, SERCOM5 |
| Flatsat SD2 / SC2 (mux channel 2) | Tester D18 (PB12) / D19 (PB13) | Pack 2's gauge, SERCOM4 |
| Flatsat SD3 / SC3 (mux channel 3) | Tester D1 (PB24) / D0 (PB25) | Pack 3's gauge, SERCOM0 |

## Wiring: flatsat outputs to tester

Each output goes to its own tester input, through 1 kΩ in series so a powered flatsat never feeds an unpowered tester.
The inputs have pull-downs, so one left unwired reads low.

| Signal | Flatsat MCU pin | Tester input |
|---|---|---|
| mtq_x_in1 / mtq_x_in2 | PA20 / PA21 | D2 (PC18) / D3 (PC19) |
| mtq_y_in1 / mtq_y_in2 | PA22 / PA23 | D4 (PC20) / D5 (PC21) |
| mtq_z_in1 / mtq_z_in2 | PD20 / PD21 | D46 (PC06) / D47 (PC07) |
| mtq_x_slp / mtq_y_slp / mtq_z_slp | PB30 / PB31 / PC27 | D14 (PB16) / A4 (PC01) / D17 (PC23) |
| heater1 / heater2 | PC23 / PC24 | A11 (PB08) / D26 (PA12) |
| burn | PB29 | D27 (PA13) |
| sband_en / sband_rst | PC25 / PC26 | D28 (PA14) / D23 (PA15) |

## Running

```bash
make hil                                   # everything that can run; same as: cd flatsat-hil/hil && pytest
cd flatsat-hil/hil && pytest tests/test_boot.py -v     # one area
cd flatsat-hil/hil && pytest -m "not slow"             # skip the perovskite sweeps and burn tests (minutes each)
cd flatsat-hil/hil && pytest -m manual -s              # tests that need someone to rotate the board
```

Running the suite resets the flatsat many times through its J-Link, and never flashes either board. Only one session
can use a J-Link at a time, so stop any GDB server or RTT viewer on either probe first.

## What is covered

| Test file | Checks |
|---|---|
| `test_boot.py` | The S-band, perovskite, photodiode and thermistor startup checks pass. Each emulated S-band and perovskite-board fault (NACK, bad ID, held bus) fails its check without stopping the boot. A dark, cold start boots. |
| `test_photodiodes.py` | PHD_0, 5, 10 and 15 each read their own voltage (catches swapped wires and `PHD_CFG` mistakes). |
| `test_thermal.py` | The thermistor check reads the simulated temperatures. One open thermistor is reported but passes it (its partner stands in); all four open fail it. Heater hysteresis and partner fallback are tested on the heater lines. |
| `test_magnetorquers.py` | IN1 and IN2 never both high (brake). 1 kHz PWM within each burst. Each burst's duty matches the duty ADCS logged for it, in order. nSLEEP rises at least t_wake before each burst and drops between bursts. Manual: B-dot reverses with rotation. |
| `test_perovskite_emulated.py` | A full sweep on the emulated perovskite board: each logged step matches the pixel the tester had routed, the current it served and the follower voltage it measured, one pixel at a time, every pixel visited, up to 1.2 V. A never-ready INA226 fails each pixel after a few steps, and a stuck MCP23017 latch is caught. The host's pixel model matches the tester's. |
| `test_perovskite.py` | A full sweep on the real perovskite board: each cell pixel follows its IV line (Voc, Isc, and the slope, which checks the pixel voltage the flatsat logs), the other pixels read open, and dark cells follow -V/R. |
| `test_sband.py` | ID and scratchpad. EN/RST power sequencing. The driver only talks to the radio while it's powered and out of reset, and only touches registers the radio has, in modes that accept them. |
| `test_state_machine.py` | IDLE, FAKE_HEARTBEAT, IDLE, PEROVSKITE_CAPTURE timing, in eclipse. |
| `test_fuel_gauges.py` | With the emulated gauges: each pack reads what its own gauge served (catches mux channel mix-ups), the battery level is the lowest pack, and the flatsat never writes a gauge. A dead gauge is left out of the level; with none the battery reads full. A low battery takes the state machine to LOW_POWER_IDLE and a recovered one back to IDLE. A gauge holding the bus doesn't stop the flatsat, which reads every pack again once it lets go. |
| `test_real_devices.py` | Real devices pass their checks (the display has none: it is write-only). The magnetometer reads the Earth's field and the gyro reads still, from the sensor line ADCS logs every ~5 s. With the EPS's gauges wired, they pass their check and report a battery level. |
| `test_burn.py` | The burn pin stays low once burned. A short-wait build fires exactly one pulse of the logged length, and resumes its wait after a reset. |
| `test_uhf.py` | With a HackRF: UHF beacons are on the air. |

A test during which the flatsat reports dropped log lines (`[!] N log lines dropped`, its RTT buffer was full) fails:
its log checks can't be trusted.

Tests for behaviour the firmware doesn't have yet are marked `xfail` with the TODO they wait on, so they start passing
when it lands:
- **Thermistor task:** no state enables the thermistor task.
- **S-band data path:** nothing calls `stxg2_store()` yet.

**Not covered:** the fuel gauges' shared ALRT line (EPS IRQ), which the tester doesn't drive, so the alert scan
(`fg_scan_alerts()`) isn't exercised.

## Soak test (`soak/`)

A run of days to weeks with no resets: the satellite has to keep running on its own. `soak/run.py` plays a simulated
orbit to the flatsat through the tester (sunlight and eclipse on the photodiodes, perovskite cells and emulated pixels,
temperatures swinging across the heater thresholds), records everything the flatsat logs, and runs monitors over it.
It only reads RTT, which doesn't disturb the target, and never resets or halts either board, so every reset it sees is
one the satellite did.

**Setup.** As for the suite, with the real batteries and charger on the flatsat (charger input from a current-limited,
fused bench supply; batteries in a LiPo bag). Flash a DEVBUILD with `make flash_to_mram` and wait for "golden copy
written from flash". Set `HIL_FLATSAT_JLINK` and `HIL_TESTER_JLINK`: identifying the probes connects to each, which
the runner does only once. The tester must be connected when the runner starts.

**Running.**

    python soak/run.py --hours 24                # foreground; Ctrl-C ends it cleanly
    soak/start.sh                                # new run, or resume soak/runs/current, under caffeinate
    python soak/run.py --resume soak/runs/<id>   # carry on after a restart
    python soak/run.py --replay rtt.log          # monitors over a recorded log, offline
    pytest soak/tests                            # the monitors against crafted logs, no hardware

For an unattended run, edit `soak/com.pvdx.flatsat-soak.plist` and load it with `launchctl`: it starts `start.sh` at
login and restarts it if the runner crashes, and the run resumes from its checkpoint. Settings are in
`soak/config.toml`, copied into the run directory when a run starts (shorten `[orbit] period_s` to cycle faster).

**Monitors** (`soak/monitors.py`). Each writes an event: fail, warn, or info.

| Monitor | Fails when |
|---|---|
| liveness | No log line for `max_log_silence_s`. |
| resets | A boot that wasn't commanded, or the MRAM boot count skipping boots the log didn't show. |
| log | Any fatal or "log lines dropped"; more than `max_warnings_per_hour` warnings off the allowlist. Each new kind of warning is a warn event. |
| state_machine | A state held longer than its `max_state_s`. |
| payload | No perovskite sweep finished in `max_sweep_gap_s`. |
| power | The logged battery level below `min_battery_level`. |
| outputs | The hourly edge capture shows a magnetorquer braking (IN1 and IN2 both high) or the burn pin high. |

**Results** in `soak/runs/<id>/` (ignored by git): `summary.md` (status, counters, every fail and warn), `daily/`,
`events.jsonl`, `metrics.csv` (once a minute), and the full flatsat log as `rtt-YYYYMMDD.log`. A live run exits 0
when it ends as asked, whatever it found; read `summary.md`.

**Radio.** With `[radio] enabled = true`, a HackRF ground station (`hil/lib/ground.py`) listens for the satellite's
telemetry beacons all the time and, in each `[passes]` window, opens the UHF link (PUSP: SYN, SYN_ACK, SRT), uplinks a
command file over CFDP and trades turns until the transfer is done. Use a cabled link with at least 60 dB of attenuation.

What each pass uplinks (`[radio.commands]`, `soak/uplinks.py`): `SET_UNIX_TIME` every pass, plus the next step of a
rotation: ADCS detumble-only, back to pointing, a device (`toggle_device`, the camera by default) disabled, then enabled.
Every frame the ground sends also carries the time, which the satellite sets its clock from.

Monitors:
- `radio`: fails on a beacon gap of more than 2.5 beacon periods outside passes, or a pass the satellite doesn't
  answer; missed bursts (VC frame count steps) and unreadable frames are warn events. Its metrics include how coded
  frames were decoded and the coded bit error rate the Viterbi decoder corrected (the link's margin).
- `uplink`: fails an uplink the satellite doesn't finish, and any command that doesn't show in a beacon within
  `uplink_settle_s` of its pass (the file's timestamp in `command_timestamps`, UTC-valid and the ground's time, the
  ADCS opmode, the device's health field). A command the satellite logs as not implemented is a warn event instead.
- `telemetry`: compares every beacon with the flatsat's log (fuel gauge readings, state, boot time, UHF health) and
  warns on the first mismatch of each kind.

The run directory gets `frames.jsonl` (every frame heard), `telemetry.jsonl` (every beacon, decoded), `passes.jsonl`
(every pass, its commands and its CFDP transfer) and `coded.jsonl` (coded frames read from their systematic bits, see
below).

The ground speaks exactly what the satellite speaks, and follows it as the radio code changes. `PVDX_ROOT` must point at
the PVDXos checkout the flatsat's build came from (the radio code differs between `main` and `radio-task`); the runner
warns when the build's commit isn't that checkout's.
- `hil/lib/ccsds.py` builds PVDXos's own SPP, USLP, CFDP PDU parsers, uplink parser and telemetry serializer for the
  Mac (`hil/lib/ccsds_host/`) and calls them through ctypes. Integer macros of the radio headers and the members of the
  enums it needs (commands, PUSP directives, device IDs, health values) come out of a table generated from the headers
  and checked by the compiler; VC routes, callsigns and the PHY settings are read from the sources.
- `hil/lib/telemetry.py` decodes beacons. The layout is learned from the serializer itself, one field at a time, and the
  run stops at startup if `tel_downlink_t` has a field the ground's table (`ground_api.c`) doesn't list.
- `hil/lib/cfdp.py` sends the uplink file as a class 2 transaction (Metadata, File Data, EOF; ACK(EOF) and Finished
  back; ACK(Finished), and anything NAKed, in the next turn).
- `hil/lib/mrfsk.py` is the 802.15.4g MR-FSK modem (2-FSK, 50 ksym/s, h = 0.5, BT 0.5). Coded frames (SFD1) go
  through a soft Viterbi decoder; the runner checks the satellite's PHY settings (`at86_pvdx.c`) against the modem's at
  startup. The ground sends uncoded frames, which the satellite also receives, until the code is confirmed.
- `hil/lib/hackrf.py` drives the HackRF through `hackrf_transfer` (half duplex: each transmit stops and restarts the
  receiver; the turnaround is recorded) and has an in-memory radio pair for tests.

**Pinning the RSC code.** The satellite codes with 802.15.4g's recursive systematic code, whose exact form the modem only
guesses (`mrfsk.RSC`, `confirmed=False`). Until it's confirmed, coded frames are read from their systematic bits (every
other coded bit is the data) and checked by their FCS, with no error correction, which a cabled link doesn't need. After
the first run with the HackRF, `python soak/tools/fit_rsc.py soak/runs/<id>/coded.jsonl` finds the code whose parity
bits match the captured frames; put the line it prints in `mrfsk.py` and the frames are Viterbi-decoded from then on.

`pytest soak/tests` (with `PVDX_ROOT` if needed) runs everything offline: framing, the coded modem with noise and
frequency offset, telemetry against PVDXos's serializer, CFDP and PUSP with lost frames (against a transcription of the
satellite's handlers), the monitors, a whole soak loop with the ground station, and `fit_rsc.py`.

**What the satellite still lacks** (the soak reports each one):
- Its Finished PDU always says delivery incomplete: `cfdp_send_fin()` sends `delivery_complete`, which only a send
  transaction sets. The ground judges an uplink by the condition code and warns once.
- `ENABLE_DISABLE_DEVICE` is ignored on both `main` and `radio-task` (a warn event).
- On `main`, `SET_UNIX_TIME` and `SET_ADCS_OP_MODE` go to ADCS operations the ADCS task doesn't handle, so their checks
  fail; `radio-task` handles them.

**Firmware for a soak:** `make dev DEFINES="DEFAULT_LOG_LEVEL=INFO"` keeps the log volume down (DEBUG eventually drops
lines), and `DEFINES="configINITIAL_TICK_COUNT=0xFFC91780"` starts FreeRTOS's tick an hour before it wraps, so the
49.7-day rollover happens early in the run. The log-level override needs the `soak-radio` branch's `globals.h` change;
the tick count works on any build.
