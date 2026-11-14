#ifndef MCP23017_MODEL_H
#define MCP23017_MODEL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    MCP23017_FAULT_NONE = 0,
    MCP23017_FAULT_NACK,  // Never answers its address
    MCP23017_FAULT_STUCK, // The output latch ignores writes
} mcp23017_fault_t;

// Called by pmb_model.c from the I2C interrupt
bool mcp23017_start(bool read);
void mcp23017_write(uint8_t byte);
uint8_t mcp23017_read(void);
void mcp23017_end(void);

void mcp23017_reset(void); // Power-on registers, statistics cleared (keeps the fault)
void mcp23017_set_fault(mcp23017_fault_t fault);
uint16_t mcp23017_selected(void); // Output pins driven high: bit N = pixel N routed to measurement
int mcp23017_json(char *buf, size_t len);

#ifdef __cplusplus
}
#endif

#endif // MCP23017_MODEL_H
