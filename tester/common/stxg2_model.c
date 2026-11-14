/**
 * stxg2_model.c
 *
 * CUBECOM STXG2 S-band transmitter, as far as src/drivers/stxg2/stxg2.c uses it:
 * - I2C at 0x26. A write is [addr LSB, addr MSB, value LSB first (4 bytes)]; a read is [addr LSB, addr MSB], repeated
 *   start, then 4 value bytes LSB first.
 * - I2C_STATUS flags (invalid address, bad size, read-only) clear when read.
 * - A MODE write makes the radio deaf to I2C (address NACK) for MODE_SWITCH_MS, as the real one is during a switch.
 * - SPI data counts towards SPI_RX_CNT (words since SPI_CTRL was enabled), and towards the memory write session while
 *   the crossbar routes SPI to memory write. The SPI buffer drains instantly, so SPI_STATUS never reports it full.
 * Register addresses and fields are copied from src/drivers/stxg2/stxg2.h. Each tester connects the I2C and SPI
 * callbacks below to its buses.
 */

#include "stxg2_model.h"

#include <stdio.h>
#include <string.h>

#include "crc32.h"
#include "tester_platform.h"

#define MODE_SWITCH_MS 30u
#define MEMORY_SIZE_PACKETS 4096u
#define PACKET_DATA_BYTES 16380u

#define REG_ID 0x0001
#define REG_SERIAL 0x0002
#define REG_SW_VERSION 0x0003
#define REG_FW_VERSION 0x0004
#define REG_HW_VERSION 0x0005
#define REG_UPTIME 0x0006
#define REG_SCRATCHPAD 0x0007
#define REG_I2C_STATUS 0x0010
#define REG_SPI_CTRL 0x002C
#define REG_SPI_STATUS 0x002D
#define REG_SPI_RX_CNT 0x002E
#define REG_BOARD_STATUS 0x0030
#define REG_RF_STATUS 0x0031
#define REG_MODE 0x0060
#define REG_MEMORY_STATUS 0x0080
#define REG_MEMORY_SIZE 0x0081
#define REG_MEM_WR_CTRL 0x0090
#define REG_MEM_WR_STAT 0x0094
#define REG_MEM_WR_CNT_0 0x0095
#define REG_MEM_WR_CNT_1 0x0096
#define REG_MEM_WR_PKT_CNT 0x0097
#define REG_MEM_WR_ERR 0x0098
#define REG_MEM_RD_CTRL 0x00A0
#define REG_MEM_RD_STAT 0x00A3
#define REG_MEM_RD_CNT 0x00A4
#define REG_MEM_RD_ERR 0x00A5
#define REG_MEM_RT_CTRL 0x00B0
#define REG_MEM_RT_STAT 0x00B2
#define REG_CROSSBAR 0x0120

#define STXG2_ID_VALUE 0x53545832u // "STX2"
#define MODE_BUSY (1u << 8)
#define I2C_STATUS_INVALID_ADDR (1u << 0)
#define I2C_STATUS_BAD_SIZE (1u << 1)
#define I2C_STATUS_NOT_WRITEABLE (1u << 2)
#define SPI_CTRL_ENABLE (1u << 0)
#define SPI_STATUS_READY (1u << 2)
#define SPI_STATUS_RX_RECVD (1u << 3)
#define RF_STATUS_LOCKED 0x3u
#define MEMORY_STATE_OFF 0u
#define MEMORY_STATE_IDLE 1u
#define MEMORY_STATE_WRITE_BUSY 2u
#define MEMORY_STATE_READ_BUSY 3u
#define MEM_CTRL_START (1u << 0)
#define MEM_CTRL_STOP (1u << 1)
#define MEM_CTRL_ABORT (1u << 2)
#define MEM_STAT_ACTIVE (1u << 0)
#define MEM_STAT_COMPLETE (1u << 1)
#define XBAR_IN_SPI 0x4u
#define XBAR_OUT_MEMWR_SHIFT 8u
#define MODE_STANDBY 0u

