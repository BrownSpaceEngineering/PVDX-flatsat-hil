/**
 * main.c
 *
 * Flatsat tester (Metro M4 Grand Central): emulates the boards missing from the flatsat (S-band radio, perovskite
 * measurement board) and drives its analog inputs (photodiodes, thermistors) for the host test suite in hil/. The
 * host talks to it over SEGGER RTT through a second J-Link. The device models and command parser (tester/common) run
 * from the bus interrupts, and the main loop serves host commands and keeps the perovskite board's inputs up to date. The analog outputs disconnect when the host has been silent for
 * HOST_TIMEOUT_MS (the host suite sends a keepalive), so a crashed or finished test run never leaves them driven.
 */

#include "analog_in.h"
#include "analog_out.h"
#include "atmel_start.h"
#include "board.h"
#include "buses.h"
#include "cmd.h"
#include "tester_platform.h"

#define HOST_TIMEOUT_MS 10000u

void platform_link_init(void);

int main(void) {
    atmel_start_init(); // Clocks, cache, and the peripherals in the Atmel START project
    systick_init();
    platform_link_init();
    analog_out_init();
    analog_in_init();
    buses_init();

    while (1) {
        cmd_poll();
        buses_poll();
        if (analog_out_armed() && millis() - cmd_last_command_ms() > HOST_TIMEOUT_MS) {
            analog_out_arm(false);
        }
        gpio_set_pin_level(LED, analog_out_armed());
    }
}
