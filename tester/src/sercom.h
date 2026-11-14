#ifndef SERCOM_H
#define SERCOM_H

#include <stdint.h>

#include "board.h"

uint8_t sercom_index(Sercom *sercom);
// Turns on the SERCOM's bus and core clocks (48 MHz) and routes its interrupts to handler(arg); NULL disables them
void sercom_enable(Sercom *sercom, void (*handler)(void *), void *arg, uint8_t priority);

#endif // SERCOM_H
