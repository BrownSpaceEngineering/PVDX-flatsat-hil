/**
 * ina226_model.c
 *
 * INA226 current monitor on the perovskite board (datasheet SBOS547A). Registers are 16-bit, MSB first; a write of
 * [reg] only sets the pointer. Conversions complete every AVG x (VBUSCT + VSHCT) in the continuous modes (one
 * conversion after a CONFIG write in the triggered modes) and set CVRF, which reading MASK_ENABLE or writing CONFIG
 * clears. Each conversion samples the pixel model: shunt = I x 10 ohm, bus = V(PIXEL+) = follower + shunt, and the
 * CURRENT and POWER registers follow the calibration as the datasheet's equations 3 and 4 do.
 */

#include "ina226_model.h"

#include <math.h>
#include <stdio.h>
#include <string.h>

#include "pixel_model.h"
#include "tester_platform.h"

#define REG_CONFIG 0x00u
#define REG_SHUNT 0x01u
#define REG_BUS 0x02u
#define REG_POWER 0x03u
#define REG_CURRENT 0x04u
#define REG_CALIBRATION 0x05u
#define REG_MASK_ENABLE 0x06u
#define REG_ALERT_LIMIT 0x07u
#define REG_MANUFACTURER_ID 0xFEu
#define REG_DIE_ID 0xFFu

#define CONFIG_POR 0x4127u
#define CONFIG_RST (1u << 15)
#define CONFIG_FIXED 0x4000u
#define ME_CVRF (1u << 3)
#define MFG_ID 0x5449u
#define DIE_ID 0x2260u

#define SHUNT_OHMS 10.0f
#define SHUNT_LSB_V 2.5e-6f
#define BUS_LSB_V 1.25e-3f
#define LOG_SIZE 256u

static const uint16_t CONVERSION_US[8] = {140, 204, 332, 588, 1100, 2116, 4156, 8244};
static const uint16_t AVERAGES[8] = {1, 4, 16, 64, 128, 256, 512, 1024};

static struct {
    ina226_fault_t fault;
    uint16_t config, calibration, mask_enable, alert_limit;
    int16_t shunt, current;
    uint16_t bus, power;
    uint8_t pointer;
    uint8_t wbuf[3];
    uint8_t wlen;
    uint16_t read_value;
    uint8_t read_index;
    bool converting;
    uint32_t next_conversion_us;
    // Inputs, set from the main loop
    float follow_v;
    uint16_t selected;
    // Statistics
    uint32_t conversions, current_reads;
    ina226_sample_t log[LOG_SIZE];
    uint32_t log_count;
} ina;

static uint32_t conversion_period_us(void) {
    const uint32_t avg = AVERAGES[(ina.config >> 9) & 7u];
    const uint32_t vbus = CONVERSION_US[(ina.config >> 6) & 7u];
    const uint32_t vsh = CONVERSION_US[(ina.config >> 3) & 7u];
    switch (ina.config & 7u) {
        case 1:
        case 5:
            return avg * vsh;
        case 2:
        case 6:
            return avg * vbus;
        default:
            return avg * (vbus + vsh);
    }
}

static void start_conversions(void) {
    const uint32_t mode = ina.config & 7u;
    ina.converting = mode != 0 && mode != 4;
    ina.next_conversion_us = platform_micros() + conversion_period_us();
}

static int32_t clamp16(float value) {
    const float rounded = roundf(value);
    if (rounded > 32767.0f) {
        return 32767;
    }
    return rounded < -32768.0f ? -32768 : (int32_t)rounded;
}

static void convert(void) {
    // Exactly one selected pixel conducts; none (or a shorted multi-selection) gives no current
    uint8_t pixel = 0xFF;
    if (ina.selected != 0 && (ina.selected & (ina.selected - 1u)) == 0) {
        pixel = (uint8_t)__builtin_ctz(ina.selected);
    }
    const float current_a = pixel_current_a(pixel, ina.follow_v);
    const float shunt_v = current_a * SHUNT_OHMS;
    ina.shunt = (int16_t)clamp16(shunt_v / SHUNT_LSB_V);
    const float bus_v = ina.follow_v + shunt_v;
    ina.bus = bus_v <= 0.0f ? 0 : (uint16_t)clamp16(bus_v / BUS_LSB_V);
    ina.current = (int16_t)clamp16((float)ina.shunt * (float)ina.calibration / 2048.0f);
    ina.power = (uint16_t)clamp16(fabsf((float)ina.current) * (float)ina.bus / 20000.0f);
    if (ina.fault != INA226_FAULT_NO_CVRF) {
        ina.mask_enable |= ME_CVRF;
    }
    ina.conversions++;
}

static void reset_registers(void) {
    ina.config = CONFIG_POR;
    ina.calibration = 0;
    ina.mask_enable = 0;
    ina.alert_limit = 0;
    ina.shunt = ina.current = 0;
    ina.bus = ina.power = 0;
    start_conversions();
}

