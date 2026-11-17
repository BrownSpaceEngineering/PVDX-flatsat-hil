/**
 * watch.c
 *
 * Records the flatsat's digital outputs (magnetorquer H-bridges, heaters, burn wire, S-band EN/RST) for the host, in
 * place of a logic analyzer. Each input is on its own EIC line, which interrupts on both edges; the interrupt stores
 * the time and the level of every input as one record:
 *
 *     uint32_t t_us    micros() when the edge was serviced (about 1 us after it)
 *     uint16_t levels  bit n: the input on EIC line n
 *     uint16_t edges   bit n: line n had an edge since the last record. Set with an unchanged level, it was a pulse
 *                      shorter than the interrupt latency.
 *
 * While recording, the records go into a RAM ring, and watch_poll() moves them to RTT channel 2, which the host drains
 * (hil/lib/logic.py). The first record of a recording has the starting levels and no edges. A record that doesn't fit
 * in the ring is dropped and counted; the host treats a recording with drops as failed. The magnetorquers' 1 kHz PWM is
 * the fastest traffic, about 6000 records/s with all three axes driving.
 *
 * Unwired inputs are pulled down so they don't make edges. Lines 10 and 11 are unused: every pin on them is taken.
 */

#include <stdio.h>

#include "SEGGER_RTT.h"
#include "atmel_start.h"
#include "board.h"
#include "tester_platform.h"
#include "watch.h"

#define EDGE_CHANNEL 2u      // RTT up channel the records stream on
#define RING_RECORDS 4096u   // Power of 2: 0.7 s of the fastest traffic, while the main loop is busy
#define EIC_LINES 16u

typedef struct {
    uint32_t t_us;
    uint16_t levels;
    uint16_t edges;
} watch_record_t;

typedef struct {
    const char *name;
    uint32_t pinmux; // ASF PINMUX_ value for the pin's EIC function
    uint8_t line;    // EIC line, fixed by the pin
} watch_input_t;

// Flatsat pins are in hil/README.md
static const watch_input_t INPUTS[] = {
    {"mtq_x_slp", PINMUX_PB16A_EIC_EXTINT0, 0},   // [D14]
    {"mtq_y_slp", PINMUX_PC01A_EIC_EXTINT1, 1},   // [A4]
    {"mtq_x_in1", PINMUX_PC18A_EIC_EXTINT2, 2},   // [D2]
    {"mtq_x_in2", PINMUX_PC19A_EIC_EXTINT3, 3},   // [D3]
    {"mtq_y_in1", PINMUX_PC20A_EIC_EXTINT4, 4},   // [D4]
    {"mtq_y_in2", PINMUX_PC21A_EIC_EXTINT5, 5},   // [D5]
    {"mtq_z_in1", PINMUX_PC06A_EIC_EXTINT6, 6},   // [D46]
    {"mtq_z_slp", PINMUX_PC23A_EIC_EXTINT7, 7},   // [D17]
    {"heater1", PINMUX_PB08A_EIC_EXTINT8, 8},     // [A11]
    {"mtq_z_in2", PINMUX_PC07A_EIC_EXTINT9, 9},   // [D47]
    {"heater2", PINMUX_PA12A_EIC_EXTINT12, 12},   // [D26]
    {"burn", PINMUX_PA13A_EIC_EXTINT13, 13},      // [D27]
    {"sband_en", PINMUX_PA14A_EIC_EXTINT14, 14},  // [D28]
    {"sband_rst", PINMUX_PA15A_EIC_EXTINT15, 15}, // [D23]
};
#define INPUT_COUNT (sizeof(INPUTS) / sizeof(INPUTS[0]))

static watch_record_t ring[RING_RECORDS];
static volatile uint32_t head = 0; // Next record the interrupt writes (free-running; index with % RING_RECORDS)
static uint32_t tail = 0;          // Next record watch_poll() sends
static volatile uint32_t recorded = 0;
static volatile uint32_t dropped = 0;
static volatile bool recording = false;
static uint16_t line_mask = 0;
static char edge_buffer[16384];

static uint16_t levels(void) {
    uint32_t in[4];
    for (uint8_t g = 0; g < 4; g++) {
        in[g] = PORT->Group[g].IN.reg;
    }
    uint16_t out = 0;
    for (uint8_t i = 0; i < INPUT_COUNT; i++) {
        const uint8_t pin = (uint8_t)PINMUX_PIN(INPUTS[i].pinmux);
        if (in[pin >> 5] & (1u << (pin & 31u))) {
            out |= (uint16_t)(1u << INPUTS[i].line);
        }
    }
    return out;
}

// From an interrupt, or with interrupts off
static void push(uint16_t edges) {
    if (head - tail >= RING_RECORDS) {
        dropped++;
        return;
    }
    // Levels after the time: an edge between the two still gets a record of its own, since its flag is already clear
    watch_record_t *const r = &ring[head % RING_RECORDS];
    r->t_us = micros();
    r->levels = levels();
    r->edges = edges;
    head++;
    recorded++;
}

static void on_edge(void) {
    const uint16_t flags = (uint16_t)(EIC->INTFLAG.reg & line_mask);
    EIC->INTFLAG.reg = flags; // Before the levels are read, so a later edge interrupts again
    if (flags == 0) {
        return; // Already serviced along with another line's edge
    }
    if (recording) {
        push(flags);
    }
}

