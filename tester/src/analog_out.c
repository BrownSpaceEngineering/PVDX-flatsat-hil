/**
 * analog_out.c
 *
 * What the tester drives, all wired as in hil/README.md:
 * - phd0-3: the flatsat's photodiode inputs PHD_0, 5, 10 and 15
 * - pvd0-3: perovskite cells on four of the perovskite board's pixels (PWM behind a series resistor)
 * - therm0-3: the flatsat's thermistor inputs THERM_0-3
 * All are 12-bit PWM at 120 MHz / 4096 = 29.3 kHz: TCC0 and TCC1 in single-slope PWM with PER = 4095, and TC6/TC7 in
 * match PWM (CC0 = 4095 is the period, CC1 the duty, output on WO1). All are ASF drivers from the Atmel START project
 * (AOUT_TCC0/1, AOUT_TC6/7). Every output is high impedance until analog_out_arm(), so a powered-down flatsat is never
 * fed through its pins. The DAC (A0, A1) is unused and its pins are kept disconnected.
 */

#include <string.h>

#include "atmel_start.h"
#include "board.h"
#include "tester_platform.h"

#define PWM_TOP 4095u // TCC PER in the Atmel START project; the TC period (CC0) is set to it with each duty

typedef enum {
    AOUT_PHD_0 = 0,
    AOUT_PHD_1,
    AOUT_PHD_2,
    AOUT_PHD_3,
    AOUT_PVD_0,
    AOUT_PVD_1,
    AOUT_PVD_2,
    AOUT_PVD_3,
    AOUT_THERM_0,
    AOUT_THERM_1,
    AOUT_THERM_2,
    AOUT_THERM_3,
    ANALOG_OUT_COUNT,
} analog_out_t;

typedef struct {
    const char *name;
    uint32_t pinmux; // ASF PINMUX_ value: pin and peripheral function
    void *timer;     // Tcc * (duty written to CCBUF[channel]), or struct pwm_descriptor * for a TC
    bool is_tc;
    uint8_t channel;
} pwm_channel_t;

static const pwm_channel_t PWM_CHANNELS[ANALOG_OUT_COUNT] = {
    [AOUT_PHD_0] = {"phd0", PINMUX_PC10F_TCC0_WO0, TCC0, false, 0},         // [D45]
    [AOUT_PHD_1] = {"phd1", PINMUX_PC11F_TCC0_WO1, TCC0, false, 1},         // [D44]
    [AOUT_PHD_2] = {"phd2", PINMUX_PC12F_TCC0_WO2, TCC0, false, 2},         // [D41]
    [AOUT_PHD_3] = {"phd3", PINMUX_PC13F_TCC0_WO3, TCC0, false, 3},         // [D40]
    [AOUT_PVD_0] = {"pvd0", PINMUX_PC14F_TCC0_WO4, TCC0, false, 4},         // [D43]
    [AOUT_PVD_1] = {"pvd1", PINMUX_PC15F_TCC0_WO5, TCC0, false, 5},         // [D42]
    [AOUT_PVD_2] = {"pvd2", PINMUX_PD20F_TCC1_WO0, TCC1, false, 0},         // [D6]
    [AOUT_PVD_3] = {"pvd3", PINMUX_PD21F_TCC1_WO1, TCC1, false, 1},         // [D7]
    [AOUT_THERM_0] = {"therm0", PINMUX_PA18F_TCC1_WO2, TCC1, false, 2},     // [D35]
    [AOUT_THERM_1] = {"therm1", PINMUX_PA19F_TCC1_WO3, TCC1, false, 3},     // [D34]
    [AOUT_THERM_2] = {"therm2", PINMUX_PB17E_TC6_WO1, &AOUT_TC6, true, 1},  // [D15]
    [AOUT_THERM_3] = {"therm3", PINMUX_PB23E_TC7_WO1, &AOUT_TC7, true, 1},  // [D11]
};

