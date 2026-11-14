#ifndef BUSES_H
#define BUSES_H

void buses_init(void);
void buses_poll(void); // Measures PVD_CRNT for the perovskite board; call from the main loop

#endif // BUSES_H
