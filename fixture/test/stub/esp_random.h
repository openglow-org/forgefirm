/*
 * Host stand-in for ESP-IDF's esp_random.h, defined by the host test.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stddef.h>

void esp_fill_random(void *buf, size_t len);
