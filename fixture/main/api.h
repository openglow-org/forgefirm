/*
 * forgefixture API: HTTP on port 80, JSON, every request but /nonce
 * under the key.
 *
 *   GET  /nonce            {"nonce": "<32 hex>", "expires_s": 10}: one
 *                          request's nonce, needing no key
 *   GET  /                 identity, uptime, the channels' states, the
 *                          enable jumper, the wifi link
 *   GET  /state            the same
 *   POST /lid        {"state": "open" | "close"}
 *   POST /interlock  {"state": "open" | "close"}
 *   POST /button     {"ms": 200}      one pulse, clamped to 20..500 ms
 *   POST /release                     every channel released
 *
 * The key never travels: a request carries a nonce from /nonce in
 * X-Fixture-Nonce and the HMAC of the request under the key in
 * X-Fixture-Mac (auth.h). A nonce answers one request and expires 10 s
 * after it was issued; without a good nonce and MAC every path answers
 * 401, and the refusal is logged with the caller's address. A lid or
 * interlock "open" energizes the channel (the loop opens); "close"
 * releases it. The button is refused with 409 while the enable jumper is
 * out or a pulse is still in progress. Errors are {"error": "..."};
 * actions answer with the state as GET / shows it.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

void api_start(const char *api_key, const char *hostname);
