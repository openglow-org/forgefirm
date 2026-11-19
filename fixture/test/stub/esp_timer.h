/*
 * Host stand-in for ESP-IDF's esp_timer.h: the one-shot timer relays.c
 * ends a button pulse with, defined by the host test, which records the
 * calls in order and fires the callback itself.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdint.h>

#include "esp_err.h"

typedef void (*esp_timer_cb_t)(void *arg);
typedef struct esp_timer *esp_timer_handle_t;

typedef struct {
    esp_timer_cb_t callback;
    void *arg;
    const char *name;
} esp_timer_create_args_t;

esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *out);
esp_err_t esp_timer_start_once(esp_timer_handle_t timer, uint64_t timeout_us);
esp_err_t esp_timer_stop(esp_timer_handle_t timer);
