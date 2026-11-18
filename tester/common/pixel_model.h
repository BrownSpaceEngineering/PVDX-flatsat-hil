#ifndef PIXEL_MODEL_H
#define PIXEL_MODEL_H

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PIXEL_COUNT 16u

void pixel_set(uint8_t pixel, float isc_ua, float voc_mv); // Isc or Voc of 0 makes the pixel dark (no current)
void pixel_get(uint8_t pixel, float *isc_ua, float *voc_mv);
float pixel_current_a(uint8_t pixel, float follow_v);     // Current with the follower at follow_v
float pixels_current_a(uint16_t mask, float follow_v);    // Summed current of the pixels in mask, all routed at once

#ifdef __cplusplus
}
#endif

#endif // PIXEL_MODEL_H
