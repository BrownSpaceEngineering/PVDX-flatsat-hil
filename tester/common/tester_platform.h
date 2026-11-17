/**
 * tester_platform.h
 *
 * What the shared tester code (tester/common: device models and the host command parser) needs from the board it runs
 * on, implemented in tester/src for the Metro M4 Grand Central. Keeping the board-specific code behind it leaves the
 * models and parser testable on a host, and portable to another board.
 * The device models' I2C callbacks run in interrupts; everything else runs in the main loop.
 */

#ifndef TESTER_PLATFORM_H
#define TESTER_PLATFORM_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

const char *platform_name(void); // Reported by "ping", e.g. "pvdx-flatsat-tester-grand-central"
uint32_t platform_millis(void);
uint32_t platform_micros(void);         // Wraps; compare by subtraction
uint32_t platform_irq_save(void);       // Disables interrupts, returning the previous state
void platform_irq_restore(uint32_t state);

// Host link (RTT through a J-Link, see platform.c)
size_t platform_link_read(char *buf, size_t max);
void platform_link_write(const char *buf, size_t len);

// Holds SCL low at the next address match on that bus (a hung device), until called with false
void platform_hold_pmb_bus(bool hold);
void platform_hold_stxg2_bus(bool hold);

// Reboots into the board's bootloader; returns false if the board has none to enter this way
bool platform_enter_bootloader(void);

// Analog outputs, one per input the board drives (names phd0..phd3, pvd0..pvd3, therm0..therm3)
uint8_t analog_out_count(void);
const char *analog_out_name(uint8_t out);
int analog_out_find(const char *name); // -1 if this board doesn't drive it
void analog_out_set(uint8_t out, float volts);
float analog_out_get(uint8_t out);
void analog_out_arm(bool arm); // Connects, or disconnects (high impedance), every output
bool analog_out_armed(void);
void analog_out_set_full_scale(float volts); // Output voltage at 100%, measured by the host
float analog_out_full_scale(void);

// Digital inputs watching the flatsat's outputs (names mtq_{x,y,z}_in{1,2}, mtq_{x,y,z}_slp, heater1, heater2, burn,
// sband_en, sband_rst). While recording, each edge goes to the host as a binary record on its own channel (on the Grand
// Central, RTT channel 2; tester/src/watch.c has the format). Each writes the JSON members of its reply.
int watch_start(char *buf, size_t len); // t_us: the first record's time
int watch_stop(char *buf, size_t len);  // t_us, records (sent to the host in all), dropped
int watch_get(char *buf, size_t len);   // lines {name: line number in the records}, levels {name: 0|1}

#ifdef __cplusplus
}
#endif

#endif // TESTER_PLATFORM_H