// Registers that just hold what was written, with their reset values
typedef struct {
    uint16_t addr;
    uint32_t value;
} plain_reg_t;

static const plain_reg_t PLAIN_RESET[] = {
    {REG_SCRATCHPAD, 0},   {0x0011, 0},   {0x0013, 0},       {0x0015, 0},   {0x001F, 0},      {0x0061, 5000},
    {0x0062, 2450000},     {0x0065, 0},   {0x0066, 1},       {0x0067, 0},   {0x0070, 0},      {0x0071, 0},
    {0x0072, 0},           {0x0073, 0},   {0x0091, 0},       {0x0092, 0},   {0x0093, 0},      {0x00A1, 0},
    {0x00A2, 0},           {0x00B1, 0},   {0x0110, 1},       {0x0112, 0},   {REG_CROSSBAR, 0}, {0x0121, 0},
    {0x0123, 0},           {0x0124, 0},   {0x0192, 27},      {0x0193, 0},   {0x0197, 0},      {0x0198, 0},
};
#define PLAIN_COUNT (sizeof(PLAIN_RESET) / sizeof(PLAIN_RESET[0]))

// Read-only registers with fixed values
static const plain_reg_t FIXED[] = {
    {REG_ID, STXG2_ID_VALUE}, {REG_SERIAL, 0x00005EED}, {REG_SW_VERSION, 0x00010000}, {REG_FW_VERSION, 0x00010000},
    {REG_HW_VERSION, 0x00000002}, {REG_BOARD_STATUS, 0}, {0x0032, 25}, {0x0033, 25}, {0x0034, 25}, {0x0035, 25},
    {0x003E, 0}, {0x003F, 0}, {0x0068, 0}, {0x0083, 0}, {0x0084, 0}, {REG_MEM_WR_ERR, 0}, {REG_MEM_RD_ERR, 0},
    {0x00B4, 0}, {0x00B5, 0}, {0x0111, 0}, {0x0113, 0}, {0x0114, 0}, {0x0115, 0}, {0x0122, 0}, {0x0125, 0},
    {0x0126, 0}, {0x0127, 0}, {0x0128, 0}, {0x0129, 0}, {0x0190, 0}, {0x0191, 0},
};
#define FIXED_COUNT (sizeof(FIXED) / sizeof(FIXED[0]))

static struct {
    stxg2_fault_t fault;

    // I2C transaction in progress
    uint8_t wbuf[6];
    uint8_t wlen;
    bool overflowed;
    uint16_t pointer;
    uint32_t read_value;
    uint8_t read_index;

    // Registers
    uint32_t plain[PLAIN_COUNT];
    uint32_t i2c_status;
    uint32_t mode;
    uint32_t mode_busy_until;
    uint32_t spi_ctrl;
    uint32_t mem_wr_stat, mem_rd_stat, mem_rt_stat;
    uint32_t mem_wr_bytes, mem_rd_count;
    bool mem_wr_active, mem_rd_active;

    // Statistics for the host
    uint32_t transactions, nacks, spi_overflows;
    uint32_t reg_reads, reg_writes, invalid;
    uint32_t spi_bytes, spi_rx_bytes_since_enable, spi_transactions, spi_crc;
    struct {
        uint16_t addr;
        uint32_t value;
    } recent[8];
    uint32_t recent_count;
} emu;

static uint32_t *plain_reg(uint16_t addr) {
    for (size_t i = 0; i < PLAIN_COUNT; i++) {
        if (PLAIN_RESET[i].addr == addr) {
            return &emu.plain[i];
        }
    }
    return NULL;
}

static bool mode_busy(void) {
    return (int32_t)(platform_millis() - emu.mode_busy_until) < 0;
}

