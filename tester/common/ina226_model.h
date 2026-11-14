#ifndef INA226_MODEL_H
#define INA226_MODEL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    INA226_FAULT_NONE = 0,
    INA226_FAULT_NACK,    // Never answers its address
    INA226_FAULT_NO_CVRF, // Conversions never report ready
    INA226_FAULT_BAD_ID,  // Wrong manufacturer ID
} ina226_fault_t;

// One CURRENT register read by the flatsat, with what the model was given at the time
typedef struct {
    uint16_t selected;  // MCP23017 outputs high (pixel bit mask)
    int16_t follow_mv;  // Follower voltage, from PVD_CRNT x 21/33
    int32_t current_na; // The value served, at the flatsat's 250 nA/LSB
} ina226_sample_t;

// Called by pmb_model.c from the I2C interrupt
bool ina226_start(bool read);
void ina226_write(uint8_t byte);
uint8_t ina226_read(void);
void ina226_end(bool was_read);

void ina226_poll(void); // Completes conversions that are due; call often
void ina226_set_inputs(float follow_v, uint16_t selected);
void ina226_reset(void); // Power-on registers, statistics and log cleared (keeps the fault)
void ina226_set_fault(ina226_fault_t fault);
uint32_t ina226_log_count(void);
bool ina226_log_entry(uint32_t index, ina226_sample_t *sample); // False if overwritten or not yet written
int ina226_json(char *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif // INA226_MODEL_H
