#ifndef ANALOG_IN_H
#define ANALOG_IN_H

void analog_in_init(void);
float analog_in_read_pvd_crnt(float full_scale_v); // PVD_CRNT on PB04 [A7] (ASF ADC_1)

#endif // ANALOG_IN_H
