# Flatsat hardware-in-the-loop tests

A second Metro M4 Grand Central (the **tester**, firmware in `tester/`) stands in for the boards the flatsat doesn't
have, and drives its analog inputs:

This is the PVDX-flatsat-hil repo, a submodule of PVDX mounted at `flatsat-hil/`. Paths starting `tester/` or `hil/`
are in this repo; `src/...` paths, `make dev` and the other `make` targets are PVDX's, run from the PVDX root. The suite
reads the flatsat's sources and `src/PVDXos.elf` from the PVDX checkout around it (or from `$PVDX_ROOT`).

- **Emulated devices:**
  - S-band radio (STXG2): control over I2C, data over SPI.
  - Perovskite measurement board: an MCP23017 and INA226 on I2C. The INA226 reads currents from a pixel IV model, at the
    voltage the flatsat's PVD_CRNT DAC sets.
- **Driven inputs:** 18 photodiode voltages and 4 thermistor voltages.

The flatsat's digital outputs go to a **logic analyzer** (sigrok, fx2lafw):
- magnetorquer H-bridges
- heaters
- burn wire
- S-band EN/RST

The real gyro, magnetometer, MRAM, camera, display, UHF radio and EPS (fuel gauges, batteries) stay connected. The
suite checks that they read plausibly.

The host suite (`pytest`) talks to the tester over SEGGER RTT through a second J-Link on the tester's SWD header
(`lib/tester.py`), and reads the PVDXos RTT log through the flatsat's J-Link (`lib/flatsat.py`). It asserts on both,
and on the logic analyzer captures (`lib/logic.py`).

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

Share a ground between the boards. Put about 1 kΩ in series with every analog output (all of the photodiode and
thermistor lines below). The tester keeps them high impedance until the suite arms them, and disconnects them when the
suite stops talking to it.

| Flatsat signal | Flatsat MCU pin | Tester pin | Tester function |
|---|---|---|---|
| PHD_0 | PC00 | D45 (PC10) | TCC0 PWM |
| PHD_1 | PA03 | D44 (PC11) | TCC0 PWM |
| PHD_2 | PB08 | D41 (PC12) | TCC0 PWM |
| PHD_3 | PB09 | D40 (PC13) | TCC0 PWM |
| PHD_4 | PA04 | D43 (PC14) | TCC0 PWM |
| PHD_5 | PA05 | D42 (PC15) | TCC0 PWM |
| PHD_6 | PA06 | D6 (PD20) | TCC1 PWM |
| PHD_7 | PA07 | D7 (PD21) | TCC1 PWM |
| PHD_8 | PA08 | D35 (PA18) | TCC1 PWM |
| PHD_9 | PA09 | D34 (PA19) | TCC1 PWM |
| PHD_10 | PA10 | D28 (PA14) | TCC2 PWM |
| PHD_11 | PA11 | D23 (PA15) | TCC2 PWM |
| PHD_12 | PB00 | D9 (PB02) | TCC2 PWM |
| PHD_13 | PB01 | D18 (PB12) | TCC3 PWM |
| PHD_14 | PB02 | D19 (PB13) | TCC3 PWM |
| PHD_15 | PB03 | D39 (PB14) | TCC4 PWM |
| PHD_16 | PC02 | D38 (PB15) | TCC4 PWM |
| PHD_17 | PC03 | D27 (PA13) | TC2 PWM |
| THERM_0 | PB04 | A0 (PA02) | DAC 0 |
| THERM_1 | PB05 | A1 (PA05) | DAC 1 |
| THERM_2 | PB06 | D15 (PB17) | TC6 PWM |
| THERM_3 | PB07 | D11 (PB23) | TC7 PWM |
| PVD_CRNT (perovskite DAC) | PA02 | A7 (PB04) | ADC1 input |
| I2C_SENSORS SDA (perovskite board) | PD09 | D20/SDA (PB20) | SERCOM3 I2C slave, 0x20 + 0x40 |
| I2C_SENSORS SCL | PD08 | D21/SCL (PB21) | |
| SBAND_SDA | PA17 | D25/SDA1 (PC16) | SERCOM6 I2C slave, 0x26 |
| SBAND_SCL | PA16 | D24/SCL1 (PC17) | |
| SBAND_MOSI | PB16 | D50 (PD11) | SERCOM7 SPI slave (data in, PAD3) |
| SBAND_SCK | PB17 | D52 (PD09) | |
| SBAND_CS | PB19 | D53 (PD10) | |

The pin choices are in the tester's Atmel START project (`tester/atmel_start_config.atstart`, generated into `tester/ASF/`), `tester/src/board.h` and `tester/src/analog_out.c`. Neither tester I2C slave adds pull-ups:
- **I2C_SENSORS:** has its pull-ups on the flight board.
- **SBAND I2C:** the flatsat enables internal pull-ups on its pins.

