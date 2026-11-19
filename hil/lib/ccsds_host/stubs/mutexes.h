/* Host stand-in for src/mutexes/mutexes.h: the host build is single-threaded. */
#ifndef HOST_MUTEXES_H
#define HOST_MUTEXES_H
#include "FreeRTOS.h"
#include "logging.h"
#define lock_mutex(m) ((void)(m))
#define unlock_mutex(m) ((void)(m))
#endif
