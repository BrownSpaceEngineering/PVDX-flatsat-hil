#ifndef I2C_SLAVE_H
#define I2C_SLAVE_H

#include <stdbool.h>
#include <stdint.h>

#include "board.h"

// Device model callbacks, called from the SERCOM interrupt
typedef struct {
    bool (*start)(void *ctx, uint8_t addr, bool read);   // Address matched; return false to NACK it
    void (*write)(void *ctx, uint8_t addr, uint8_t byte); // Byte from the master (always ACKed)
    uint8_t (*read)(void *ctx, uint8_t addr);            // Next byte for the master
    void (*end)(void *ctx, uint8_t addr, bool was_read); // Stop or repeated start
} i2c_slave_ops_t;

typedef struct {
    Sercom *sercom;
    const i2c_slave_ops_t *ops;
    void *ctx;
    volatile bool active;
    volatile bool read;
    volatile bool first_byte;
    volatile bool hold_bus;
    volatile uint8_t addr;
    volatile uint32_t transactions;
    volatile uint32_t nacks;
    volatile uint32_t bus_errors;
} i2c_slave_t;

// addr1 = 0 answers addr0 only. sda and scl are ASF PINMUX_ values for the SERCOM's PAD0 and PAD1.
void i2c_slave_init(i2c_slave_t *s, Sercom *sercom, uint32_t sda, uint32_t scl, uint8_t addr0, uint8_t addr1,
                    const i2c_slave_ops_t *ops, void *ctx);
// While held, the next address match stretches SCL indefinitely (a hung device)
void i2c_slave_hold_bus(i2c_slave_t *s, bool hold);

#endif // I2C_SLAVE_H
