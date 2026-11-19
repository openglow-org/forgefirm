/*
 * Host stand-in for FreeRTOS's task.h: the stack high-water mark api.c
 * reports, defined by the host test.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

typedef void *TaskHandle_t;
typedef unsigned UBaseType_t;

UBaseType_t uxTaskGetStackHighWaterMark(TaskHandle_t task);
