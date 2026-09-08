/*
 * forgefixture relays: the three channels on their GPIOs, the button's
 * pulse timer, and the enable jumper that gates the button.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdbool.h>

#include "esp_err.h"
#include "policy.h"

/* DevKitC-1 pins with no strapping, USB, flash or PSRAM role. Active
 * high into the relay modules' opto inputs. */
#define RELAY_GPIO_LID       4
#define RELAY_GPIO_INTERLOCK 5
#define RELAY_GPIO_BUTTON    6
/* The enable jumper for the button channel: input with the pull-up on,
 * the jumper shorts it to GND. No jumper = high = button disabled. */
#define ENABLE_GPIO_BUTTON   7

typedef struct {
    bool energized[CH_COUNT];
    bool button_enabled;        /* the jumper is in */
    bool button_pulsing;        /* a press is in progress */
} relays_state_t;

/* Every line low before anything else runs. */
void relays_init(void);

/* Lid or interlock: hold the channel energized (loop open) or released
 * (loop closed). ESP_ERR_INVALID_ARG for the button, which is never
 * held. */
esp_err_t relays_set_loop(channel_t ch, bool energize);

/* The button: one pulse of `ms` (already clamped by policy_button_ms).
 * ESP_ERR_NOT_ALLOWED without the enable jumper, ESP_ERR_INVALID_STATE
 * while a pulse is still in progress. */
esp_err_t relays_pulse_button(int ms);

/* Everything low, a pulse in progress cut short. */
void relays_release(void);

relays_state_t relays_state(void);