#define EIC_HANDLER(n)           \
    void EIC_##n##_Handler(void) { \
        on_edge();               \
    }
EIC_HANDLER(0)
EIC_HANDLER(1)
EIC_HANDLER(2)
EIC_HANDLER(3)
EIC_HANDLER(4)
EIC_HANDLER(5)
EIC_HANDLER(6)
EIC_HANDLER(7)
EIC_HANDLER(8)
EIC_HANDLER(9)
EIC_HANDLER(12)
EIC_HANDLER(13)
EIC_HANDLER(14)
EIC_HANDLER(15)

// After atmel_start_init(), which may have given some of these pins to other peripherals
void watch_init(void) {
    SEGGER_RTT_ConfigUpBuffer(EDGE_CHANNEL, "PVDX tester edges", edge_buffer, sizeof(edge_buffer), SEGGER_RTT_MODE_NO_BLOCK_SKIP);

    MCLK->APBAMASK.bit.EIC_ = 1;
    gclk_connect(EIC_GCLK_ID, GCLK_48M); // Synchronous edge detection: pulses over about 40 ns are seen
    EIC->CTRLA.reg = EIC_CTRLA_SWRST;
    while (EIC->SYNCBUSY.reg & EIC_SYNCBUSY_SWRST) {
    }

    uint32_t config[2] = {0, 0};
    for (uint8_t i = 0; i < INPUT_COUNT; i++) {
        // Never true, but tells GCC that ASF's "function off" branch, which would index port 255, can't be reached
        if (INPUTS[i].pinmux == GPIO_PIN_FUNCTION_OFF) {
            continue;
        }
        const uint8_t line = INPUTS[i].line;
        const uint8_t pin = (uint8_t)PINMUX_PIN(INPUTS[i].pinmux);
        gpio_set_pin_direction(pin, GPIO_DIRECTION_IN);
        gpio_set_pin_pull_mode(pin, GPIO_PULL_DOWN);
        gpio_set_pin_function(pin, INPUTS[i].pinmux);
        config[line / 8u] |= EIC_CONFIG_SENSE0_BOTH_Val << (4u * (line % 8u)); // No filter
        line_mask |= (uint16_t)(1u << line);

        const IRQn_Type irq = (IRQn_Type)(EIC_0_IRQn + line);
        NVIC_SetPriority(irq, 0); // Above the I2C targets, so edge times stay within a microsecond or so
        NVIC_ClearPendingIRQ(irq);
        NVIC_EnableIRQ(irq);
    }
    EIC->CONFIG[0].reg = config[0];
    EIC->CONFIG[1].reg = config[1];
    EIC->CTRLA.reg = EIC_CTRLA_ENABLE;
    while (EIC->SYNCBUSY.reg & EIC_SYNCBUSY_ENABLE) {
    }
}

static void begin(void) {
    const uint32_t irq = platform_irq_save();
    head = tail = 0;
    recorded = dropped = 0;
    EIC->INTENCLR.reg = EIC_INTENCLR_EXTINT_Msk;
    EIC->INTFLAG.reg = line_mask;
    recording = true;
    EIC->INTENSET.reg = line_mask;
    push(0); // The starting levels
    platform_irq_restore(irq);
}

static void end(void) {
    EIC->INTENCLR.reg = EIC_INTENCLR_EXTINT_Msk;
    recording = false;
}

void watch_poll(void) {
    while (tail != head) {
        const uint32_t pending = head - tail;
        const uint32_t contiguous = RING_RECORDS - tail % RING_RECORDS;
        const uint32_t room = SEGGER_RTT_GetAvailWriteSpace(EDGE_CHANNEL) / sizeof(watch_record_t);
        uint32_t n = pending < contiguous ? pending : contiguous;
        n = n < room ? n : room;
        if (n == 0) {
            return; // The host hasn't caught up
        }
        SEGGER_RTT_WriteNoLock(EDGE_CHANNEL, &ring[tail % RING_RECORDS], n * sizeof(watch_record_t));
        tail += n;
    }
}

int watch_start(char *buf, size_t len) {
    end();
    begin();
    return snprintf(buf, len, "\"t_us\":%lu", (unsigned long)ring[0].t_us);
}

// The records already in the ring still go to the host
int watch_stop(char *buf, size_t len) {
    end();
    return snprintf(buf, len, "\"t_us\":%lu,\"records\":%lu,\"dropped\":%lu", (unsigned long)micros(), (unsigned long)recorded,
                    (unsigned long)dropped);
}

int watch_get(char *buf, size_t len) {
    const uint16_t now = levels();
    int n = snprintf(buf, len, "\"recording\":%s,\"lines\":{", recording ? "true" : "false");
    for (uint8_t i = 0; i < INPUT_COUNT; i++) {
        n += snprintf(buf + n, len - (size_t)n, "%s\"%s\":%u", i ? "," : "", INPUTS[i].name, INPUTS[i].line);
    }
    n += snprintf(buf + n, len - (size_t)n, "},\"levels\":{");
    for (uint8_t i = 0; i < INPUT_COUNT; i++) {
        n += snprintf(buf + n, len - (size_t)n, "%s\"%s\":%u", i ? "," : "", INPUTS[i].name, (now >> INPUTS[i].line) & 1u);
    }
    return n + snprintf(buf + n, len - (size_t)n, "}");
}
