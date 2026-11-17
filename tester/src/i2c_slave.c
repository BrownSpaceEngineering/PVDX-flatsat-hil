/**
 * i2c_slave.c
 *
 * Interrupt-driven SERCOM I2C slave answering one or two 7-bit addresses (AMODE = 1: ADDR and ADDRMASK are both
 * addresses). Smart mode ACKs a written byte when DATA is read; SCLSM = 0 stretches the clock before every ACK bit, so
 * the device model decides there whether to ACK an address.
 *
 * A master that resets in the middle of reading from us leaves the slave driving a 0 on SDA, waiting for clocks that
 * never come, and nothing else on the bus can be used (the flatsat's I2C_SENSORS also has the fuel gauges and camera).
 * i2c_slave_poll() notices a transaction that has been idle for STUCK_MS with SCL high (a master stretching the clock
 * holds SCL low instead) and resets the SERCOM, which releases SDA.
 */

#include "i2c_slave.h"

#include "sercom.h"
#include "tester_platform.h"

#define STUCK_MS 50u

static void finish(i2c_slave_t *s) {
    if (s->active) {
        s->active = false;
        s->ops->end(s->ctx, s->addr, s->read);
    }
}

static void service(void *arg) {
    i2c_slave_t *const s = arg;
    SercomI2cs *const hw = &s->sercom->I2CS;
    const uint8_t flags = hw->INTFLAG.reg & hw->INTENSET.reg;
    s->last_activity_ms = millis();

    if (flags & SERCOM_I2CS_INTFLAG_ERROR) {
        hw->INTFLAG.reg = SERCOM_I2CS_INTFLAG_ERROR;
        hw->STATUS.reg = SERCOM_I2CS_STATUS_BUSERR | SERCOM_I2CS_STATUS_COLL | SERCOM_I2CS_STATUS_LOWTOUT;
        s->bus_errors++;
    }

    // A stop ends the transaction. Handled before AMATCH in case a new start follows it closely.
    if (flags & SERCOM_I2CS_INTFLAG_PREC) {
        hw->INTFLAG.reg = SERCOM_I2CS_INTFLAG_PREC;
        finish(s);
    }

    if (flags & SERCOM_I2CS_INTFLAG_AMATCH) {
        if (s->hold_bus) {
            // Leave AMATCH pending with its interrupt off: SCL stays low until i2c_slave_hold_bus(s, false)
            hw->INTENCLR.reg = SERCOM_I2CS_INTENCLR_AMATCH;
            return;
        }
        finish(s); // Repeated start
        s->addr = (uint8_t)((hw->DATA.reg >> 1) & 0x7F);
        s->read = hw->STATUS.bit.DIR;
        s->first_byte = true;
        const bool ack = s->ops->start(s->ctx, s->addr, s->read);
        s->active = ack;
        if (ack) {
            s->transactions++;
            hw->CTRLB.reg = (hw->CTRLB.reg & ~(SERCOM_I2CS_CTRLB_ACKACT | SERCOM_I2CS_CTRLB_CMD_Msk)) | SERCOM_I2CS_CTRLB_CMD(3);
        } else {
            s->nacks++;
            hw->CTRLB.reg = (hw->CTRLB.reg & ~SERCOM_I2CS_CTRLB_CMD_Msk) | SERCOM_I2CS_CTRLB_ACKACT | SERCOM_I2CS_CTRLB_CMD(2);
        }
        return;
    }

    if (flags & SERCOM_I2CS_INTFLAG_DRDY) {
        if (s->read) {
            if (!s->first_byte && hw->STATUS.bit.RXNACK) {
                // The master NACKed the last byte: wait for its stop or repeated start
                hw->CTRLB.reg = (hw->CTRLB.reg & ~(SERCOM_I2CS_CTRLB_ACKACT | SERCOM_I2CS_CTRLB_CMD_Msk)) | SERCOM_I2CS_CTRLB_CMD(2);
                return;
            }
            s->first_byte = false;
            hw->DATA.reg = s->ops->read(s->ctx, s->addr);
        } else {
            hw->CTRLB.reg &= ~SERCOM_I2CS_CTRLB_ACKACT;
            const uint8_t byte = (uint8_t)hw->DATA.reg; // Smart mode: reading DATA sends the ACK
            s->ops->write(s->ctx, s->addr, byte);
        }
    }
}

static void configure(i2c_slave_t *s) {
    SercomI2cs *const hw = &s->sercom->I2CS;
    hw->CTRLA.bit.SWRST = 1;
    while (hw->SYNCBUSY.bit.SWRST) {
    }
    hw->CTRLA.reg = SERCOM_I2CS_CTRLA_MODE(4) | SERCOM_I2CS_CTRLA_SDAHOLD(2) | SERCOM_I2CS_CTRLA_SPEED(0);
    hw->CTRLB.reg = SERCOM_I2CS_CTRLB_SMEN | SERCOM_I2CS_CTRLB_AMODE(1);
    hw->ADDR.reg = SERCOM_I2CS_ADDR_ADDR(s->addr0) | SERCOM_I2CS_ADDR_ADDRMASK(s->addr1 != 0 ? s->addr1 : s->addr0);
    hw->INTENSET.reg = SERCOM_I2CS_INTENSET_PREC | SERCOM_I2CS_INTENSET_AMATCH | SERCOM_I2CS_INTENSET_DRDY |
                       SERCOM_I2CS_INTENSET_ERROR;
    hw->CTRLA.bit.ENABLE = 1;
    while (hw->SYNCBUSY.bit.ENABLE) {
    }
}

void i2c_slave_init(i2c_slave_t *s, Sercom *sercom, uint32_t sda, uint32_t scl, uint8_t addr0, uint8_t addr1,
                    const i2c_slave_ops_t *ops, void *ctx) {
    s->sercom = sercom;
    s->ops = ops;
    s->ctx = ctx;
    s->active = false;
    s->hold_bus = false;
    s->addr0 = addr0;
    s->addr1 = addr1;
    s->scl_pin = (uint8_t)PINMUX_PIN(scl);
    s->transactions = 0;
    s->nacks = 0;
    s->bus_errors = 0;
    s->recoveries = 0;

    sercom_enable(sercom, service, s, 1);
    configure(s);

    gpio_set_pin_function(PINMUX_PIN(sda), sda);
    gpio_set_pin_function(PINMUX_PIN(scl), scl);
    PORT->Group[s->scl_pin >> 5].PINCFG[s->scl_pin & 31u].bit.INEN = 1; // So i2c_slave_poll() can read SCL
}

void i2c_slave_poll(i2c_slave_t *s) {
    if (!s->active || s->hold_bus || millis() - s->last_activity_ms < STUCK_MS) {
        return;
    }
    const bool scl_high = PORT->Group[s->scl_pin >> 5].IN.reg & (1u << (s->scl_pin & 31u));
    if (!scl_high) {
        return; // The master is stretching the clock
    }
    const uint32_t irq = platform_irq_save();
    configure(s);
    finish(s);
    s->recoveries++;
    platform_irq_restore(irq);
}

void i2c_slave_hold_bus(i2c_slave_t *s, bool hold) {
    s->hold_bus = hold;
    if (!hold) {
        s->sercom->I2CS.INTENSET.reg = SERCOM_I2CS_INTENSET_AMATCH; // Services an address match left pending
    }
}
