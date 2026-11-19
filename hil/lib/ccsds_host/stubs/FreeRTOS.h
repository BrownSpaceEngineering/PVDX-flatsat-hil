/* Host stand-in for FreeRTOS, so PVDXos's CCSDS code builds natively (hil/lib/ccsds.py). Types only. */
#ifndef HOST_FREERTOS_H
#define HOST_FREERTOS_H
#include <stddef.h>
#include <stdint.h>
typedef uint32_t TickType_t;
typedef uint32_t StackType_t;
typedef long BaseType_t;
typedef unsigned long UBaseType_t;
typedef void *QueueHandle_t;
typedef void *SemaphoreHandle_t;
typedef void *TaskHandle_t;
typedef void *TimerHandle_t;
typedef void *EventGroupHandle_t;
typedef struct { uint8_t opaque[96]; } StaticQueue_t;
typedef StaticQueue_t StaticSemaphore_t;
typedef struct { uint8_t opaque[128]; } StaticTask_t;
typedef struct { uint8_t opaque[64]; } StaticTimer_t;
typedef struct { uint8_t opaque[64]; } StaticEventGroup_t;
typedef void (*TaskFunction_t)(void *);
#define pdTRUE 1
#define pdFALSE 0
#define pdPASS 1
#define pdFAIL 0
#define portMAX_DELAY 0xFFFFFFFFu
#define pdMS_TO_TICKS(ms) ((TickType_t)(ms))
#define configTICK_RATE_HZ 1000
#define configMAX_PRIORITIES 8
#define configMINIMAL_STACK_SIZE 128
SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t *buffer);
#endif