static uint32_t memory_state(void) {
    if (emu.mode == MODE_STANDBY) {
        return MEMORY_STATE_OFF;
    }
    if (emu.mem_wr_active) {
        return MEMORY_STATE_WRITE_BUSY;
    }
    return emu.mem_rd_active ? MEMORY_STATE_READ_BUSY : MEMORY_STATE_IDLE;
}

static uint32_t read_reg(uint16_t addr) {
    emu.reg_reads++;
    switch (addr) {
        case REG_ID:
            return emu.fault == STXG2_FAULT_BAD_ID ? 0xDEADBEEFu : STXG2_ID_VALUE;
        case REG_UPTIME:
            return platform_millis();
        case REG_I2C_STATUS: {
            const uint32_t value = emu.i2c_status;
            emu.i2c_status = 0;
            return value;
        }
        case REG_MODE:
            return emu.mode | (mode_busy() ? MODE_BUSY : 0);
        case REG_SPI_CTRL:
            return emu.spi_ctrl;
        case REG_SPI_STATUS:
            return ((emu.spi_ctrl & SPI_CTRL_ENABLE) ? SPI_STATUS_READY : 0) | (emu.spi_rx_bytes_since_enable ? SPI_STATUS_RX_RECVD : 0);
        case REG_SPI_RX_CNT:
            return emu.spi_rx_bytes_since_enable / 4u;
        case REG_RF_STATUS:
            return emu.mode == MODE_STANDBY ? 0 : RF_STATUS_LOCKED;
        case REG_MEMORY_STATUS:
            return memory_state();
        case REG_MEMORY_SIZE:
            return MEMORY_SIZE_PACKETS;
        case REG_MEM_WR_CTRL:
        case REG_MEM_RD_CTRL:
        case REG_MEM_RT_CTRL:
            return 0;
        case REG_MEM_WR_STAT:
            return emu.mem_wr_stat;
        case REG_MEM_WR_CNT_0:
            return emu.mem_wr_bytes & 0xFFFFu;
        case REG_MEM_WR_CNT_1:
            return emu.mem_wr_bytes >> 16;
        case REG_MEM_WR_PKT_CNT:
            return (emu.mem_wr_bytes + PACKET_DATA_BYTES - 1u) / PACKET_DATA_BYTES;
        case REG_MEM_RD_STAT:
            return emu.mem_rd_stat;
        case REG_MEM_RD_CNT:
            return emu.mem_rd_count;
        case REG_MEM_RT_STAT:
            return emu.mem_rt_stat;
        default:
            break;
    }
    const uint32_t *const plain = plain_reg(addr);
    if (plain != NULL) {
        return *plain;
    }
    for (size_t i = 0; i < FIXED_COUNT; i++) {
        if (FIXED[i].addr == addr) {
            return FIXED[i].value;
        }
    }
    emu.i2c_status |= I2C_STATUS_INVALID_ADDR;
    emu.invalid++;
    return 0;
}

