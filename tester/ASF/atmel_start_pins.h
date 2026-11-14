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

#define THERM_0 GPIO(GPIO_PORTA, 2)
#define THERM_1 GPIO(GPIO_PORTA, 5)
#define SELFTEST_MISO GPIO(GPIO_PORTA, 6)
#define PHD_17 GPIO(GPIO_PORTA, 13)
#define PHD_10 GPIO(GPIO_PORTA, 14)
#define PHD_11 GPIO(GPIO_PORTA, 15)
#define SELFTEST_SDA GPIO(GPIO_PORTA, 16)
#define SELFTEST_SCL GPIO(GPIO_PORTA, 17)
#define PHD_8 GPIO(GPIO_PORTA, 18)
#define PHD_9 GPIO(GPIO_PORTA, 19)
#define SELFTEST_CS GPIO(GPIO_PORTB, 0)
#define LED GPIO(GPIO_PORTB, 1)
#define PHD_12 GPIO(GPIO_PORTB, 2)
#define PVD_SENSE GPIO(GPIO_PORTB, 4)
#define PHD_13 GPIO(GPIO_PORTB, 12)
#define PHD_14 GPIO(GPIO_PORTB, 13)
#define PHD_15 GPIO(GPIO_PORTB, 14)
#define PHD_16 GPIO(GPIO_PORTB, 15)
#define THERM_2 GPIO(GPIO_PORTB, 17)
#define THERM_3 GPIO(GPIO_PORTB, 23)
#define SELFTEST_MOSI GPIO(GPIO_PORTB, 24)
#define SELFTEST_SCK GPIO(GPIO_PORTB, 25)
#define PHD_0 GPIO(GPIO_PORTC, 10)
#define PHD_1 GPIO(GPIO_PORTC, 11)
#define PHD_2 GPIO(GPIO_PORTC, 12)
#define PHD_3 GPIO(GPIO_PORTC, 13)
#define PHD_4 GPIO(GPIO_PORTC, 14)
#define PHD_5 GPIO(GPIO_PORTC, 15)
#define PD08 GPIO(GPIO_PORTD, 8)
#define PD09 GPIO(GPIO_PORTD, 9)
#define PD10 GPIO(GPIO_PORTD, 10)
#define PD11 GPIO(GPIO_PORTD, 11)
#define PHD_6 GPIO(GPIO_PORTD, 20)
#define PHD_7 GPIO(GPIO_PORTD, 21)

#endif // ATMEL_START_PINS_H_INCLUDED
