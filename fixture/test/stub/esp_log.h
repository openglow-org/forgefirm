/*
 * Host stand-in for ESP-IDF's esp_log.h: the log calls take their
 * arguments and print nothing.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

static inline void esp_log_stub(const char *tag, const char *fmt, ...)
{
    (void)tag;
    (void)fmt;
}

#define ESP_LOGI(tag, ...) esp_log_stub(tag, __VA_ARGS__)
#define ESP_LOGW(tag, ...) esp_log_stub(tag, __VA_ARGS__)
#define ESP_LOGE(tag, ...) esp_log_stub(tag, __VA_ARGS__)