static void write_reg(uint16_t addr, uint32_t value) {
    emu.reg_writes++;
    emu.recent[emu.recent_count % 8].addr = addr;
    emu.recent[emu.recent_count % 8].value = value;
    emu.recent_count++;

    switch (addr) {
        case REG_MODE:
            emu.mode = value & 0xFFu;
            emu.mode_busy_until = platform_millis() + MODE_SWITCH_MS;
            return;
        case REG_SPI_CTRL:
            if ((value & SPI_CTRL_ENABLE) && !(emu.spi_ctrl & SPI_CTRL_ENABLE)) {
                emu.spi_rx_bytes_since_enable = 0;
            }
            emu.spi_ctrl = value;
            return;
        case REG_MEM_WR_CTRL:
            if (value & (MEM_CTRL_ABORT | MEM_CTRL_STOP)) {
                emu.mem_wr_stat = (value & MEM_CTRL_STOP) && emu.mem_wr_active ? MEM_STAT_COMPLETE : 0;
                emu.mem_wr_active = false;
            } else if (value & MEM_CTRL_START) {
                emu.mem_wr_active = true;
                emu.mem_wr_bytes = 0;
                emu.mem_wr_stat = MEM_STAT_ACTIVE;
            }
            return;
        case REG_MEM_RD_CTRL:
            if (value & (MEM_CTRL_ABORT | MEM_CTRL_STOP)) {
                emu.mem_rd_stat = (value & MEM_CTRL_STOP) && emu.mem_rd_active ? MEM_STAT_COMPLETE : 0;
                emu.mem_rd_active = false;
            } else if (value & MEM_CTRL_START) {
                emu.mem_rd_active = true;
                emu.mem_rd_count = 0;
                emu.mem_rd_stat = MEM_STAT_ACTIVE;
            }
            return;
        case REG_MEM_RT_CTRL:
            emu.mem_rt_stat = (value & MEM_CTRL_START) ? MEM_STAT_ACTIVE : 0;
            return;
        default:
            break;
    }
    uint32_t *const plain = plain_reg(addr);
    if (plain != NULL) {
        *plain = value;
        return;
    }
    bool read_only = addr == REG_ID || addr == REG_UPTIME || addr == REG_I2C_STATUS || addr == REG_SPI_STATUS ||
                     addr == REG_SPI_RX_CNT || addr == REG_RF_STATUS || addr == REG_MEMORY_STATUS || addr == REG_MEMORY_SIZE ||
                     addr == REG_MEM_WR_STAT || addr == REG_MEM_WR_CNT_0 || addr == REG_MEM_WR_CNT_1 ||
                     addr == REG_MEM_WR_PKT_CNT || addr == REG_MEM_RD_STAT || addr == REG_MEM_RD_CNT || addr == REG_MEM_RT_STAT;
    for (size_t i = 0; i < FIXED_COUNT && !read_only; i++) {
        read_only = FIXED[i].addr == addr;
    }
    emu.i2c_status |= read_only ? I2C_STATUS_NOT_WRITEABLE : I2C_STATUS_INVALID_ADDR;
    if (read_only) {
        return;
    }
    emu.invalid++;
}

/* ---------- I2C ---------- */

bool stxg2_start(bool read) {
    if (emu.fault == STXG2_FAULT_NACK || mode_busy()) {
        emu.nacks++;
        return false;
    }
    emu.transactions++;
    if (read) {
        emu.read_value = read_reg(emu.pointer);
        emu.read_index = 0;
    } else {
        emu.wlen = 0;
        emu.overflowed = false;
    }
    return true;
}

void stxg2_write(uint8_t byte) {
    if (emu.wlen < sizeof(emu.wbuf)) {
        emu.wbuf[emu.wlen++] = byte;
    } else {
        emu.overflowed = true;
    }
}

uint8_t stxg2_read(void) {
    const uint8_t byte = emu.read_index < 4 ? (uint8_t)(emu.read_value >> (8 * emu.read_index)) : 0xFF;
    emu.read_index++;
    return byte;
}

void stxg2_end(bool was_read) {
    if (was_read) {
        return;
    }
    if (emu.wlen >= 2) {
        emu.pointer = (uint16_t)(emu.wbuf[0] | (emu.wbuf[1] << 8));
    }
    if (emu.wlen == 6 && !emu.overflowed) {
        write_reg(emu.pointer, (uint32_t)emu.wbuf[2] | (uint32_t)emu.wbuf[3] << 8 | (uint32_t)emu.wbuf[4] << 16 | (uint32_t)emu.wbuf[5] << 24);
    } else if (emu.wlen != 2) {
        emu.i2c_status |= I2C_STATUS_BAD_SIZE;
    }
}

/* ---------- SPI ---------- */

void stxg2_spi_byte(uint8_t byte) {
    emu.spi_bytes++;
    emu.spi_crc = crc32_update(emu.spi_crc, &byte, 1);
    if (emu.spi_ctrl & SPI_CTRL_ENABLE) {
        emu.spi_rx_bytes_since_enable++;
        const bool routed = ((*plain_reg(REG_CROSSBAR) >> XBAR_OUT_MEMWR_SHIFT) & 0xFu) == XBAR_IN_SPI;
        if (routed && emu.mem_wr_active) {
            emu.mem_wr_bytes++;
        }
    }
}

