/* Host stand-in for FreeRTOS task.h: the host build is single-threaded, so critical sections do nothing. */
#ifndef HOST_TASK_H
#define HOST_TASK_H
#include "FreeRTOS.h"
#define taskENTER_CRITICAL() ((void)0)
#define taskEXIT_CRITICAL() ((void)0)
#define taskSCHEDULER_NOT_STARTED 1
TickType_t xTaskGetTickCount(void);
BaseType_t xTaskGetSchedulerState(void);
#endif
