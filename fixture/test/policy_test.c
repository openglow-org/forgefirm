/*
 * forgefixture policy, held to account on the host: gcc -o policy_test
 * policy_test.c ../main/policy.c (see run.sh).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include <stdio.h>
#include <string.h>

#include "../main/policy.h"

static int failures;

#define CHECK(cond) do { if (!(cond)) { failures++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

int main(void)
{
    /* names and paths */
    CHECK(strcmp(policy_channel_name(CH_LID), "lid") == 0);
    CHECK(strcmp(policy_channel_name(CH_INTERLOCK), "interlock") == 0);
    CHECK(strcmp(policy_channel_name(CH_BUTTON), "button") == 0);
    CHECK(policy_channel_from_path("/lid") == CH_LID);
    CHECK(policy_channel_from_path("/interlock") == CH_INTERLOCK);
    CHECK(policy_channel_from_path("/button") == CH_BUTTON);
    CHECK(policy_channel_from_path("/lids") == -1);
    CHECK(policy_channel_from_path("lid") == -1);
    CHECK(policy_channel_from_path(NULL) == -1);

    /* a loop request: open energizes, close/closed releases, nothing else */
    bool e = false;
    CHECK(policy_parse_loop_state("open", &e) && e);
    CHECK(policy_parse_loop_state("close", &e) && !e);
    CHECK(policy_parse_loop_state("closed", &e) && !e);
    CHECK(!policy_parse_loop_state("Open", &e));
    CHECK(!policy_parse_loop_state("on", &e));
    CHECK(!policy_parse_loop_state("", &e));
    CHECK(!policy_parse_loop_state(NULL, &e));

    /* the button pulse: a default, a floor, a ceiling, never a hold */
    CHECK(policy_button_ms(0) == BUTTON_DEFAULT_MS);
    CHECK(policy_button_ms(-5) == BUTTON_DEFAULT_MS);
    CHECK(policy_button_ms(1) == BUTTON_MIN_MS);
    CHECK(policy_button_ms(250) == 250);
    CHECK(policy_button_ms(500) == BUTTON_MAX_MS);
    CHECK(policy_button_ms(501) == BUTTON_MAX_MS);
    CHECK(policy_button_ms(60000) == BUTTON_MAX_MS);
    CHECK(BUTTON_MAX_MS <= 500);

    /* state names */
    CHECK(strcmp(policy_state_name(CH_LID, true), "open") == 0);
    CHECK(strcmp(policy_state_name(CH_LID, false), "closed") == 0);
    CHECK(strcmp(policy_state_name(CH_INTERLOCK, true), "open") == 0);
    CHECK(strcmp(policy_state_name(CH_BUTTON, true), "pressed") == 0);
    CHECK(strcmp(policy_state_name(CH_BUTTON, false), "idle") == 0);

    /* the key: exact, whole, and never by prefix */
    CHECK(policy_key_matches("abc123", "abc123"));
    CHECK(!policy_key_matches("abc12", "abc123"));
    CHECK(!policy_key_matches("abc1234", "abc123"));
    CHECK(!policy_key_matches("", "abc123"));
    CHECK(!policy_key_matches("abc123", ""));
    CHECK(!policy_key_matches(NULL, "abc123"));
    CHECK(!policy_key_matches("ABC123", "abc123"));

    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("policy: all checks passed\n");
    return 0;
}