**Photodiode PWM:** the photodiodes get raw PWM (29 kHz, 12-bit). The flatsat averages only 4 instantaneous ADC samples
per reading. If its inputs aren't filtered, `test_each_channel_reads_its_own_voltage` fails with scattered errors of a
few hundred mV. The fix is a capacitor from each photodiode line to ground on the flatsat side of the series resistor:
1 kΩ with 10 µF, or 10 kΩ with 1 µF. No firmware change is needed.

## Wiring: logic analyzer to flatsat

`hil/logic_channels.json` maps each signal to an analyzer channel. Leave a signal empty when it isn't wired, and the
tests that need it skip. An 8-channel analyzer can't watch everything at once, so rewire for the tests you're running.

| Signal (`logic_channels.json`) | Flatsat MCU pin | Default channel |
|---|---|---|
| mtq_x_in1 / mtq_x_in2 | PA20 / PA21 | D0 / D1 |
| mtq_y_in1 / mtq_y_in2 | PA22 / PA23 | D2 / D3 |
| mtq_z_in1 / mtq_z_in2 | PD20 / PD21 | D4 / D5 |
| heater1 | PC23 | D6 |
| burn | PB29 | D7 |
| mtq_x_slp / mtq_y_slp / mtq_z_slp | PB30 / PB31 / PC27 | |
| heater2 | PC24 | |
| sband_en / sband_rst | PC25 / PC26 | |

Close PulseView before running: sigrok-cli can't open the analyzer while PulseView holds it.

## Running

```bash
make hil                                   # everything that can run; same as: cd flatsat-hil/hil && pytest
cd flatsat-hil/hil && pytest tests/test_boot.py -v     # one area
cd flatsat-hil/hil && pytest -m "not slow"             # skip the perovskite sweeps and burn tests (minutes each)
cd flatsat-hil/hil && pytest -m selftest               # tester alone, flatsat disconnected, with the self-test jumpers
cd flatsat-hil/hil && pytest -m manual -s              # tests that need someone to rotate the board
```

**Self-test jumpers** (flatsat disconnected; see `tester/src/selftest.c`):
- **I2C:** D37 to D20 and D25 (SDA), D36 to D21 and D24 (SCL).
- **SPI:** D1 to D50, D0 to D52, D12 to D53.
- **Analog:** D9 to A7 (a PWM output into the PVD_CRNT input).

Running the suite resets the flatsat many times through its J-Link, and never flashes either board. Only one session
can use a J-Link at a time, so stop any GDB server or RTT viewer on either probe first.

## What is covered

| Test file | Checks |
|---|---|
| `test_boot.py` | Emulated devices pass their startup checks. Each emulated fault (NACK, bad ID, held bus) fails the right check without stopping the boot. A dark, cold start boots. |
| `test_photodiodes.py` | Every channel reads its own voltage (catches swapped wires and `PHD_CFG` mistakes). ADCS's sun-vector estimate matches the simulated sun. Eclipse reads as not in sun. |
| `test_thermal.py` | The thermistor check reads the simulated temperatures. An open thermistor fails it. Heater hysteresis and partner fallback are tested on the heater lines. |
| `test_magnetorquers.py` | No shoot-through. 1 kHz PWM. Each burst's duty matches the duty ADCS logged. nSLEEP high while driving. Manual: B-dot reverses with rotation. |
| `test_perovskite.py` | A full sweep matches what the emulated board served, step by step: pixel routing, follower voltage and current. Faults are handled. |
| `test_sband.py` | ID and scratchpad. EN/RST power sequencing. No stray registers. |
| `test_state_machine.py` | IDLE, FAKE_HEARTBEAT, IDLE, PEROVSKITE_CAPTURE timing. |
| `test_real_devices.py` | Real devices pass their checks. The magnetometer reads the Earth's field and the gyro reads still. The fuel gauges report a battery level. |
| `test_burn.py` | The burn pin stays low once burned. A short-wait build fires once and resumes its wait after a reset. |
| `test_uhf.py` | With a HackRF: UHF beacons are on the air. |

Tests for behaviour the firmware doesn't have yet are marked `xfail` with the TODO they wait on, so they start passing
when it lands:
- **ADCS sunlight reporting:** ADCS doesn't report sunlight to the state machine yet.
- **Thermistor task:** no state enables the thermistor task.
- **S-band data path:** nothing calls `stxg2_store()` yet.

**Not covered:** low-battery behaviour. The fuel gauges and batteries are real, so the rig can't set the battery level.
