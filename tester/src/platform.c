/**
 * platform.c
 *
 * tester/common/tester_platform.h for the Metro M4 Grand Central. The host link is SEGGER RTT channel 1, read and
 * written by the host through a J-Link on the tester's SWD header (hil/lib/tester.py). The bus hooks are in buses.c,
 * and the analog outputs in analog_out.c.
 */

#include "tester_platform.h"

#include "SEGGER_RTT.h"
#include "board.h"

// The UF2 bootloader stays in bootloader mode after a reset when it finds this word at the end of RAM
#define UF2_DOUBLE_TAP_ADDR (HSRAM_ADDR + HSRAM_SIZE - 4u)
#define UF2_DOUBLE_TAP_MAGIC 0xF01669EFu

// Not channel 0: SEGGER_RTT_ConfigUp/DownBuffer keep channel 0's name and default buffers
#define LINK_CHANNEL 1u
#define LINK_WRITE_TIMEOUT_MS 500u // How long a reply waits for the host to make room before it is dropped

// Big enough for the largest reply (cmd.c REPLY_MAX), so a reply never has to wait for the host to drain the last one
static char link_up_buffer[8192];
static char link_down_buffer[512];

void platform_link_init(void) {
    SEGGER_RTT_Init();
    SEGGER_RTT_ConfigUpBuffer(LINK_CHANNEL, "PVDX tester", link_up_buffer, sizeof(link_up_buffer), SEGGER_RTT_MODE_NO_BLOCK_SKIP);
    SEGGER_RTT_ConfigDownBuffer(LINK_CHANNEL, "PVDX tester", link_down_buffer, sizeof(link_down_buffer), SEGGER_RTT_MODE_NO_BLOCK_SKIP);
}

const char *platform_name(void) {
    return "pvdx-flatsat-tester-grand-central";
}

uint32_t platform_millis(void) {
    return millis();
}

uint32_t platform_micros(void) {
    return micros();
}

uint32_t platform_irq_save(void) {
    const uint32_t primask = __get_PRIMASK();
    __disable_irq();
    return primask;
}

void platform_irq_restore(uint32_t state) {
    __set_PRIMASK(state);
}

size_t platform_link_read(char *buf, size_t max) {
    return SEGGER_RTT_Read(LINK_CHANNEL, buf, max);
}

// Whole replies or nothing: a reply cut short would be unparseable JSON
void platform_link_write(const char *buf, size_t len) {
    const uint32_t start = millis();
    while (SEGGER_RTT_GetAvailWriteSpace(LINK_CHANNEL) < len) {
        if (millis() - start > LINK_WRITE_TIMEOUT_MS) {
            return; // Nobody is reading
        }
    }
    SEGGER_RTT_Write(LINK_CHANNEL, buf, len);
}

bool platform_enter_bootloader(void) {
    for (uint32_t start = millis(); millis() - start < 50u;) {
        // Give the host a moment to read the reply
    }
    *(volatile uint32_t *)UF2_DOUBLE_TAP_ADDR = UF2_DOUBLE_TAP_MAGIC;
    NVIC_SystemReset();
    return true;
}
