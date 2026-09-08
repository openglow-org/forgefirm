/*
 * forgefixture policy: the decisions that need no hardware, kept apart
 * so the host test can hold them to account. What a channel is called,
 * what a request may ask of it, and how long a button press may last.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

/* The three channels, in the order of the GPIO table in relays.c. */
typedef enum {
    CH_LID = 0,          /* NC contact in the lid-switch loop: energized = loop open */
    CH_INTERLOCK = 1,    /* NC contact in the interlock loop: energized = loop open */
    CH_BUTTON = 2,       /* NO contact across the button input: energized = pressed */
    CH_COUNT = 3
} channel_t;

/* A button press is a pulse and nothing else: never held, never longer
 * than BUTTON_MAX_MS whatever the request says, never shorter than the
 * debounce the machine's input needs to see it. */
#define BUTTON_DEFAULT_MS 200
#define BUTTON_MIN_MS     20
#define BUTTON_MAX_MS     500

/* The channel's name on the API and in the log. */
const char *policy_channel_name(channel_t ch);

/* The channel named by an API path ("/lid", "/interlock", "/button"), or
 * -1. */
int policy_channel_from_path(const char *path);

/* The level a lid or interlock request asks for: "open" energizes the
 * channel (the loop opens), "close" or "closed" releases it. Returns
 * false for any other word. */
bool policy_parse_loop_state(const char *word, bool *energize);

/* The pulse a button request gets: the default for 0 or an absent
 * value, otherwise the request clamped into [BUTTON_MIN_MS,
 * BUTTON_MAX_MS]. A negative request is the default too. */
int policy_button_ms(int requested);

/* What the state of a channel is called on the API: a loop channel is
 * "open" or "closed", the button "pressed" or "idle". */
const char *policy_state_name(channel_t ch, bool energized);

/* Constant-time equality of two keys, so a wrong key costs the same
 * whichever byte is wrong. */
bool policy_key_matches(const char *presented, const char *expected);
