/**
 * selftest.c
 *
 * Checks the tester on its own, before it is wired to the flatsat:
 * - analog: reads one PWM output, PHD_12 on D9 (PB02), back through a jumper to A7 (PB04, the PVD_CRNT input).
 * - i2c: an I2C master (ASF SELFTEST_I2C, SERCOM1) talks to the STXG2, MCP23017 and INA226 emulators the way the
 *   flatsat's drivers do. Jumpers: D37 to D20 and D25 (SDA), D36 to D21 and D24 (SCL). The master's internal
 *   pull-ups are the bus pull-ups.
 * - spi: an SPI master (ASF SELFTEST_SPI, SERCOM0) sends a known block to the S-band SPI slave. Jumpers: D1 to D50
 *   (MOSI), D0 to D52 (SCK), D12 to D53 (CS).
 * Each test leaves the emulators reset.
 */

#include <math.h>
#include <stdio.h>

#include "analog_in.h"
#include "atmel_start.h"
#include "board.h"
#include "crc32.h"
#include "pmb_model.h"
#include "stxg2_model.h"
#include "tester_platform.h"

/* ---------- I2C MASTER ---------- */

// Write `wlen` bytes, then (if rlen > 0) repeated start and read `rlen` bytes, as src/drivers/pmb/pmb_i2c.c does
static bool i2c_transfer(uint8_t addr, uint8_t *wbuf, uint16_t wlen, uint8_t *rbuf, uint16_t rlen) {
    struct _i2c_m_msg wr = {.addr = addr, .len = wlen, .buffer = wbuf, .flags = rlen ? 0 : I2C_M_STOP};
    if (wlen > 0 && i2c_m_sync_transfer(&SELFTEST_I2C, &wr) != 0) {
        i2c_m_sync_send_stop(&SELFTEST_I2C);
        return false;
    }
    if (rlen == 0) {
        return true;
    }
    struct _i2c_m_msg rd = {.addr = addr, .len = rlen, .buffer = rbuf, .flags = I2C_M_RD | I2C_M_STOP};
    if (i2c_m_sync_transfer(&SELFTEST_I2C, &rd) != 0) {
        i2c_m_sync_send_stop(&SELFTEST_I2C);
        return false;
    }
    return true;
}

static bool master_stxg2_read(uint16_t reg, uint32_t *value) {
    uint8_t a[2] = {(uint8_t)reg, (uint8_t)(reg >> 8)};
    uint8_t v[4];
    if (!i2c_transfer(0x26, a, 2, v, 4)) {
        return false;
    }
    *value = (uint32_t)v[0] | (uint32_t)v[1] << 8 | (uint32_t)v[2] << 16 | (uint32_t)v[3] << 24;
    return true;
}

static bool master_stxg2_write(uint16_t reg, uint32_t value) {
    uint8_t b[6] = {(uint8_t)reg, (uint8_t)(reg >> 8), (uint8_t)value, (uint8_t)(value >> 8), (uint8_t)(value >> 16),
                    (uint8_t)(value >> 24)};
    return i2c_transfer(0x26, b, 6, NULL, 0);
}

int selftest_i2c(char *buf, size_t len) {
    gpio_set_pin_pull_mode(SELFTEST_SDA, GPIO_PULL_UP);
    gpio_set_pin_pull_mode(SELFTEST_SCL, GPIO_PULL_UP);
    i2c_m_sync_enable(&SELFTEST_I2C);

    uint32_t id = 0, scratch = 0;
    const bool stxg2_id_ok = master_stxg2_read(0x0001, &id) && id == 0x53545832u;
    const bool stxg2_scratch_ok = master_stxg2_write(0x0007, 0xB5E0CAFEu) && master_stxg2_read(0x0007, &scratch) && scratch == 0xB5E0CAFEu;

    uint8_t mfg_reg = 0xFE;
    uint8_t mfg[2] = {0};
    const bool ina_id_ok = i2c_transfer(0x40, &mfg_reg, 1, mfg, 2) && mfg[0] == 0x54 && mfg[1] == 0x49;

    uint8_t olat_write[3] = {0x14, 0x5A, 0xA5}; // OLATA, OLATB
    uint8_t olat_reg = 0x14;
    uint8_t olat[2] = {0};
    const bool mcp_ok = i2c_transfer(0x20, olat_write, 3, NULL, 0) && i2c_transfer(0x20, &olat_reg, 1, olat, 2) && olat[0] == 0x5A &&
                        olat[1] == 0xA5;

    i2c_m_sync_disable(&SELFTEST_I2C);
    gpio_set_pin_pull_mode(SELFTEST_SDA, GPIO_PULL_OFF);
    gpio_set_pin_pull_mode(SELFTEST_SCL, GPIO_PULL_OFF);
    stxg2_reset();
    pmb_reset();
    return snprintf(buf, len,
                    "\"pass\":%s,\"stxg2_id\":%lu,\"stxg2_id_ok\":%s,\"stxg2_scratchpad_ok\":%s,\"ina226_id_ok\":%s,"
                    "\"mcp23017_ok\":%s",
                    stxg2_id_ok && stxg2_scratch_ok && ina_id_ok && mcp_ok ? "true" : "false", (unsigned long)id,
                    stxg2_id_ok ? "true" : "false", stxg2_scratch_ok ? "true" : "false", ina_id_ok ? "true" : "false",
                    mcp_ok ? "true" : "false");
}

