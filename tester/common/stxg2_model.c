/**
 * stxg2_model.c
 *
 * CUBECOM STXG2 S-band transmitter, as far as src/drivers/stxg2/stxg2.c uses it, following the user manual (USM) and
 * ICD:
 * - It answers only while SBAND_EN and SBAND_RST (nRESET) are both high (ICD 2.2), and comes up from each power-on or
 *   reset with its reset register values; UPTIME counts from then.
 * - I2C at 0x26. A write is [addr LSB, addr MSB, value LSB first (4 bytes)]; a read is [addr LSB, addr MSB], repeated
 *   start, then 4 value bytes LSB first, then zeros. The ICD shows a 2-byte address echo before the value, but a real
 *   radio doesn't send one: a 6-byte read of ID returned 32 58 54 53 00 00 (src/drivers/stxg2/stxg2.c).
 * - I2C_STATUS flags (invalid address, bad size, read-only) clear when read; BOARD_STATUS bit 0 shows whether any is set.
 * - A MODE write sets MODE.Busy for MODE_SWITCH_MS, during which the radio keeps answering and ignores further MODE
 *   writes (USM 9.2.5.1). The mode_nack fault makes it NACK its address during the switch instead, as a comment in
 *   stxg2.c says the real one does (not confirmed), so the driver's retry path is tested too.
 * - SPI_CTRL can only be written in Flash Access, Config and Transmit (USM 9.2.3.7), and the memory registers not in
 *   Standby (USM 9.2.7); such writes set Not_Writeable. The RF synthesizers lock only in Config and Transmit.
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
#define MODE_FLASH_ACCESS 1u
#define MODE_CONFIG 2u
#define MODE_TRANSMIT 3u
#define BOARD_STATUS_I2C (1u << 0)
#define FIRST_MEMORY_REG 0x0090u // MEM_WR_CTRL to MEM_RT_ERR: not writable in Standby
#define LAST_MEMORY_REG 0x00B5u

// Registers that just hold what was written, with their reset values
typedef struct {
    uint16_t addr;
    uint32_t value;
} plain_reg_t;

static const plain_reg_t PLAIN_RESET[] = {
    {REG_SCRATCHPAD, 0},
    {0x0011, STXG2_ADDR}, // I2C_CONF holds the radio's address
    {0x0013, 0},
    {0x0015, 0},
    {0x001F, 0},
    {0x0061, 5000},
    {0x0062, 2450000}, // FREQUENCY, kHz: PVDX's radio is the 2400-2480 MHz option (src/drivers/stxg2/stxg2.h)
    {0x0065, 0x0000000C}, // ENCODING_CTRL: RRC 0x2, Insert_ASM
    {0x0066, 0x00000104}, // DVBS2_CONF: MODCOD 4, Pilot_En
    {0x0067, 0},
    {0x0070, 0x001E005F}, // PROTECTION_LEVEL_1-3, BOARD_TLIM (USM 9.2.6)
    {0x0071, 0x001B0061},
    {0x0072, 0x00000064},
    {0x0073, 0x004B0046},
    {0x0091, 0},
    {0x0092, 0},
    {0x0093, 0},
    {0x00A1, 0},
    {0x00A2, 0},
    {0x00B1, 0},
    {0x0110, 0x00000002}, // TF_CTRL: the register summary's value (its field table gives 0xAA02)
    {0x0112, 0x00002006}, // TF_CONF
    {REG_CROSSBAR, 0},
    {0x0121, 0},
    {0x0123, 0},
    {0x0124, 0},
    {0x0192, 27}, // PA_CONF_0, dBm: a flight-model value (USM 9.2.9.2)
    {0x0193, 0x00000001}, // PA_CONF_1: PID_Mode 1
    {0x0197, 0x005A01C2}, // PA_LIM
    {0x0198, 0x04380000}, // PA_TIMEOUT
};
#define PLAIN_COUNT (sizeof(PLAIN_RESET) / sizeof(PLAIN_RESET[0]))

// Read-only registers with fixed values. Telemetry reads 0 (25 C for the temperatures)
static const plain_reg_t FIXED[] = {
    {REG_ID, STXG2_ID_VALUE}, {REG_SERIAL, 0x00005EED}, {REG_SW_VERSION, 0x00000001}, {REG_FW_VERSION, 0x00000001}, // 1.0.0
    {REG_HW_VERSION, 0x00000002}, {0x0012, 0}, {0x0014, 0}, {0x0032, 0x00640064}, {0x0033, 0x00640064},
    {0x0034, 0x00640064}, {0x0035, 0x00640064}, {0x003E, 0}, {0x003F, 0}, {0x0068, 0}, {0x0082, 0}, {0x0083, 0},
    {0x0084, 0}, {0x0085, 0}, {0x0086, 0}, {0x0087, 0}, {0x0088, 0}, {0x0089, 0}, {0x008A, 0}, {REG_MEM_WR_ERR, 0},
    {REG_MEM_RD_ERR, 0}, {0x00B4, 0}, {0x00B5, 0}, {0x0111, 0}, {0x0113, 0}, {0x0114, 0}, {0x0115, 0}, {0x0122, 0},
    {0x0125, 0}, {0x0126, 0}, {0x0127, 0}, {0x0128, 0}, {0x0129, 0}, {0x0190, 0}, {0x0191, 0}, {0x0326, 0},
    {0x0327, 0}, {0x0328, 0},
};
#define FIXED_COUNT (sizeof(FIXED) / sizeof(FIXED[0]))

// Read-only blocks that read 0: HS_* (0x20-0x2B; whether any are writable isn't modelled), CURRENTS, VOLTAGES
static bool in_zero_block(uint16_t addr) {
    return (addr >= 0x0020 && addr <= 0x002B) || (addr >= 0x0040 && addr <= 0x005F);
}

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
    bool powered;         // SBAND_EN and SBAND_RST both high when last checked
    uint32_t powered_at;  // platform_millis() at the last power-on or reset release
    uint32_t spi_ctrl;
    uint32_t mem_wr_stat, mem_rd_stat, mem_rt_stat;
    uint32_t mem_wr_bytes, mem_rd_count;
    bool mem_wr_active, mem_rd_active;

    // Statistics for the host
    uint32_t transactions, nacks, spi_overflows;
    uint32_t reg_reads, reg_writes, invalid;
    uint32_t unpowered_accesses; // I2C or SPI traffic while the radio was off or held in reset
    uint32_t rejected_writes;    // Writes ignored for the mode the radio was in
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

static void reset_registers(void);

// Comes up with reset values whenever EN and nRESET are both high again. Called from the I2C interrupt and the main loop
static bool track_power(void) {
    const bool powered = platform_stxg2_powered();
    if (powered && !emu.powered) {
        reset_registers();
        emu.powered_at = platform_millis();
    }
    emu.powered = powered;
    return powered;
}

static bool rf_on(void) {
    return emu.mode == MODE_CONFIG || emu.mode == MODE_TRANSMIT;
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
            return platform_millis() - emu.powered_at;
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
        case REG_BOARD_STATUS:
            return emu.i2c_status != 0 ? BOARD_STATUS_I2C : 0;
        case REG_RF_STATUS:
            return rf_on() ? RF_STATUS_LOCKED : 0;
        case REG_MEMORY_STATUS:
            return memory_state();
        case REG_MEMORY_SIZE:
            return memory_state() == MEMORY_STATE_OFF ? 0 : MEMORY_SIZE_PACKETS; // Read from the flash when it starts
        case REG_MEM_WR_CTRL:
        case REG_MEM_RD_CTRL:
        case REG_MEM_RT_CTRL:
            return 0;
        case REG_MEM_WR_STAT:
            return emu.mem_wr_stat;
        case REG_MEM_WR_CNT_0: // Low and high 32 bits of a 64-bit byte count
            return emu.mem_wr_bytes;
        case REG_MEM_WR_CNT_1:
            return 0;
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
    if (in_zero_block(addr)) {
        return 0;
    }
    emu.i2c_status |= I2C_STATUS_INVALID_ADDR;
    emu.invalid++;
    return 0;
}

static void reject_write(void) {
    emu.i2c_status |= I2C_STATUS_NOT_WRITEABLE;
    emu.rejected_writes++;
}

static void write_reg(uint16_t addr, uint32_t value) {
    emu.reg_writes++;
    emu.recent[emu.recent_count % 8].addr = addr;
    emu.recent[emu.recent_count % 8].value = value;
    emu.recent_count++;

    if (addr >= FIRST_MEMORY_REG && addr <= LAST_MEMORY_REG && emu.mode == MODE_STANDBY) {
        reject_write(); // The memory is powered down in Standby
        return;
    }
    switch (addr) {
        case REG_MODE:
            if (mode_busy()) {
                emu.rejected_writes++; // Not processed while a change is in progress
                return;
            }
            emu.mode = value & 0xFFu;
            emu.mode_busy_until = platform_millis() + MODE_SWITCH_MS;
            if (emu.mode == MODE_STANDBY) {
                // Standby powers the memory down: sessions end, and the write counts clear (USM 9.2.7)
                emu.mem_wr_active = emu.mem_rd_active = false;
                emu.mem_wr_bytes = 0;
            }
            return;
        case REG_SPI_CTRL:
            if (emu.mode != MODE_FLASH_ACCESS && emu.mode != MODE_CONFIG && emu.mode != MODE_TRANSMIT) {
                reject_write();
                return;
            }
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
    bool read_only = in_zero_block(addr) || addr == REG_BOARD_STATUS || addr == REG_ID || addr == REG_UPTIME ||
                     addr == REG_I2C_STATUS || addr == REG_SPI_STATUS ||
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
    if (!track_power()) {
        emu.unpowered_accesses++;
        emu.nacks++;
        return false;
    }
    if (emu.fault == STXG2_FAULT_NACK || (emu.fault == STXG2_FAULT_MODE_NACK && mode_busy())) {
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
    const uint8_t byte = emu.read_index < 4 ? (uint8_t)(emu.read_value >> (8 * emu.read_index)) : 0x00;
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
    if (!track_power()) {
        emu.unpowered_accesses++;
        return;
    }
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

void stxg2_poll(void) {
    const uint32_t state = platform_irq_save();
    track_power();
    platform_irq_restore(state);
}

void stxg2_set_fault(stxg2_fault_t fault) {
    emu.fault = fault;
    platform_hold_stxg2_bus(fault == STXG2_FAULT_HOLD_BUS);
}

void stxg2_reset(void) {
    const uint32_t state = platform_irq_save();
    reset_registers();
    emu.reg_reads = emu.reg_writes = emu.invalid = emu.unpowered_accesses = emu.rejected_writes = 0;
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
                     "\"fault\":%d,\"powered\":%s,\"mode\":%lu,\"scratchpad\":%lu,\"spi_ctrl\":%lu,\"i2c_transactions\":%lu,"
                     "\"i2c_nacks\":%lu,\"unpowered_accesses\":%lu,\"rejected_writes\":%lu,"
                     "\"reg_reads\":%lu,\"reg_writes\":%lu,\"invalid_addr\":%lu,\"spi_bytes\":%lu,"
                     "\"spi_words_since_enable\":%lu,\"spi_transactions\":%lu,\"spi_crc\":%lu,\"spi_overflows\":%lu,"
                     "\"mem_wr_bytes\":%lu,\"recent_writes\":[",
                     (int)emu.fault, emu.powered ? "true" : "false", (unsigned long)emu.mode, (unsigned long)scratch,
                     (unsigned long)emu.spi_ctrl, (unsigned long)emu.transactions, (unsigned long)emu.nacks,
                     (unsigned long)emu.unpowered_accesses, (unsigned long)emu.rejected_writes,
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
