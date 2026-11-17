/*
 * Code generated from Atmel Start.
 *
 * This file will be overwritten when reconfiguring your Atmel Start project.
 * Please copy examples or other code you want to keep to a separate file
 * to avoid losing it when reconfiguring.
 */
#ifndef ATMEL_START_PINS_H_INCLUDED
#define ATMEL_START_PINS_H_INCLUDED

#include <hal_gpio.h>

// SAMD51 has 14 pin functions

#define GPIO_PIN_FUNCTION_A 0
#define GPIO_PIN_FUNCTION_B 1
#define GPIO_PIN_FUNCTION_C 2
#define GPIO_PIN_FUNCTION_D 3
#define GPIO_PIN_FUNCTION_E 4
#define GPIO_PIN_FUNCTION_F 5
#define GPIO_PIN_FUNCTION_G 6
#define GPIO_PIN_FUNCTION_H 7
#define GPIO_PIN_FUNCTION_I 8
#define GPIO_PIN_FUNCTION_J 9
#define GPIO_PIN_FUNCTION_K 10
#define GPIO_PIN_FUNCTION_L 11
#define GPIO_PIN_FUNCTION_M 12
#define GPIO_PIN_FUNCTION_N 13

#define THERM_0 GPIO(GPIO_PORTA, 18)
#define THERM_1 GPIO(GPIO_PORTA, 19)
#define LED GPIO(GPIO_PORTB, 1)
#define PVD_SENSE GPIO(GPIO_PORTB, 4)
#define THERM_2 GPIO(GPIO_PORTB, 17)
#define THERM_3 GPIO(GPIO_PORTB, 23)
#define PHD_0 GPIO(GPIO_PORTC, 10)
#define PHD_1 GPIO(GPIO_PORTC, 11)
#define PHD_2 GPIO(GPIO_PORTC, 12)
#define PHD_3 GPIO(GPIO_PORTC, 13)
#define PVD_0 GPIO(GPIO_PORTC, 14)
#define PVD_1 GPIO(GPIO_PORTC, 15)
#define PD08 GPIO(GPIO_PORTD, 8)
#define PD09 GPIO(GPIO_PORTD, 9)
#define PD10 GPIO(GPIO_PORTD, 10)
#define PD11 GPIO(GPIO_PORTD, 11)
#define PVD_2 GPIO(GPIO_PORTD, 20)
#define PVD_3 GPIO(GPIO_PORTD, 21)

#endif // ATMEL_START_PINS_H_INCLUDED
