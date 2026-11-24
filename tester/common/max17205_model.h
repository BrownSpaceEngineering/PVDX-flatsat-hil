#ifndef MAX17205_MODEL_H
#define MAX17205_MODEL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// The flatsat's four battery fuel gauges, one per channel of its TCA9546A mux. The mux stays on the flatsat; each pack
// here is an I2C target on that channel's bus, answering both of the gauge's addresses.
#define MAX17205_PACKS 4u
#define MAX17205_ADDR_LOW 0x36u  // Registers 0x000-0x0FF
#define MAX17205_ADDR_HIGH 0x0Bu // Registers 0x100-0x1FF (the low byte is sent)
#define MAX17205_REG_COUNT 0x200u

typedef enum {
    MAX17205_FAULT_NONE = 0,
    MAX17205_FAULT_NACK,     // Never answers its addresses (gauge dead or unpowered)
    MAX17205_FAULT_HOLD_BUS, // Holds SCL low on the next address match, which through the mux hangs the flatsat's bus
} max17205_fault_t;

// I2C target callbacks, called from that pack's bus interrupt
bool max17205_start(uint8_t pack, uint8_t addr, bool read); // false = NACK
void max17205_write(uint8_t pack, uint8_t byte);
uint8_t max17205_read(uint8_t pack);
void max17205_end(uint8_t pack); // Stop or repeated start

void max17205_reset(void); // Every pack to power-on registers, statistics cleared (faults kept)
bool max17205_set_reg(uint8_t pack, uint16_t reg, uint16_t value); // false if pack or reg is out of range
void max17205_set_fault(uint8_t pack, max17205_fault_t fault);
int max17205_json(char *buf, size_t len); // State as JSON object members (no braces)

#ifdef __cplusplus
}
#endif

#endif // MAX17205_MODEL_H
