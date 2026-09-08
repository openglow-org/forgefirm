/*
 * forgefixture policy (see policy.h). No ESP-IDF here: test/policy_test.c
 * compiles this file with a host compiler.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "policy.h"

#include <string.h>

static const char *const NAMES[CH_COUNT] = {"lid", "interlock", "button"};

const char *policy_channel_name(channel_t ch)
{
    return (ch >= 0 && ch < CH_COUNT) ? NAMES[ch] : "?";
}

int policy_channel_from_path(const char *path)
{
    if (path == NULL || path[0] != '/')
        return -1;
    for (int i = 0; i < CH_COUNT; i++)
        if (strcmp(path + 1, NAMES[i]) == 0)
            return i;
    return -1;
}

bool policy_parse_loop_state(const char *word, bool *energize)
{
    if (word == NULL)
        return false;
    if (strcmp(word, "open") == 0) {
        *energize = true;
        return true;
    }
    if (strcmp(word, "close") == 0 || strcmp(word, "closed") == 0) {
        *energize = false;
        return true;
    }
    return false;
}

int policy_button_ms(int requested)
{
    if (requested <= 0)
        return BUTTON_DEFAULT_MS;
    if (requested < BUTTON_MIN_MS)
        return BUTTON_MIN_MS;
    if (requested > BUTTON_MAX_MS)
        return BUTTON_MAX_MS;
    return requested;
}

const char *policy_state_name(channel_t ch, bool energized)
{
    if (ch == CH_BUTTON)
        return energized ? "pressed" : "idle";
    return energized ? "open" : "closed";
}

bool policy_key_matches(const char *presented, const char *expected)
{
    if (presented == NULL || expected == NULL)
        return false;
    size_t lp = strlen(presented), le = strlen(expected);
    size_t n = lp > le ? lp : le;
    unsigned diff = (unsigned)(lp != le);
    for (size_t i = 0; i < n; i++) {
        unsigned char a = i < lp ? (unsigned char)presented[i] : 0;
        unsigned char b = i < le ? (unsigned char)expected[i] : 0;
        diff |= (unsigned)(a ^ b);
    }
    return diff == 0;
}
