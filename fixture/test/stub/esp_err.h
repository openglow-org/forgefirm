/*
 * Host stand-in for ESP-IDF's esp_err.h: what relays.c and the host test
 * of it need, nothing more (the values are the IDF's own).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdlib.h>

typedef int esp_err_t;

#define ESP_OK                0
#define ESP_ERR_INVALID_ARG   0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NOT_ALLOWED   0x10D

#define ESP_ERROR_CHECK(x) do { esp_err_t err_ = (x); if (err_ != ESP_OK) abort(); } while (0)
