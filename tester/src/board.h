/**
 * board.h
 *
 * The tester's (Metro M4 Grand Central, ATSAMD51P20A) pins and clocks that its own code uses: the I2C targets' pins,
 * and the PWM outputs it connects and disconnects (analog_out.c). Everything else is in the Atmel START project
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

// Fuel gauges (the flatsat's EPS, behind its TCA9546A mux): a MAX17205 target answering 0x36 and 0x0B on each mux
// channel's bus (SDn/SCn on the flatsat's EPS header)
#define FG0_SERCOM SERCOM1
#define FG0_SDA PINMUX_PA16C_SERCOM1_PAD0 // [D37]
#define FG0_SCL PINMUX_PA17C_SERCOM1_PAD1 // [D36]
#define FG1_SERCOM SERCOM5
#define FG1_SDA PINMUX_PA23D_SERCOM5_PAD0 // [D30]
#define FG1_SCL PINMUX_PA22D_SERCOM5_PAD1 // [D31]
#define FG2_SERCOM SERCOM4
#define FG2_SDA PINMUX_PB12C_SERCOM4_PAD0 // [D18]
#define FG2_SCL PINMUX_PB13C_SERCOM4_PAD1 // [D19]
#define FG3_SERCOM SERCOM0
#define FG3_SDA PINMUX_PB24C_SERCOM0_PAD0 // [D1]
#define FG3_SCL PINMUX_PB25C_SERCOM0_PAD1 // [D0]

// S-band radio data SPI (flatsat SPI_SBAND) is ASF SBAND_SPI on SERCOM7: SCK PD09 [D52], SS PD10 [D53], data in
// PD11 [D50] (PAD3). Data out, PD08 [D51], isn't wired.

// The flatsat's digital outputs (magnetorquers, heaters, burn wire, S-band EN/RST) are recorded by watch.c

/* ---------- CLOCKS (set up by ASF from the Atmel START project; clocks.c has the time base) ---------- */

#define CPU_HZ 120000000u
#define GCLK_CPU 0u  // DPLL0, 120 MHz: CPU, TCC/TC PWM
#define GCLK_48M 1u  // DFLL, 48 MHz (open loop): SERCOM, ADC
#define GCLK_1M 2u   // DFLL / 48: DPLL0 reference
#define GCLK_12M 3u  // DFLL / 4: unused (it clocked the DAC)
#define GCLK_32K 4u  // OSCULP32K: SERCOM slow clock

void systick_init(void);
void gclk_connect(uint8_t peripheral_id, uint8_t generator);
uint32_t millis(void);
uint32_t micros(void);

#endif // BOARD_H
