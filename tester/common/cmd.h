#ifndef CMD_H
#define CMD_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stddef.h>
#include <stdint.h>

void cmd_poll(void);                // Reads and runs any complete command lines from the host
uint32_t cmd_last_command_ms(void); // platform_millis() when the last command arrived (0 = never)

// snprintf() at buf + n for a reply built up piece by piece: returns n plus the length it wanted to add, as snprintf()
// does, but never writes past buf[len - 1], even once n has passed len (the caller sees n >= len and reports it)
int cmd_append(char *buf, size_t len, int n, const char *format, ...) __attribute__((format(printf, 4, 5)));
size_t cmd_room(size_t len, int n); // What is left of len after n, for a function that writes its own members

#ifdef __cplusplus
}
#endif

#endif // CMD_H
