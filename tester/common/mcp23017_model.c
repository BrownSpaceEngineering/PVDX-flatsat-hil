/**
 * mcp23017_model.c
 *
 * MCP23017 port expander on the perovskite board (datasheet DS20001952C). Register addressing follows IOCON.BANK, and
 * the address pointer advances after each byte unless IOCON.SEQOP is set (wrapping within the register file). Its 16
 * outputs drive the ADG734 switch inputs: output N high routes pixel N to the measurement node (src/drivers/pmb/adg734.c),
 * which is what mcp23017_selected() reports to the INA226 model.
 */

#include "mcp23017_model.h"

#include <stdio.h>
#include <string.h>

#define REG_COUNT 0x16u
#define IODIRA 0x00u
#define IPOLA 0x02u
#define IOCON 0x0Au
#define IOCON_ALT 0x0Bu
#define GPPUA 0x0Cu
#define INTFA 0x0Eu
#define INTCAPA 0x10u
#define GPIOA 0x12u
#define OLATA 0x14u
#define IOCON_BANK (1u << 7)
#define IOCON_SEQOP (1u << 5)

static struct {
    mcp23017_fault_t fault;
    uint8_t regs[REG_COUNT]; // In BANK = 0 order: A and B registers interleaved
    uint8_t pointer;         // Index into regs
    bool pointer_set;        // The first byte of a write is the register address
    bool routing_changed;    // OLAT or IODIR written in this transaction
    // Statistics
    uint32_t writes;
    uint32_t multi_select; // Writes that left more than one pixel routed to measurement
    uint32_t selections;   // Writes that routed exactly one pixel (a new selection)
    uint16_t visited;      // Pixels that have been selected at least once
    uint16_t last_selected;
} mcp;

// Register index (BANK = 0 order) for a bus address under the current IOCON.BANK
static uint8_t index_for(uint8_t address) {
    if (!(mcp.regs[IOCON] & IOCON_BANK)) {
        return address < REG_COUNT ? address : 0xFF;
    }
    if (address <= 0x0A) {
        return (uint8_t)(address * 2u);
    }
    if (address >= 0x10 && address <= 0x1A) {
        return (uint8_t)((address - 0x10u) * 2u + 1u);
    }
    return 0xFF;
}

static void advance(void) {
    if (mcp.regs[IOCON] & IOCON_SEQOP) {
        return;
    }
    mcp.pointer = (uint8_t)((mcp.pointer + 1u) % REG_COUNT);
}

static uint16_t reg16(uint8_t index_a) {
    return (uint16_t)(mcp.regs[index_a] | (mcp.regs[index_a + 1u] << 8));
}

static uint8_t pin_levels(uint8_t port) {
    const uint8_t iodir = mcp.regs[IODIRA + port];
    const uint8_t outputs = mcp.regs[OLATA + port] & (uint8_t)~iodir;
    const uint8_t inputs = (mcp.regs[GPPUA + port] & iodir) ^ (mcp.regs[IPOLA + port] & iodir);
    return outputs | inputs;
}

static void note_selection(void) {
    const uint16_t selected = mcp23017_selected();
    uint8_t count = 0;
    for (uint16_t s = selected; s; s &= (uint16_t)(s - 1u)) {
        count++;
    }
    if (count > 1) {
        mcp.multi_select++;
    } else if (count == 1 && selected != mcp.last_selected) {
        mcp.selections++;
        mcp.visited |= selected;
    }
    mcp.last_selected = selected;
}

static void write_register(uint8_t index, uint8_t value) {
    mcp.writes++;
    switch (index) {
        case IOCON:
        case IOCON_ALT:
            mcp.regs[IOCON] = mcp.regs[IOCON_ALT] = value;
            break;
        case INTFA:
        case INTFA + 1:
        case INTCAPA:
        case INTCAPA + 1:
            break; // Read-only
        case GPIOA:
        case GPIOA + 1:
            index = (uint8_t)(index + (OLATA - GPIOA)); // Writing GPIO writes the latch
            // fall through
        default:
            if (mcp.fault == MCP23017_FAULT_STUCK && (index == OLATA || index == OLATA + 1)) {
                break; // Latch ignores writes
            }
            mcp.regs[index] = value;
            break;
    }
    if (index == OLATA || index == OLATA + 1 || index == IODIRA || index == IODIRA + 1) {
        mcp.routing_changed = true;
    }
}

static uint8_t read_register(uint8_t index) {
    if (index == GPIOA || index == GPIOA + 1) {
        return pin_levels((uint8_t)(index - GPIOA));
    }
    return mcp.regs[index];
}

/* ---------- I2C CALLBACKS (from pmb_model.c) ---------- */

bool mcp23017_start(bool read) {
    if (mcp.fault == MCP23017_FAULT_NACK) {
        return false;
    }
    mcp.pointer_set = read; // A read continues from the current pointer
    return true;
}

void mcp23017_write(uint8_t byte) {
    if (!mcp.pointer_set) {
        const uint8_t index = index_for(byte);
        mcp.pointer = index == 0xFF ? 0 : index;
        mcp.pointer_set = true;
        return;
    }
    write_register(mcp.pointer, byte);
    advance();
}

// Judged once per transaction, so a 16-bit latch write is never seen half done
void mcp23017_end(void) {
    if (mcp.routing_changed) {
        mcp.routing_changed = false;
        note_selection();
    }
}

uint8_t mcp23017_read(void) {
    const uint8_t value = read_register(mcp.pointer);
    advance();
    return value;
}

/* ---------- PUBLIC ---------- */

void mcp23017_reset(void) {
    const mcp23017_fault_t fault = mcp.fault;
    memset(&mcp, 0, sizeof(mcp));
    mcp.fault = fault;
    mcp.regs[IODIRA] = mcp.regs[IODIRA + 1] = 0xFF; // Power-on: all inputs
}

void mcp23017_set_fault(mcp23017_fault_t fault) {
    mcp.fault = fault;
}

uint16_t mcp23017_selected(void) {
    const uint16_t outputs = (uint16_t)~reg16(IODIRA);
    return reg16(OLATA) & outputs;
}

int mcp23017_json(char *buf, size_t len) {
    return snprintf(buf, len,
                    "\"fault\":%d,\"iodir\":%u,\"olat\":%u,\"iocon\":%u,\"selected_mask\":%u,\"writes\":%lu,\"selections\":%lu,"
                    "\"multi_select\":%lu,\"visited_mask\":%u",
                    (int)mcp.fault, reg16(IODIRA), reg16(OLATA), mcp.regs[IOCON], mcp23017_selected(), (unsigned long)mcp.writes,
                    (unsigned long)mcp.selections, (unsigned long)mcp.multi_select, mcp.visited);
}
