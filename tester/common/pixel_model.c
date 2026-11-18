/**
 * pixel_model.c
 *
 * Perovskite pixels as ideal single-diode cells: I(V) = Isc - I0 (exp(V / (n Vt)) - 1), with I0 chosen so that I(Voc) = 0.
 * Positive current is the pixel sourcing current, as src/drivers/pmb/ina226.h counts it. On the board every routed pixel
 * is on PIXEL+, which sits at the follower voltage plus the 10 ohm shunt drop of their summed current,
 * V = V_follow + I * 10 ohm. The sum falls as I rises, so bisection on I always finds the one solution.
 */

#include "pixel_model.h"

#include <math.h>

#define IDEALITY 1.5f
#define THERMAL_VOLTAGE_V 0.025852f
#define SHUNT_OHMS 10.0f
#define CURRENT_LIMIT_A 0.008f // The INA226 shunt register's range at 10 ohm (+-81.9 mV)
#define BISECTION_STEPS 32     // 16 mA / 2^32: far below the 250 nA the flatsat resolves

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

// Summed current of the pixels in mask with PIXEL+ at v
static float total_at(uint16_t mask, float v) {
    float total = 0.0f;
    for (uint8_t pixel = 0; pixel < PIXEL_COUNT; pixel++) {
        if (mask & (1u << pixel)) {
            total += current_at(pixel, v);
        }
    }
    return total;
}

float pixels_current_a(uint16_t mask, float follow_v) {
    // Solves I = total_at(V_follow + I * R). I - total_at(...) rises with I; outside the range the current is clamped
    float lo = -CURRENT_LIMIT_A, hi = CURRENT_LIMIT_A;
    if (lo - total_at(mask, follow_v + lo * SHUNT_OHMS) >= 0.0f) {
        return lo;
    }
    if (hi - total_at(mask, follow_v + hi * SHUNT_OHMS) <= 0.0f) {
        return hi;
    }
    for (int step = 0; step < BISECTION_STEPS; step++) {
        const float mid = 0.5f * (lo + hi);
        if (mid - total_at(mask, follow_v + mid * SHUNT_OHMS) < 0.0f) {
            lo = mid;
        } else {
            hi = mid;
        }
    }
    return 0.5f * (lo + hi);
}

float pixel_current_a(uint8_t pixel, float follow_v) {
    return pixel < PIXEL_COUNT ? pixels_current_a((uint16_t)(1u << pixel), follow_v) : 0.0f;
}
