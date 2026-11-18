#ifndef STXG2_MODEL_H
#define STXG2_MODEL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define STXG2_ADDR 0x26u

typedef enum {
    STXG2_FAULT_NONE = 0,
    STXG2_FAULT_NACK,     // Never answers its address (radio off or dead)
    STXG2_FAULT_BAD_ID,   // ID register reads 0xDEADBEEF
    STXG2_FAULT_HOLD_BUS, // Holds SCL low on the next address match (a hung device)
    STXG2_FAULT_MODE_NACK, // NACKs its address while switching modes, instead of answering with MODE.Busy set
} stxg2_fault_t;

// I2C target callbacks, called from the bus interrupt
bool stxg2_start(bool read); // false = NACK (off or in reset, or a fault)
void stxg2_write(uint8_t byte);
uint8_t stxg2_read(void);
void stxg2_end(bool was_read); // Stop or repeated start

// SPI data input (boards without an SPI slave never call these)
void stxg2_spi_byte(uint8_t byte);
void stxg2_spi_end(void); // CS went high
void stxg2_spi_overflow(void);

void stxg2_init(void);
void stxg2_poll(void); // Follows SBAND_EN and SBAND_RST; call from the main loop
void stxg2_set_fault(stxg2_fault_t fault);
void stxg2_reset(void);                // Back to power-on registers, statistics cleared
int stxg2_json(char *buf, size_t len); // State as JSON object members (no braces)

#ifdef __cplusplus
}
#endif

#endif // STXG2_MODEL_H
