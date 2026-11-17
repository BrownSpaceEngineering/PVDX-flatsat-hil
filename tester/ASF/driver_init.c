/*
 * Code generated from Atmel Start.
 *
 * This file will be overwritten when reconfiguring your Atmel Start project.
 * Please copy examples or other code you want to keep to a separate file
 * to avoid losing it when reconfiguring.
 */

#include "driver_init.h"
#include <peripheral_clk_config.h>
#include <utils.h>
#include <hal_init.h>

#include <hpl_adc_base.h>

struct adc_sync_descriptor ADC_1;

struct spi_s_async_descriptor SBAND_SPI;
static uint16_t               SBAND_SPI_buf[16];

struct pwm_descriptor AOUT_TC6;

struct pwm_descriptor AOUT_TC7;

struct pwm_descriptor AOUT_TCC0;

struct pwm_descriptor AOUT_TCC1;

void ADC_1_PORT_init(void)
{

	// Disable digital pin circuitry
	gpio_set_pin_direction(PVD_SENSE, GPIO_DIRECTION_OFF);

	gpio_set_pin_function(PVD_SENSE, PINMUX_PB04B_ADC1_AIN6);
}

void ADC_1_CLOCK_init(void)
{
	hri_mclk_set_APBDMASK_ADC1_bit(MCLK);
	hri_gclk_write_PCHCTRL_reg(GCLK, ADC1_GCLK_ID, CONF_GCLK_ADC1_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));
}

void ADC_1_init(void)
{
	ADC_1_CLOCK_init();
	ADC_1_PORT_init();
	adc_sync_init(&ADC_1, ADC1, (void *)NULL);
}

void SBAND_SPI_PORT_init(void)
{

	gpio_set_pin_level(PD08,
	                   // <y> Initial level
	                   // <id> pad_initial_level
	                   // <false"> Low
	                   // <true"> High
	                   false);

	// Set pin direction to output
	gpio_set_pin_direction(PD08, GPIO_DIRECTION_OUT);

	gpio_set_pin_function(PD08, PINMUX_PD08C_SERCOM7_PAD0);

	gpio_set_pin_level(PD09,
	                   // <y> Initial level
	                   // <id> pad_initial_level
	                   // <false"> Low
	                   // <true"> High
	                   false);

	// Set pin direction to output
	gpio_set_pin_direction(PD09, GPIO_DIRECTION_OUT);

	gpio_set_pin_function(PD09, PINMUX_PD09C_SERCOM7_PAD1);

	// Set pin direction to input
	gpio_set_pin_direction(PD10, GPIO_DIRECTION_IN);

	gpio_set_pin_pull_mode(PD10,
	                       // <y> Pull configuration
	                       // <id> pad_pull_config
	                       // <GPIO_PULL_OFF"> Off
	                       // <GPIO_PULL_UP"> Pull-up
	                       // <GPIO_PULL_DOWN"> Pull-down
	                       GPIO_PULL_OFF);

	gpio_set_pin_function(PD10, PINMUX_PD10C_SERCOM7_PAD2);

	// Set pin direction to input
	gpio_set_pin_direction(PD11, GPIO_DIRECTION_IN);

	gpio_set_pin_pull_mode(PD11,
	                       // <y> Pull configuration
	                       // <id> pad_pull_config
	                       // <GPIO_PULL_OFF"> Off
	                       // <GPIO_PULL_UP"> Pull-up
	                       // <GPIO_PULL_DOWN"> Pull-down
	                       GPIO_PULL_OFF);

	gpio_set_pin_function(PD11, PINMUX_PD11C_SERCOM7_PAD3);
}

void SBAND_SPI_CLOCK_init(void)
{
	hri_gclk_write_PCHCTRL_reg(GCLK, SERCOM7_GCLK_ID_CORE, CONF_GCLK_SERCOM7_CORE_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));
	hri_gclk_write_PCHCTRL_reg(GCLK, SERCOM7_GCLK_ID_SLOW, CONF_GCLK_SERCOM7_SLOW_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));

	hri_mclk_set_APBDMASK_SERCOM7_bit(MCLK);
}

void SBAND_SPI_init(void)
{
	SBAND_SPI_CLOCK_init();
	spi_s_async_init(&SBAND_SPI, SERCOM7, (uint8_t *)SBAND_SPI_buf, 32);
	SBAND_SPI_PORT_init();
}

