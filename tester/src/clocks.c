/**
 * clocks.c
 *
 * The time base: a 1 ms SysTick that also gives microseconds, and connecting peripherals that the Atmel START project
 * doesn't set up to a clock generator. The clocks themselves (120 MHz CPU from DPLL0 referenced to the DFLL / 48, the
 * 48 MHz DFLL in open loop) are set up by ASF's atmel_start_init(), from tester/atmel_start_config.atstart.
 */

#include "board.h"

static volatile uint32_t ms_ticks = 0;

void SysTick_Handler(void) {
    ms_ticks++;
}

uint32_t millis(void) {
    return ms_ticks;
}

// Wraps after 71 minutes, like any uint32_t microsecond count; compare times by subtraction
uint32_t micros(void) {
    const uint32_t primask = __get_PRIMASK();
    __disable_irq();
    uint32_t ms = ms_ticks;
    uint32_t count = SysTick->VAL;
    // A tick that hasn't been counted yet (wrapped while interrupts are off, or inside a higher-priority interrupt)
    if (SCB->ICSR & SCB_ICSR_PENDSTSET_Msk) {
        count = SysTick->VAL;
        ms++;
    }
    __set_PRIMASK(primask);
    return ms * 1000u + (SysTick->LOAD - count) / (CPU_HZ / 1000000u);
}

void gclk_connect(uint8_t peripheral_id, uint8_t generator) {
    GCLK->PCHCTRL[peripheral_id].reg = GCLK_PCHCTRL_GEN(generator) | GCLK_PCHCTRL_CHEN;
    while (!(GCLK->PCHCTRL[peripheral_id].reg & GCLK_PCHCTRL_CHEN)) {
    }
}

void systick_init(void) {
    SystemCoreClock = CPU_HZ;
    SysTick_Config(CPU_HZ / 1000u);
}
