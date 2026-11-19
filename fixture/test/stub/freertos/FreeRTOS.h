/*
 * Host stand-in for FreeRTOS.h: the types and the constant relays.c uses.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdint.h>

typedef int BaseType_t;
typedef uint32_t TickType_t;

#define pdTRUE        1
#define portMAX_DELAY ((TickType_t)0xffffffffu)
