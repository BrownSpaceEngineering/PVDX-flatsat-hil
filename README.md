# PVDX flatsat hardware-in-the-loop rig

Tests the PVDX flatsat (the flight computer running [PVDXos](https://github.com/BrownSpaceEngineering/PVDXosV2)) with
a second board standing in for the hardware it lacks. Lives in PVDX as the `flatsat-hil/` submodule.

- `tester/`: firmware for a Metro M4 Grand Central that emulates the S-band radio, the perovskite measurement
  board and the battery fuel gauges and drives the photodiode and thermistor inputs and the real perovskite board's pixels. Atmel START project
  in `tester/tester.atzip`, generated into `tester/ASF/`.
- `hil/`: the pytest suite. It drives the tester over RTT through its J-Link, reads the flatsat's log through the
  flatsat's J-Link, and checks the flatsat's digital outputs, which the tester records.
- `.agents/skills/run-flatsat-hil/`: how to build, flash and run it (PVDX links it into its own skills).

Start with [hil/README.md](hil/README.md): setup, wiring and what each test covers.
