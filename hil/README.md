# Flatsat hardware-in-the-loop tests

A second Metro M4 Grand Central (the **tester**, firmware in `tester/`) stands in for the S-band radio, which the flatsat
doesn't have, drives its analog inputs and the perovskite board's pixels, and records its digital outputs:

This is the PVDX-flatsat-hil repo, a submodule of PVDX mounted at `flatsat-hil/`. Paths starting `tester/` or `hil/`
are in this repo; `src/...` paths, `make dev` and the other `make` targets are PVDX's, run from the PVDX root. The suite
reads the flatsat's sources and `src/PVDXos.elf` from the PVDX checkout around it (or from `$PVDX_ROOT`).

- **Emulated devices:** the S-band radio (STXG2): control over I2C, data over SPI. The perovskite measurement board
  (MCP23017 and INA226 over I2C, with a model of its 16 pixels), wired in place of the real one.
- **Driven inputs:** 4 photodiode voltages, 4 thermistor voltages, and 4 perovskite cells on the flatsat's real
  perovskite measurement board (PVDX-ADCS-PMB).

- **Recorded outputs:** the magnetorquer H-bridges (IN1, IN2, nSLEEP per axis), heaters, burn wire and S-band EN/RST,
  all 14 at once, each edge timed to about 1 us (`tester/src/watch.c`).

The real gyro, magnetometer, MRAM, camera, display, UHF radio and EPS (fuel gauges, batteries) stay connected. The
suite checks that they read plausibly.

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
flatsat averages only 4 instantaneous samples, so raw PWM reads as 0, 1023, 2047, 3071 or 4095. The cells need only the
series resistor, because the perovskite board's INA226 integrates over each conversion.

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
| Flatsat PVD DAC (PA02) | PMB J6-1, VDAC | | Pixel voltage (21k/12k divider and follower on the PMB) |
| Flatsat I2C_SENSORS SDA (PD09) | PMB J6-2, SDA | | MCP23017 0x20, INA226 0x40 |
| Flatsat I2C_SENSORS SCL (PD08) | PMB J6-3, SCK | | |
| Flatsat 3V3, GND | PMB J4-2, J4-1 | | PMB power |

**Emulated perovskite board**, instead of the real one (disconnect the real board's J6: both answer at 0x20 and 0x40).
The pvd outputs then do nothing. The tests that need it are `test_perovskite_emulated.py` and the perovskite faults in
`test_boot.py`; `test_perovskite.py` needs the real board.

| From | To | Signal |
|---|---|---|
| Flatsat I2C_SENSORS SDA (PD09) | Tester D20/SDA (PB20) | MCP23017 0x20, INA226 0x40 (the flatsat's pull-ups) |
| Flatsat I2C_SENSORS SCL (PD08) | Tester D21/SCL (PB21) | |
| Flatsat PVD DAC (PA02) | Tester A7 (PB04) | Pixel voltage: the tester applies the PMB's 21k/12k divider |

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
| `test_thermal.py` | The thermistor check reads the simulated temperatures. An open thermistor fails it. Heater hysteresis and partner fallback are tested on the heater lines. |
| `test_magnetorquers.py` | No shoot-through. 1 kHz PWM. Each burst's duty matches the duty ADCS logged. nSLEEP high while driving. Manual: B-dot reverses with rotation. |
| `test_perovskite_emulated.py` | A full sweep on the emulated perovskite board: each logged step matches the pixel the tester had routed and the current it served, one pixel at a time, every pixel visited. A never-ready INA226 and a stuck MCP23017 latch are handled. |
| `test_perovskite.py` | A full sweep on the real perovskite board: each cell pixel follows its IV line (Voc, Isc, and the slope, which checks the pixel voltage the flatsat logs), the other pixels read open, and dark cells only sink current. |
| `test_sband.py` | ID and scratchpad. EN/RST power sequencing. No stray registers. |
| `test_state_machine.py` | IDLE, FAKE_HEARTBEAT, IDLE, PEROVSKITE_CAPTURE timing. |
| `test_real_devices.py` | Real devices pass their checks. The magnetometer reads the Earth's field and the gyro reads still. The fuel gauges report a battery level. |
| `test_burn.py` | The burn pin stays low once burned. A short-wait build fires once and resumes its wait after a reset. |
| `test_uhf.py` | With a HackRF: UHF beacons are on the air. |

Tests for behaviour the firmware doesn't have yet are marked `xfail` with the TODO they wait on, so they start passing
when it lands:
- **Thermistor task:** no state enables the thermistor task.
- **S-band data path:** nothing calls `stxg2_store()` yet.

**Not covered:** low-battery behaviour. The fuel gauges and batteries are real, so the rig can't set the battery level.
