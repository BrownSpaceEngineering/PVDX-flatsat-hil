/**
 * analog_in.c
 *
 * PVD_CRNT, the flatsat's perovskite DAC output, on ASF ADC_1 (Atmel START project: AIN6 on PB04 [A7], VDDANA
 * reference like the flatsat's ADCs, 64 samples averaged in hardware to a 12-bit mean, about 0.5 ms).
 */

#include "analog_in.h"

#include "atmel_start.h"

void analog_in_init(void) {
    adc_sync_enable_channel(&ADC_1, 0);
}

float analog_in_read_pvd_crnt(float full_scale_v) {
    uint8_t result[2] = {0, 0};
    adc_sync_read_channel(&ADC_1, 0, result, sizeof(result));
    return (float)(result[0] | (result[1] << 8)) * full_scale_v / 4095.0f;
}
