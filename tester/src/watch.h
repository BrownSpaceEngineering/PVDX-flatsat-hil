#ifndef WATCH_H
#define WATCH_H

void watch_init(void);
void watch_poll(void); // Sends recorded edges to the host; call from the main loop

#endif // WATCH_H
