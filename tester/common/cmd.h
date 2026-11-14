#ifndef CMD_H
#define CMD_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdint.h>

void cmd_poll(void);                // Reads and runs any complete command lines from the host
uint32_t cmd_last_command_ms(void); // platform_millis() when the last command arrived (0 = never)

#ifdef __cplusplus
}
#endif

#endif // CMD_H