void AOUT_TC6_PORT_init(void)
{

	gpio_set_pin_function(THERM_2, PINMUX_PB17E_TC6_WO1);
}

void AOUT_TC6_CLOCK_init(void)
{

	hri_mclk_set_APBDMASK_TC6_bit(MCLK);
	hri_gclk_write_PCHCTRL_reg(GCLK, TC6_GCLK_ID, CONF_GCLK_TC6_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));
}

void AOUT_TC6_init(void)
{
	AOUT_TC6_CLOCK_init();
	AOUT_TC6_PORT_init();
	pwm_init(&AOUT_TC6, TC6, _tc_get_pwm());
}

void AOUT_TC7_PORT_init(void)
{

	gpio_set_pin_function(THERM_3, PINMUX_PB23E_TC7_WO1);
}

void AOUT_TC7_CLOCK_init(void)
{

	hri_mclk_set_APBDMASK_TC7_bit(MCLK);
	hri_gclk_write_PCHCTRL_reg(GCLK, TC7_GCLK_ID, CONF_GCLK_TC7_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));
}

void AOUT_TC7_init(void)
{
	AOUT_TC7_CLOCK_init();
	AOUT_TC7_PORT_init();
	pwm_init(&AOUT_TC7, TC7, _tc_get_pwm());
}

void AOUT_TCC0_PORT_init(void)
{

	gpio_set_pin_function(PHD_0, PINMUX_PC10F_TCC0_WO0);

	gpio_set_pin_function(PHD_1, PINMUX_PC11F_TCC0_WO1);

	gpio_set_pin_function(PHD_2, PINMUX_PC12F_TCC0_WO2);

	gpio_set_pin_function(PHD_3, PINMUX_PC13F_TCC0_WO3);

	gpio_set_pin_function(PVD_0, PINMUX_PC14F_TCC0_WO4);

	gpio_set_pin_function(PVD_1, PINMUX_PC15F_TCC0_WO5);
}

void AOUT_TCC0_CLOCK_init(void)
{

	hri_mclk_set_APBBMASK_TCC0_bit(MCLK);
	hri_gclk_write_PCHCTRL_reg(GCLK, TCC0_GCLK_ID, CONF_GCLK_TCC0_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));
}

void AOUT_TCC0_init(void)
{
	AOUT_TCC0_CLOCK_init();
	AOUT_TCC0_PORT_init();
	pwm_init(&AOUT_TCC0, TCC0, _tcc_get_pwm());
}

void AOUT_TCC1_PORT_init(void)
{

	gpio_set_pin_function(PVD_2, PINMUX_PD20F_TCC1_WO0);

	gpio_set_pin_function(PVD_3, PINMUX_PD21F_TCC1_WO1);

	gpio_set_pin_function(THERM_0, PINMUX_PA18F_TCC1_WO2);

	gpio_set_pin_function(THERM_1, PINMUX_PA19F_TCC1_WO3);
}

void AOUT_TCC1_CLOCK_init(void)
{

	hri_mclk_set_APBBMASK_TCC1_bit(MCLK);
	hri_gclk_write_PCHCTRL_reg(GCLK, TCC1_GCLK_ID, CONF_GCLK_TCC1_SRC | (1 << GCLK_PCHCTRL_CHEN_Pos));
}

void AOUT_TCC1_init(void)
{
	AOUT_TCC1_CLOCK_init();
	AOUT_TCC1_PORT_init();
	pwm_init(&AOUT_TCC1, TCC1, _tcc_get_pwm());
}

void system_init(void)
{
	init_mcu();

	// GPIO on PB01

	gpio_set_pin_level(LED,
	                   // <y> Initial level
	                   // <id> pad_initial_level
	                   // <false"> Low
	                   // <true"> High
	                   false);

	// Set pin direction to output
	gpio_set_pin_direction(LED, GPIO_DIRECTION_OUT);

	gpio_set_pin_function(LED, GPIO_PIN_FUNCTION_OFF);

	ADC_1_init();

	SBAND_SPI_init();

	AOUT_TC6_init();

	AOUT_TC7_init();

	AOUT_TCC0_init();

	AOUT_TCC1_init();
}
