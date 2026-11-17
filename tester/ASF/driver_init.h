/*
 * Code generated from Atmel Start.
 *
 * This file will be overwritten when reconfiguring your Atmel Start project.
 * Please copy examples or other code you want to keep to a separate file
 * to avoid losing it when reconfiguring.
 */
#ifndef DRIVER_INIT_INCLUDED
#define DRIVER_INIT_INCLUDED

#include "atmel_start_pins.h"

#ifdef __cplusplus
extern "C" {
#endif

#include <hal_atomic.h>
#include <hal_delay.h>
#include <hal_gpio.h>
#include <hal_init.h>
#include <hal_io.h>
#include <hal_sleep.h>

#include <hal_adc_sync.h>

#include <hal_spi_s_async.h>
#include <hal_pwm.h>
#include <hpl_tc_base.h>
#include <hal_pwm.h>
#include <hpl_tc_base.h>

#include <hal_pwm.h>
#include <hpl_tcc.h>

#include <hal_pwm.h>
#include <hpl_tcc.h>

extern struct adc_sync_descriptor ADC_1;

extern struct spi_s_async_descriptor SBAND_SPI;

extern struct pwm_descriptor AOUT_TC6;

extern struct pwm_descriptor AOUT_TC7;

extern struct pwm_descriptor AOUT_TCC0;

extern struct pwm_descriptor AOUT_TCC1;

void ADC_1_PORT_init(void);
void ADC_1_CLOCK_init(void);
void ADC_1_init(void);

void SBAND_SPI_PORT_init(void);
void SBAND_SPI_CLOCK_init(void);
void SBAND_SPI_init(void);

void AOUT_TC6_PORT_init(void);
void AOUT_TC6_CLOCK_init(void);
void AOUT_TC6_init(void);

void AOUT_TC7_PORT_init(void);
void AOUT_TC7_CLOCK_init(void);
void AOUT_TC7_init(void);

void AOUT_TCC0_PORT_init(void);
void AOUT_TCC0_CLOCK_init(void);
void AOUT_TCC0_init(void);

void AOUT_TCC1_PORT_init(void);
void AOUT_TCC1_CLOCK_init(void);
void AOUT_TCC1_init(void);

/**
 * \brief Perform system initialization, initialize pins and clocks for
 * peripherals
 */
void system_init(void);

#ifdef __cplusplus
}
#endif
#endif // DRIVER_INIT_INCLUDED