/* ---------- SPI MASTER ---------- */

int selftest_spi(char *buf, size_t len) {
    struct io_descriptor *io = NULL;
    spi_m_sync_get_io_descriptor(&SELFTEST_SPI, &io);
    spi_m_sync_enable(&SELFTEST_SPI);
    stxg2_reset();

    uint8_t block[64];
    for (size_t i = 0; i < sizeof(block); i++) {
        block[i] = (uint8_t)(i * 37u + 11u);
    }
    gpio_set_pin_level(SELFTEST_CS, false);
    io_write(io, block, sizeof(block));
    gpio_set_pin_level(SELFTEST_CS, true);
    for (uint32_t start = millis(); millis() - start < 2u;) {
        // Let the slave see SS go high
    }
    spi_m_sync_disable(&SELFTEST_SPI);

    const uint32_t expected_crc = crc32_update(0, block, sizeof(block));
    int n = snprintf(buf, len, "\"sent_bytes\":%u,\"expected_crc\":%lu,\"stxg2\":{", (unsigned)sizeof(block), (unsigned long)expected_crc);
    n += stxg2_json(buf + n, len - (size_t)n);
    n += snprintf(buf + n, len - (size_t)n, "}");
    stxg2_reset();
    return n;
}

/* ---------- ANALOG ---------- */

#define LOOPBACK_OUTPUT "phd12" // PB02 [D9], TCC2 WO2, jumpered to A7

// Steps one PWM output through three levels and reads it back on the PVD_CRNT input. 16 reads of 64 samples average
// out the raw PWM. The DAC outputs are checked in the flatsat tests (the thermistor device check reads them).
int selftest_analog(char *buf, size_t len) {
    static const float LEVELS[] = {0.5f, 1.65f, 2.8f};
    const bool was_armed = analog_out_armed();
    const uint8_t out = (uint8_t)analog_out_find(LOOPBACK_OUTPUT);
    const float previous = analog_out_get(out);
    analog_out_arm(true);
    int n = snprintf(buf, len, "\"readings\":[");
    float worst = 0.0f;
    for (size_t l = 0; l < sizeof(LEVELS) / sizeof(LEVELS[0]); l++) {
        analog_out_set(out, LEVELS[l]);
        for (uint32_t start = millis(); millis() - start < 5u;) {
            // Settle
        }
        float measured = 0.0f;
        for (int i = 0; i < 16; i++) {
            measured += analog_in_read_pvd_crnt(analog_out_full_scale()) / 16.0f;
        }
        const float error = fabsf(measured - LEVELS[l]);
        if (error > worst) {
            worst = error;
        }
        n += snprintf(buf + n, len - (size_t)n, "%s[\"%s\",%d,%d]", l ? "," : "", LOOPBACK_OUTPUT, (int)(LEVELS[l] * 1000.0f),
                      (int)(measured * 1000.0f));
    }
    analog_out_set(out, previous);
    analog_out_arm(was_armed);
    n += snprintf(buf + n, len - (size_t)n, "],\"worst_error_mv\":%d,\"pass\":%s", (int)(worst * 1000.0f), worst < 0.05f ? "true" : "false");
    return n;
}
