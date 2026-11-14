#ifndef PMB_MODEL_H
#define PMB_MODEL_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PMB_MCP23017_ADDR 0x20u
#define PMB_INA226_ADDR 0x40u

// I2C target callbacks for both addresses, called from the bus interrupt
bool pmb_start(uint8_t addr, bool read); // false = NACK
void pmb_write(uint8_t addr, uint8_t byte);
uint8_t pmb_read(uint8_t addr);
void pmb_end(uint8_t addr, bool was_read); // Stop or repeated start

void pmb_set_pvd_crnt(float volts); // Latest PVD_CRNT measurement; also runs due INA226 conversions. Call often.
float pmb_pvd_crnt_v(void);
uint32_t pmb_transactions(void);
uint32_t pmb_nacks(void);
void pmb_reset(void); // Both devices to power-on, statistics cleared (faults kept)

#ifdef __cplusplus
}
#endif

#endif // PMB_MODEL_H
