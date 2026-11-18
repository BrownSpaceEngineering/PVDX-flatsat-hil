/**
 * buses.c
 *
 * Connects the shared device models (tester/common) to this board's buses: the perovskite board on SERCOM3 (I2C,
 * 0x20 and 0x40) and the S-band radio on SERCOM6 (I2C, 0x26), both i2c_slave.c, and on SERCOM7 (ASF SPI slave
 * SBAND_SPI, data in on PAD3 = PD11 [D50]). Also measures PVD_CRNT for the perovskite board's follower.
 */

#include "buses.h"

#include "analog_in.h"
#include "board.h"
#include "i2c_slave.h"
#include "tester_platform.h"
#include "pmb_model.h"
#include "stxg2_model.h"

static i2c_slave_t pmb_i2c;
static i2c_slave_t stxg2_i2c;
static struct io_descriptor *sband_spi_io;

static bool pmb_i2c_start(void *ctx, uint8_t addr, bool read) {
    return pmb_start(addr, read);
}
static void pmb_i2c_write(void *ctx, uint8_t addr, uint8_t byte) {
    pmb_write(addr, byte);
}
static uint8_t pmb_i2c_read(void *ctx, uint8_t addr) {
    return pmb_read(addr);
}
static void pmb_i2c_end(void *ctx, uint8_t addr, bool was_read) {
    pmb_end(addr, was_read);
}
static const i2c_slave_ops_t PMB_OPS = {pmb_i2c_start, pmb_i2c_write, pmb_i2c_read, pmb_i2c_end};

static bool stxg2_i2c_start(void *ctx, uint8_t addr, bool read) {
    return stxg2_start(read);
}
static void stxg2_i2c_write(void *ctx, uint8_t addr, uint8_t byte) {
    stxg2_write(byte);
}
static uint8_t stxg2_i2c_read(void *ctx, uint8_t addr) {
    return stxg2_read();
}
static void stxg2_i2c_end(void *ctx, uint8_t addr, bool was_read) {
    stxg2_end(was_read);
}
static const i2c_slave_ops_t STXG2_OPS = {stxg2_i2c_start, stxg2_i2c_write, stxg2_i2c_read, stxg2_i2c_end};

// ASF SPI slave callbacks, from the SERCOM7 interrupt
static void sband_spi_rx(const struct spi_s_async_descriptor *const spi) {
    uint8_t byte;
    while (io_read(sband_spi_io, &byte, 1) == 1) {
        stxg2_spi_byte(byte);
    }
}
static void sband_spi_complete(const struct spi_s_async_descriptor *const spi) {
    stxg2_spi_end(); // SS went high
}
static void sband_spi_error(const struct spi_s_async_descriptor *const spi, const int32_t status) {
    stxg2_spi_overflow();
}

void buses_init(void) {
    pmb_reset();
    stxg2_init();
    i2c_slave_init(&pmb_i2c, PMB_SERCOM, PMB_SDA, PMB_SCL, PMB_MCP23017_ADDR, PMB_INA226_ADDR, &PMB_OPS, NULL);
    i2c_slave_init(&stxg2_i2c, STXG2_SERCOM, STXG2_SDA, STXG2_SCL, STXG2_ADDR, 0, &STXG2_OPS, NULL);
    // The flatsat drives SPI_SBAND in mode 0 (src/drivers/stxg2/stxg2.h STXG2_SPI_MODE)
    spi_s_async_set_mode(&SBAND_SPI, SPI_MODE_0);
    spi_s_async_get_io_descriptor(&SBAND_SPI, &sband_spi_io);
    spi_s_async_register_callback(&SBAND_SPI, SPI_S_CB_RX, (FUNC_PTR)sband_spi_rx);
    spi_s_async_register_callback(&SBAND_SPI, SPI_S_CB_COMPLETE, (FUNC_PTR)sband_spi_complete);
    spi_s_async_register_callback(&SBAND_SPI, SPI_S_CB_ERROR, (FUNC_PTR)sband_spi_error);
    spi_s_async_enable(&SBAND_SPI);
}

void buses_poll(void) {
    i2c_slave_poll(&pmb_i2c);
    i2c_slave_poll(&stxg2_i2c);
    stxg2_poll();
    pmb_set_pvd_crnt(analog_in_read_pvd_crnt(analog_out_full_scale()));
}

void platform_hold_pmb_bus(bool hold) {
    i2c_slave_hold_bus(&pmb_i2c, hold);
}

void platform_hold_stxg2_bus(bool hold) {
    i2c_slave_hold_bus(&stxg2_i2c, hold);
}