static uint16_t read_register(uint8_t reg) {
    switch (reg) {
        case REG_CONFIG:
            return ina.config;
        case REG_SHUNT:
            return (uint16_t)ina.shunt;
        case REG_BUS:
            return ina.bus;
        case REG_POWER:
            return ina.power;
        case REG_CURRENT: {
            ina.current_reads++;
            ina226_sample_t *const s = &ina.log[ina.log_count % LOG_SIZE];
            s->selected = ina.selected;
            s->follow_mv = (int16_t)clamp16(ina.follow_v * 1000.0f);
            s->current_na = (int32_t)ina.current * 250; // Current_LSB the flatsat calibrates for
            ina.log_count++;
            return (uint16_t)ina.current;
        }
        case REG_CALIBRATION:
            return ina.calibration;
        case REG_MASK_ENABLE: {
            const uint16_t value = ina.mask_enable;
            ina.mask_enable &= (uint16_t)~ME_CVRF;
            return value;
        }
        case REG_ALERT_LIMIT:
            return ina.alert_limit;
        case REG_MANUFACTURER_ID:
            return ina.fault == INA226_FAULT_BAD_ID ? 0x1234u : MFG_ID;
        case REG_DIE_ID:
            return DIE_ID;
        default:
            return 0;
    }
}

static void write_register(uint8_t reg, uint16_t value) {
    switch (reg) {
        case REG_CONFIG:
            if (value & CONFIG_RST) {
                reset_registers();
                return;
            }
            ina.config = (uint16_t)((value & 0x0FFFu) | CONFIG_FIXED);
            ina.mask_enable &= (uint16_t)~ME_CVRF;
            start_conversions();
            break;
        case REG_CALIBRATION:
            ina.calibration = value & 0x7FFFu;
            break;
        case REG_MASK_ENABLE:
            ina.mask_enable = (uint16_t)((value & 0xFC01u) | (ina.mask_enable & 0x001Eu));
            break;
        case REG_ALERT_LIMIT:
            ina.alert_limit = value;
            break;
        default:
            break; // Read-only
    }
}

/* ---------- I2C CALLBACKS (from pmb_model.c) ---------- */

bool ina226_start(bool read) {
    if (ina.fault == INA226_FAULT_NACK) {
        return false;
    }
    ina226_poll();
    if (read) {
        ina.read_value = read_register(ina.pointer);
        ina.read_index = 0;
    } else {
        ina.wlen = 0;
    }
    return true;
}

void ina226_write(uint8_t byte) {
    if (ina.wlen < sizeof(ina.wbuf)) {
        ina.wbuf[ina.wlen++] = byte;
    }
}

uint8_t ina226_read(void) {
    const uint8_t byte = ina.read_index == 0 ? (uint8_t)(ina.read_value >> 8) : (uint8_t)ina.read_value;
    ina.read_index ^= 1u;
    return byte;
}

void ina226_end(bool was_read) {
    if (was_read || ina.wlen == 0) {
        return;
    }
    ina.pointer = ina.wbuf[0];
    if (ina.wlen == 3) {
        write_register(ina.pointer, (uint16_t)((ina.wbuf[1] << 8) | ina.wbuf[2]));
    }
}

/* ---------- PUBLIC ---------- */

void ina226_poll(void) {
    const uint32_t state = platform_irq_save();
    if (ina.converting && (int32_t)(platform_micros() - ina.next_conversion_us) >= 0) {
        convert();
        const uint32_t mode = ina.config & 7u;
        if (mode >= 5) {
            ina.next_conversion_us += conversion_period_us();
            if ((int32_t)(platform_micros() - ina.next_conversion_us) >= 0) {
                ina.next_conversion_us = platform_micros() + conversion_period_us(); // Fell behind; don't replay missed ones
            }
        } else {
            ina.converting = false; // Triggered: one conversion
        }
    }
    platform_irq_restore(state);
}

void ina226_set_inputs(float follow_v, uint16_t selected) {
    const uint32_t state = platform_irq_save();
    ina.follow_v = follow_v;
    ina.selected = selected;
    platform_irq_restore(state);
}

void ina226_reset(void) {
    const ina226_fault_t fault = ina.fault;
    const float follow_v = ina.follow_v;
    memset(&ina, 0, sizeof(ina));
    ina.fault = fault;
    ina.follow_v = follow_v;
    reset_registers();
}

void ina226_set_fault(ina226_fault_t fault) {
    ina.fault = fault;
}

uint32_t ina226_log_count(void) {
    return ina.log_count;
}

bool ina226_log_entry(uint32_t index, ina226_sample_t *sample) {
    const uint32_t state = platform_irq_save();
    const bool kept = index < ina.log_count && ina.log_count - index <= LOG_SIZE;
    if (kept) {
        *sample = ina.log[index % LOG_SIZE];
    }
    platform_irq_restore(state);
    return kept;
}

int ina226_json(char *buf, size_t len) {
    return snprintf(buf, len,
                    "\"fault\":%d,\"config\":%u,\"calibration\":%u,\"conversion_us\":%lu,\"conversions\":%lu,\"current_reads\":%lu,"
                    "\"follow_mv\":%ld,\"shunt_raw\":%d,\"bus_raw\":%u,\"current_raw\":%d",
                    (int)ina.fault, ina.config, ina.calibration, (unsigned long)conversion_period_us(), (unsigned long)ina.conversions,
                    (unsigned long)ina.current_reads, (long)clamp16(ina.follow_v * 1000.0f), ina.shunt, ina.bus, ina.current);
}
