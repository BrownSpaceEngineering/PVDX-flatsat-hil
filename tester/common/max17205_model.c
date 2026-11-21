/**
 * max17205_model.c
 *
 * MAX17205 fuel gauges (datasheet MAX17201-MAX17215, PVDXdevref markdown/fuel-gauge), one per battery pack. Each is a
 * file of 16-bit registers: 0x36 addresses 0x000-0x0FF and 0x0B addresses 0x100-0x1FF, the first byte of a write
 * sets the address within that page, and words go LSB first, the address advancing after each word. The host sets the
 * registers the flatsat reads (fg set), already encoded: the conversions depend on the flatsat's sense resistor, which
 * hil/lib/models.py reads from the flatsat's sources. Writes are stored and counted: the flatsat's driver should never
 * write, since the nonvolatile registers only take a few writes in the part's life.
 */

#include "max17205_model.h"

#include <string.h>

#include "cmd.h"
#include "tester_platform.h"

#define REG_STATUS 0x000u
#define REG_REPSOC 0x006u
#define REG_TEMP 0x008u
#define REG_VCELL 0x009u
#define REG_CURRENT 0x00Au
#define REG_DESIGNCAP 0x018u
#define REG_DEVNAME 0x021u
#define REG_PACKCFG 0x0BDu
#define REG_CELL2 0x0D7u
#define REG_CELL1 0x0D8u
#define REG_BATT 0x0DAu
#define REG_NDESIGNCAP 0x1B3u
#define REG_NPACKCFG 0x1B5u

#define STATUS_POR 0x0002u
#define DEVNAME_MAX17205 0x4005u // Device 5h (MAX17205/15), revision 4
#define PACKCFG_2S 0x0002u       // NCELLS = 2
#define DESIGNCAP_3000MAH 0x1770u
#define CELL_3V7 0xB900u      // 3.7 V at 78.125 uV/LSB
#define BATT_7V4 0x1720u      // 7.4 V at 1.25 mV/LSB
#define REPSOC_50_PCT 0x3200u // 1/256 % per LSB
#define TEMP_20_C 0x1400u     // 1/256 degC per LSB

typedef struct {
    max17205_fault_t fault;
    uint16_t regs[MAX17205_REG_COUNT];
    uint16_t pointer;  // Register the next word goes to or comes from
    bool pointer_set;  // The first byte of a write is the register address
    bool high_byte;    // The next byte is the current word's MSB
    uint8_t low_byte;  // LSB of a word being written
    // Statistics
    uint32_t transactions;
    uint32_t nacks;
    uint32_t words_read;
    uint32_t words_written;
} gauge_t;

static gauge_t gauges[MAX17205_PACKS];

static void power_on(gauge_t *g) {
    const max17205_fault_t fault = g->fault;
    memset(g, 0, sizeof(*g));
    g->fault = fault;
    g->regs[REG_STATUS] = STATUS_POR;
    g->regs[REG_REPSOC] = REPSOC_50_PCT;
    g->regs[REG_TEMP] = TEMP_20_C;
    g->regs[REG_VCELL] = CELL_3V7;
    g->regs[REG_CELL1] = CELL_3V7;
    g->regs[REG_CELL2] = CELL_3V7;
    g->regs[REG_BATT] = BATT_7V4;
    g->regs[REG_DESIGNCAP] = DESIGNCAP_3000MAH;
    g->regs[REG_NDESIGNCAP] = DESIGNCAP_3000MAH;
    g->regs[REG_DEVNAME] = DEVNAME_MAX17205;
    g->regs[REG_PACKCFG] = PACKCFG_2S;
    g->regs[REG_NPACKCFG] = PACKCFG_2S;
}

static void advance(gauge_t *g) {
    g->pointer = (uint16_t)((g->pointer + 1u) % MAX17205_REG_COUNT);
}

/* ---------- I2C CALLBACKS (from the bus interrupts) ---------- */

bool max17205_start(uint8_t pack, uint8_t addr, bool read) {
    gauge_t *const g = &gauges[pack];
    if (g->fault == MAX17205_FAULT_NACK) {
        g->nacks++;
        return false;
    }
    g->transactions++;
    g->high_byte = false;
    g->pointer_set = read; // A read continues from the current pointer
    if (!read) {
        g->pointer = addr == MAX17205_ADDR_HIGH ? 0x100u : 0x000u; // The page; the first byte gives the rest
    }
    return true;
}

void max17205_write(uint8_t pack, uint8_t byte) {
    gauge_t *const g = &gauges[pack];
    if (!g->pointer_set) {
        g->pointer = (uint16_t)(g->pointer | byte);
        g->pointer_set = true;
        return;
    }
    if (!g->high_byte) {
        g->low_byte = byte;
        g->high_byte = true;
        return;
    }
    g->regs[g->pointer] = (uint16_t)(g->low_byte | (byte << 8));
    g->words_written++;
    g->high_byte = false;
    advance(g);
}

uint8_t max17205_read(uint8_t pack) {
    gauge_t *const g = &gauges[pack];
    const uint16_t word = g->regs[g->pointer];
    if (!g->high_byte) {
        g->high_byte = true;
        return (uint8_t)word;
    }
    g->high_byte = false;
    g->words_read++;
    advance(g);
    return (uint8_t)(word >> 8);
}

void max17205_end(uint8_t pack) {
    gauges[pack].high_byte = false; // A half-written word is dropped
}

/* ---------- PUBLIC ---------- */

void max17205_reset(void) {
    const uint32_t state = platform_irq_save();
    for (uint8_t p = 0; p < MAX17205_PACKS; p++) {
        power_on(&gauges[p]);
    }
    platform_irq_restore(state);
}

bool max17205_set_reg(uint8_t pack, uint16_t reg, uint16_t value) {
    if (pack >= MAX17205_PACKS || reg >= MAX17205_REG_COUNT) {
        return false;
    }
    gauges[pack].regs[reg] = value; // One aligned halfword store, so the interrupt never sees half of it
    return true;
}

void max17205_set_fault(uint8_t pack, max17205_fault_t fault) {
    if (pack >= MAX17205_PACKS) {
        return;
    }
    gauges[pack].fault = fault;
    platform_hold_fg_bus(pack, fault == MAX17205_FAULT_HOLD_BUS);
}

int max17205_json(char *buf, size_t len) {
    int n = cmd_append(buf, len, 0, "\"packs\":[");
    for (uint8_t p = 0; p < MAX17205_PACKS; p++) {
        const uint32_t state = platform_irq_save();
        const gauge_t *const g = &gauges[p];
        const uint32_t counts[4] = {g->transactions, g->nacks, g->words_read, g->words_written};
        const uint16_t regs[5] = {g->regs[REG_STATUS], g->regs[REG_REPSOC], g->regs[REG_BATT], g->regs[REG_CURRENT],
                                  g->regs[REG_TEMP]};
        const max17205_fault_t fault = g->fault;
        platform_irq_restore(state);
        n = cmd_append(buf, len, n,
                       "%s{\"fault\":%d,\"transactions\":%lu,\"nacks\":%lu,\"words_read\":%lu,\"words_written\":%lu,"
                       "\"status\":%u,\"repsoc\":%u,\"batt\":%u,\"current\":%u,\"temp\":%u}",
                       p ? "," : "", (int)fault, (unsigned long)counts[0], (unsigned long)counts[1], (unsigned long)counts[2],
                       (unsigned long)counts[3], regs[0], regs[1], regs[2], regs[3], regs[4]);
    }
    return cmd_append(buf, len, n, "]");
}
