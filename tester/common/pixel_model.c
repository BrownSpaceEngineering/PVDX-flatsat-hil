/**
 * pixel_model.c
 *
 * Perovskite pixels as ideal single-diode cells: I(V) = Isc - I0 (exp(V / (n Vt)) - 1), with I0 chosen so that I(Voc) = 0.
 * Positive current is the pixel sourcing current, as src/drivers/pmb/ina226.h counts it. On the board the pixel sits at
 * the follower voltage plus the 10 ohm shunt drop, V = V_follow + I * 10 ohm, which a few fixed-point steps solve.
 */

#include "pixel_model.h"

#include <math.h>

#define IDEALITY 1.5f
#define THERMAL_VOLTAGE_V 0.025852f
#define SHUNT_OHMS 10.0f
#define CURRENT_LIMIT_A 0.008f // The INA226 shunt register's range at 10 ohm (+-81.9 mV)

static float isc_a[PIXEL_COUNT];
static float voc_v[PIXEL_COUNT];

void pixel_set(uint8_t pixel, float isc_ua, float voc_mv) {
    if (pixel < PIXEL_COUNT) {
        isc_a[pixel] = isc_ua * 1e-6f;
        voc_v[pixel] = voc_mv * 1e-3f;
    }
}

void pixel_get(uint8_t pixel, float *isc_ua, float *voc_mv) {
    *isc_ua = pixel < PIXEL_COUNT ? isc_a[pixel] * 1e6f : 0.0f;
    *voc_mv = pixel < PIXEL_COUNT ? voc_v[pixel] * 1e3f : 0.0f;
}

static float current_at(uint8_t pixel, float v) {
    if (isc_a[pixel] <= 0.0f || voc_v[pixel] <= 0.0f) {
        return 0.0f; // Dark or not set: no photocurrent, and no forward conduction modelled
    }
    const float nvt = IDEALITY * THERMAL_VOLTAGE_V;
    const float i0 = isc_a[pixel] / (expf(voc_v[pixel] / nvt) - 1.0f);
    float i = isc_a[pixel] - i0 * (expf(v / nvt) - 1.0f);
    if (i > CURRENT_LIMIT_A) {
        i = CURRENT_LIMIT_A;
    } else if (i < -CURRENT_LIMIT_A) {
        i = -CURRENT_LIMIT_A;
    }
    return i;
}

float pixel_current_a(uint8_t pixel, float follow_v) {
    if (pixel >= PIXEL_COUNT) {
        return 0.0f;
    }
    float i = current_at(pixel, follow_v);
    for (int step = 0; step < 4; step++) {
        i = current_at(pixel, follow_v + i * SHUNT_OHMS);
    }
    return i;
}