void stxg2_spi_end(void) {
    emu.spi_transactions++;
}

void stxg2_spi_overflow(void) {
    emu.spi_overflows++;
}

/* ---------- PUBLIC ---------- */

static void reset_registers(void) {
    for (size_t i = 0; i < PLAIN_COUNT; i++) {
        emu.plain[i] = PLAIN_RESET[i].value;
    }
    emu.i2c_status = 0;
    emu.mode = MODE_STANDBY;
    emu.mode_busy_until = platform_millis();
    emu.spi_ctrl = 0;
    emu.mem_wr_stat = emu.mem_rd_stat = emu.mem_rt_stat = 0;
    emu.mem_wr_bytes = emu.mem_rd_count = 0;
    emu.mem_wr_active = emu.mem_rd_active = false;
    emu.pointer = 0;
}

void stxg2_init(void) {
    memset(&emu, 0, sizeof(emu));
    reset_registers();
}

void stxg2_set_fault(stxg2_fault_t fault) {
    emu.fault = fault;
    platform_hold_stxg2_bus(fault == STXG2_FAULT_HOLD_BUS);
}

void stxg2_reset(void) {
    const uint32_t state = platform_irq_save();
    reset_registers();
    emu.reg_reads = emu.reg_writes = emu.invalid = 0;
    emu.spi_bytes = emu.spi_transactions = emu.spi_crc = 0;
    emu.spi_rx_bytes_since_enable = 0;
    emu.recent_count = 0;
    emu.transactions = emu.nacks = emu.spi_overflows = 0;
    platform_irq_restore(state);
}

int stxg2_json(char *buf, size_t len) {
    const uint32_t state = platform_irq_save();
    const uint32_t scratch = *plain_reg(REG_SCRATCHPAD);
    int n = snprintf(buf, len,
                     "\"fault\":%d,\"mode\":%lu,\"scratchpad\":%lu,\"spi_ctrl\":%lu,\"i2c_transactions\":%lu,\"i2c_nacks\":%lu,"
                     "\"reg_reads\":%lu,\"reg_writes\":%lu,\"invalid_addr\":%lu,\"spi_bytes\":%lu,"
                     "\"spi_words_since_enable\":%lu,\"spi_transactions\":%lu,\"spi_crc\":%lu,\"spi_overflows\":%lu,"
                     "\"mem_wr_bytes\":%lu,\"recent_writes\":[",
                     (int)emu.fault, (unsigned long)emu.mode, (unsigned long)scratch, (unsigned long)emu.spi_ctrl,
                     (unsigned long)emu.transactions, (unsigned long)emu.nacks,
                     (unsigned long)emu.reg_reads, (unsigned long)emu.reg_writes, (unsigned long)emu.invalid,
                     (unsigned long)emu.spi_bytes, (unsigned long)(emu.spi_rx_bytes_since_enable / 4u),
                     (unsigned long)emu.spi_transactions, (unsigned long)emu.spi_crc, (unsigned long)emu.spi_overflows,
                     (unsigned long)emu.mem_wr_bytes);
    const uint32_t count = emu.recent_count < 8 ? emu.recent_count : 8;
    for (uint32_t i = 0; i < count && n > 0 && (size_t)n < len; i++) {
        const uint32_t index = (emu.recent_count - count + i) % 8;
        n += snprintf(buf + n, len - (size_t)n, "%s[%u,%lu]", i ? "," : "", emu.recent[index].addr,
                      (unsigned long)emu.recent[index].value);
    }
    if (n > 0 && (size_t)n < len) {
        n += snprintf(buf + n, len - (size_t)n, "]");
    }
    platform_irq_restore(state);
    return n;
}
