/*
 * Host stand-in for ESP-IDF's driver/gpio.h: the calls relays.c makes,
 * defined by the host test, which records them in order.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdint.h>

#include "esp_err.h"

typedef int gpio_num_t;

typedef enum { GPIO_MODE_INPUT = 1, GPIO_MODE_OUTPUT = 2 } gpio_mode_t;
typedef enum { GPIO_PULLUP_ONLY = 0 } gpio_pull_mode_t;

esp_err_t gpio_reset_pin(gpio_num_t gpio);
esp_err_t gpio_set_level(gpio_num_t gpio, uint32_t level);
int gpio_get_level(gpio_num_t gpio);
esp_err_t gpio_set_direction(gpio_num_t gpio, gpio_mode_t mode);
esp_err_t gpio_set_pull_mode(gpio_num_t gpio, gpio_pull_mode_t pull);
