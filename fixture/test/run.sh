#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
# The host tests of the fixture: its policy, its request authentication
# (the hash, the HMAC, the nonces), its relays against the stand-in GPIO,
# timer and mutex layer in stub/, and its API handlers (api.c as the
# firmware builds it) against the stand-in server and cJSON there. Needs
# only a C compiler.
set -e
cd "$(dirname "$0")"
${CC:-cc} -std=c11 -Wall -Wextra -Werror -o policy_test policy_test.c ../main/policy.c
./policy_test
${CC:-cc} -std=c11 -Wall -Wextra -Werror -o auth_test auth_test.c ../main/auth.c ../main/sha256.c ../main/policy.c
./auth_test
${CC:-cc} -std=c11 -Wall -Wextra -Werror -Istub -o relays_test relays_test.c ../main/relays.c ../main/policy.c
./relays_test
${CC:-cc} -std=c11 -D_DEFAULT_SOURCE -Wall -Wextra -Werror -Istub -o api_test api_test.c ../main/api.c \
    ../main/auth.c ../main/sha256.c ../main/policy.c stub/cJSON.c
./api_test
