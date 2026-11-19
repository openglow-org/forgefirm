/*
 * Host stand-in for ESP-IDF's esp_idf_version.h.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

static inline const char *esp_get_idf_version(void)
{
    return "host";
}
