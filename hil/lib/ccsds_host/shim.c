/**
 * shim.c
 *
 * What PVDXos's CCSDS code calls outside itself, for the native host build that hil/lib/ccsds.py loads: logging goes
 * to stderr, and the single-threaded host needs no real mutex or clock.
 */

#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>

#include "FreeRTOS.h"
#include "task.h"

static int quiet = 1;

void ccsds_host_set_verbose(int verbose) {
    quiet = !verbose;
}

static void vlog(const char *string, va_list args) {
    if (!quiet) {
        vfprintf(stderr, string, args);
    }
}

void fatal_impl(const char *string, ...) {
    va_list args;
    va_start(args, string);
    vfprintf(stderr, string, args);
    va_end(args);
    abort();
}

void fatal_no_log_impl(void) {
    abort();
}

#define LOG_IMPL(name, type)                                                                                           \
    void name(type string, ...) {                                                                                      \
        va_list args;                                                                                                  \
        va_start(args, string);                                                                                        \
        vlog(string, args);                                                                                            \
        va_end(args);                                                                                                  \
    }
LOG_IMPL(warning_impl, const char *)
LOG_IMPL(event_impl, const char *)
LOG_IMPL(info_impl, char *)
LOG_IMPL(debug_impl, const char *)

// telemetry_collect() (never called on the host) stamps the downlink with these; weak, so a build whose PVDXos has
// no utc.c still links
__attribute__((weak)) int utc_get(uint32_t *unix_time) {
    (void)unix_time;
    return -1;
}

__attribute__((weak)) int utc_get_boot_time(uint32_t *unix_time) {
    (void)unix_time;
    return -1;
}

TickType_t xTaskGetTickCount(void) {
    return 0;
}

BaseType_t xTaskGetSchedulerState(void) {
    return taskSCHEDULER_NOT_STARTED;
}

SemaphoreHandle_t xSemaphoreCreateMutexStatic(StaticSemaphore_t *buffer) {
    return buffer;
}
