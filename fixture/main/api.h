/*
 * forgefixture API: HTTP on port 80, JSON, every request under the key.
 *
 *   GET  /                 identity, uptime, the channels' states, the
 *                          enable jumper, the wifi link
 *   GET  /state            the same
 *   POST /lid        {"state": "open" | "close"}
 *   POST /interlock  {"state": "open" | "close"}
 *   POST /button     {"ms": 200}      one pulse, clamped to 20..500 ms
 *   POST /release                     every channel released
 *
 * The key travels in the X-Fixture-Key header; without it, or with a
 * wrong one, every path answers 401. A lid or interlock "open" energizes
 * the channel (the loop opens); "close" releases it. The button is
 * refused with 409 while the enable jumper is out or a pulse is still in
 * progress. Errors are {"error": "..."}; actions answer with the state
 * as GET / shows it.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

void api_start(const char *api_key, const char *hostname);