static float volts[ANALOG_OUT_COUNT];
static float full_scale_v = 3.3f; // The tester's VDD, which is 100% duty
static bool armed = false;

static void connect(uint32_t pinmux) {
    // Never true (the table holds real PINMUX_ values), but tells GCC that ASF's "function off" branch, which would
    // index port 255, can't be reached from here
    if (pinmux == GPIO_PIN_FUNCTION_OFF) {
        return;
    }
    gpio_set_pin_function(PINMUX_PIN(pinmux), pinmux);
}

// High impedance: no peripheral, no output, no input buffer
static void disconnect(uint32_t pinmux) {
    gpio_set_pin_function(PINMUX_PIN(pinmux), GPIO_PIN_FUNCTION_OFF);
    gpio_set_pin_direction(PINMUX_PIN(pinmux), GPIO_DIRECTION_OFF);
}

static uint32_t code_for(analog_out_t out) {
    float fraction = volts[out] / full_scale_v;
    if (fraction < 0.0f) {
        fraction = 0.0f;
    } else if (fraction > 1.0f) {
        fraction = 1.0f;
    }
    return (uint32_t)(fraction * (float)PWM_TOP + 0.5f);
}

static void apply(analog_out_t out) {
    const pwm_channel_t *const c = &PWM_CHANNELS[out];
    const uint32_t code = code_for(out);
    if (c->is_tc) {
        pwm_set_parameters((struct pwm_descriptor *)c->timer, PWM_TOP, code);
    } else {
        ((Tcc *)c->timer)->CCBUF[c->channel].reg = code;
    }
}

uint8_t analog_out_count(void) {
    return ANALOG_OUT_COUNT;
}

// After atmel_start_init(), which has configured the timers and connected their pins
void analog_out_init(void) {
    disconnect(PINMUX_PA02B_DAC_VOUT0); // [A0]
    disconnect(PINMUX_PA05B_DAC_VOUT1); // [A1]

    pwm_enable(&AOUT_TCC0);
    pwm_enable(&AOUT_TCC1);

    pwm_enable(&AOUT_TC6);
    pwm_enable(&AOUT_TC7);

    memset(volts, 0, sizeof(volts));
    for (analog_out_t out = 0; out < ANALOG_OUT_COUNT; out++) {
        disconnect(PWM_CHANNELS[out].pinmux);
        apply(out);
    }
}

void analog_out_arm(bool arm) {
    if (arm == armed) {
        return;
    }
    armed = arm;
    for (analog_out_t out = 0; out < ANALOG_OUT_COUNT; out++) {
        if (arm) {
            connect(PWM_CHANNELS[out].pinmux);
            apply(out);
        } else {
            disconnect(PWM_CHANNELS[out].pinmux);
        }
    }
}

bool analog_out_armed(void) {
    return armed;
}

void analog_out_set(uint8_t out, float v) {
    if (out >= ANALOG_OUT_COUNT) {
        return;
    }
    volts[out] = v;
    apply(out);
}

float analog_out_get(uint8_t out) {
    return out < ANALOG_OUT_COUNT ? volts[out] : 0.0f;
}

const char *analog_out_name(uint8_t out) {
    return out < ANALOG_OUT_COUNT ? PWM_CHANNELS[out].name : "?";
}

int analog_out_find(const char *name) {
    for (analog_out_t out = 0; out < ANALOG_OUT_COUNT; out++) {
        if (strcmp(name, PWM_CHANNELS[out].name) == 0) {
            return (int)out;
        }
    }
    return -1;
}

bool analog_out_set_full_scale(float v) {
    if (!(v > 2.5f && v < 3.6f)) {
        return false;
    }
    full_scale_v = v;
    for (analog_out_t out = 0; out < ANALOG_OUT_COUNT; out++) {
        apply(out);
    }
    return true;
}

float analog_out_full_scale(void) {
    return full_scale_v;
}
