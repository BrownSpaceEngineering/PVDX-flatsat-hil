/**
 * pmb_model.c
 *
 * The perovskite measurement board as one I2C target answering two addresses, the MCP23017 (0x20) and the INA226
 * (0x40), and its follower voltage. The board measures PVD_CRNT (the flatsat's DAC output) and passes it in;
 * on the real board it goes through R1 (21k) to the divider node and R2 (12k) to ground, and an op-amp follows the
 * node (PVDXdevref boards/adcs-pmb/schematics/perovskites.kicad_sch), so the follower sits at 12/33 of it: 0-1.2 V.
 */

#include "pmb_model.h"

#include "ina226_model.h"
#include "mcp23017_model.h"
#include "tester_platform.h"

#define DIVIDER_RATIO (12.0f / 33.0f)

static volatile float pvd_crnt_v = 0.0f;
static volatile uint32_t transactions = 0;
static volatile uint32_t nacks = 0;

bool pmb_start(uint8_t addr, bool read) {
    bool ack = false;
    if (addr == PMB_MCP23017_ADDR) {
        ack = mcp23017_start(read);
    } else if (addr == PMB_INA226_ADDR) {
        ack = ina226_start(read);
    }
    if (ack) {
        transactions++;
    } else {
        nacks++;
    }
    return ack;
}

void pmb_write(uint8_t addr, uint8_t byte) {
    if (addr == PMB_MCP23017_ADDR) {
        mcp23017_write(byte);
    } else {
        ina226_write(byte);
    }
}

uint8_t pmb_read(uint8_t addr) {
    return addr == PMB_MCP23017_ADDR ? mcp23017_read() : ina226_read();
}

void pmb_end(uint8_t addr, bool was_read) {
    if (addr == PMB_MCP23017_ADDR) {
        mcp23017_end();
        ina226_set_inputs(pvd_crnt_v * DIVIDER_RATIO, mcp23017_selected()); // Routing changes take effect at once
    } else {
        ina226_end(was_read);
    }
}

void pmb_set_pvd_crnt(float volts) {
    pvd_crnt_v = volts;
    ina226_set_inputs(volts * DIVIDER_RATIO, mcp23017_selected());
    ina226_poll();
}

float pmb_pvd_crnt_v(void) {
    return pvd_crnt_v;
}

uint32_t pmb_transactions(void) {
    return transactions;
}

uint32_t pmb_nacks(void) {
    return nacks;
}

void pmb_reset(void) {
    const uint32_t state = platform_irq_save();
    mcp23017_reset();
    ina226_reset();
    transactions = 0;
    nacks = 0;
    platform_irq_restore(state);
}
