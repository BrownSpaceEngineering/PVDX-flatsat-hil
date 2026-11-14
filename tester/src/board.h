/**
 * board.h
 *
 * The tester's (Metro M4 Grand Central, ATSAMD51P20A) pins and clocks that its own code uses: the I2C targets' pins,
 * and the PWM and DAC outputs it connects and disconnects (analog_out.c). Everything else is in the Atmel START project
 * (ASF/atmel_start_pins.h). Pins are ASF PINMUX_ values, which carry both the pin and its peripheral function; the
 * Grand Central header label is in brackets. hil/README.md has the whole table from the flatsat's side.
 */

#ifndef BOARD_H
#define BOARD_H

#include <stdbool.h>
#include <stdint.h>

#include "atmel_start.h"

#define PINMUX_PIN(pinmux) ((pinmux) >> 16) // The ASF pin number in a PINMUX_ value

// Perovskite board I2C (flatsat I2C_SENSORS): MCP23017 at 0x20 and INA226 at 0x40 on SERCOM3
#define PMB_SERCOM SERCOM3
#define PMB_SDA PINMUX_PB20C_SERCOM3_PAD0 // [D20/SDA]
#define PMB_SCL PINMUX_PB21C_SERCOM3_PAD1 // [D21/SCL]

// S-band radio control I2C (flatsat I2C_SBAND): STXG2 at 0x26 on SERCOM6
#define STXG2_SERCOM SERCOM6
#define STXG2_SDA PINMUX_PC16C_SERCOM6_PAD0 // [D25/SDA1]
#define STXG2_SCL PINMUX_PC17C_SERCOM6_PAD1 // [D24/SCL1]

// S-band radio data SPI (flatsat SPI_SBAND) is ASF SBAND_SPI on SERCOM7: SCK PD09 [D52], SS PD10 [D53], data in
// PD11 [D50] (PAD3). Data out, PD08 [D51], isn't wired.

// The flatsat's digital outputs (magnetorquers, heaters, burn wire, S-band EN/RST) go to the logic analyzer, not here

/* ---------- CLOCKS (set up by ASF from the Atmel START project; clocks.c has the time base) ---------- */

#define CPU_HZ 120000000u
#define GCLK_CPU 0u  // DPLL0, 120 MHz: CPU, TCC/TC PWM
#define GCLK_48M 1u  // DFLL, 48 MHz (open loop): SERCOM, ADC
#define GCLK_1M 2u   // DFLL / 48: DPLL0 reference
#define GCLK_12M 3u  // DFLL / 4: DAC (12 MHz maximum)
#define GCLK_32K 4u  // OSCULP32K: SERCOM slow clock

void systick_init(void);
void gclk_connect(uint8_t peripheral_id, uint8_t generator);
uint32_t millis(void);
uint32_t micros(void);

#endif // BOARD_H
