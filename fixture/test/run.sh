#!/bin/sh
# Copyright 2026 514 LLC d/b/a OpenGlow
# Written by Scott Wiederhold
# https://community.openglow.org
# SPDX-License-Identifier:    MIT
# The host tests of the fixture: its policy, and its relays against the
# stand-in GPIO, timer and mutex layer in stub/. Needs only a C compiler.
set -e
cd "$(dirname "$0")"
${CC:-cc} -std=c11 -Wall -Wextra -Werror -o policy_test policy_test.c ../main/policy.c
./policy_test
${CC:-cc} -std=c11 -Wall -Wextra -Werror -Istub -o relays_test relays_test.c ../main/relays.c ../main/policy.c
./relays_test
