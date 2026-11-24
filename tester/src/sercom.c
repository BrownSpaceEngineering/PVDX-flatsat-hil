/**
 * sercom.c
 *
 * Clocks and interrupt dispatch for the SERCOMs the tester drives itself: the I2C targets on SERCOM3, SERCOM6 and,
 * for the fuel gauges, SERCOM0, 1, 4 and 5 (i2c_slave.c). Every other SERCOM is an ASF driver, which defines its own interrupt handlers. Each SERCOM has four
 * interrupt lines on the SAMD51; all four go to the one handler registered for that SERCOM.
 */

#include "sercom.h"

#include <stddef.h>

static Sercom *const SERCOMS[SERCOM_INST_NUM] = SERCOM_INSTS;
static const uint8_t CORE_GCLK_IDS[SERCOM_INST_NUM] = {SERCOM0_GCLK_ID_CORE, SERCOM1_GCLK_ID_CORE, SERCOM2_GCLK_ID_CORE,
                                                       SERCOM3_GCLK_ID_CORE, SERCOM4_GCLK_ID_CORE, SERCOM5_GCLK_ID_CORE,
                                                       SERCOM6_GCLK_ID_CORE, SERCOM7_GCLK_ID_CORE};
_Static_assert(SERCOM7_3_IRQn - SERCOM0_0_IRQn == 4 * SERCOM_INST_NUM - 1, "SERCOM interrupt lines are not consecutive");

static void (*handlers[SERCOM_INST_NUM])(void *);
static void *handler_args[SERCOM_INST_NUM];

uint8_t sercom_index(Sercom *sercom) {
    for (uint8_t i = 0; i < SERCOM_INST_NUM; i++) {
        if (SERCOMS[i] == sercom) {
            return i;
        }
    }
    return 0xFF;
}

void sercom_enable(Sercom *sercom, void (*handler)(void *), void *arg, uint8_t priority) {
    const uint8_t index = sercom_index(sercom);
    switch (index) {
        case 0:
            MCLK->APBAMASK.bit.SERCOM0_ = 1;
            break;
        case 1:
            MCLK->APBAMASK.bit.SERCOM1_ = 1;
            break;
        case 2:
            MCLK->APBBMASK.bit.SERCOM2_ = 1;
            break;
        case 3:
            MCLK->APBBMASK.bit.SERCOM3_ = 1;
            break;
        case 4:
            MCLK->APBDMASK.bit.SERCOM4_ = 1;
            break;
        case 5:
            MCLK->APBDMASK.bit.SERCOM5_ = 1;
            break;
        case 6:
            MCLK->APBDMASK.bit.SERCOM6_ = 1;
            break;
        case 7:
            MCLK->APBDMASK.bit.SERCOM7_ = 1;
            break;
        default:
            return;
    }
    gclk_connect(CORE_GCLK_IDS[index], GCLK_48M);
    gclk_connect(SERCOM0_GCLK_ID_SLOW, GCLK_32K); // Shared by every SERCOM (SMBus timeouts, unused), as in ASF

    handlers[index] = handler;
    handler_args[index] = arg;
    for (uint8_t line = 0; line < 4; line++) {
        const IRQn_Type irq = (IRQn_Type)(SERCOM0_0_IRQn + 4 * index + line);
        NVIC_ClearPendingIRQ(irq);
        if (handler != NULL) {
            NVIC_SetPriority(irq, priority);
            NVIC_EnableIRQ(irq);
        } else {
            NVIC_DisableIRQ(irq);
        }
    }
}

static void dispatch(uint8_t index) {
    if (handlers[index] != NULL) {
        handlers[index](handler_args[index]);
    }
}

#define SERCOM_HANDLERS(n)                                                                                                       \
    void SERCOM##n##_0_Handler(void) {                                                                                           \
        dispatch(n);                                                                                                             \
    }                                                                                                                            \
    void SERCOM##n##_1_Handler(void) {                                                                                           \
        dispatch(n);                                                                                                             \
    }                                                                                                                            \
    void SERCOM##n##_2_Handler(void) {                                                                                           \
        dispatch(n);                                                                                                             \
    }                                                                                                                            \
    void SERCOM##n##_3_Handler(void) {                                                                                           \
        dispatch(n);                                                                                                             \
    }

SERCOM_HANDLERS(0) // Fuel gauge 3 I2C target
SERCOM_HANDLERS(1) // Fuel gauge 0 I2C target
SERCOM_HANDLERS(3) // Perovskite board I2C target
SERCOM_HANDLERS(4) // Fuel gauge 2 I2C target
SERCOM_HANDLERS(5) // Fuel gauge 1 I2C target
SERCOM_HANDLERS(6) // S-band I2C target
