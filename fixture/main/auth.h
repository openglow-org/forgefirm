/*
 * forgefixture request authentication: a single-use nonce the fixture
 * issues, and an HMAC-SHA256 under the API key over the request and that
 * nonce. The key never travels; a captured request cannot be played again,
 * because its nonce was spent the first time it was presented, and a
 * nonce left unused expires.
 *
 * The request carries X-Fixture-Nonce (a nonce from GET /nonce) and
 * X-Fixture-Mac, the lowercase hex of
 *
 *     HMAC-SHA256(key, method "\n" uri "\n" nonce "\n" body)
 *
 * with the method as the request line says it ("GET", "POST"), the URI as
 * sent (path and any query), and the body's bytes as sent (empty for a
 * GET). No ESP-IDF here: test/auth_test.c holds it to account on the host
 * with a fake clock.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "sha256.h"

#define AUTH_NONCE_BYTES   16
#define AUTH_NONCE_HEX     (2 * AUTH_NONCE_BYTES)
#define AUTH_MAC_HEX       (2 * SHA256_DIGEST)
/* Nonces outstanding at once: one per client request in flight, with room
 * for the tool's probe and a test's action to overlap. Issuing past this
 * reuses the oldest slot. */
#define AUTH_NONCE_SLOTS   8
/* A nonce is good for this long after it was issued (microseconds). */
#define AUTH_NONCE_LIFE_US 10000000LL

typedef struct {
    char hex[AUTH_NONCE_HEX + 1];
    int64_t issued_us;
    bool live;
} auth_nonce_t;

typedef struct {
    auth_nonce_t slot[AUTH_NONCE_SLOTS];
} auth_store_t;

typedef enum {
    AUTH_OK = 0,
    AUTH_NO_NONCE,          /* the request names no nonce */
    AUTH_UNKNOWN_NONCE,     /* a nonce this store did not issue, one already presented, or one expired */
    AUTH_BAD_MAC,           /* a good nonce, and a MAC missing or not the key's */
} auth_result_t;

void auth_init(auth_store_t *s);

/* A new nonce made of `random`, issued at now_us, written as hex into
 * `out`. A free or expired slot takes it; with every slot live the oldest
 * is reused. */
void auth_issue(auth_store_t *s, const uint8_t random[AUTH_NONCE_BYTES], int64_t now_us,
                char out[AUTH_NONCE_HEX + 1]);

/* The MAC a request carries, as hex (see above). */
void auth_request_mac(const char *key, const char *method, const char *uri, const char *nonce,
                      const uint8_t *body, size_t body_len, char out[AUTH_MAC_HEX + 1]);

/* The decision for one request at now_us. A nonce that names a live slot
 * is spent here, whatever the MAC says, so one nonce answers one request
 * and a wrong MAC costs the caller that nonce. */
auth_result_t auth_check(auth_store_t *s, const char *key, const char *method, const char *uri,
                         const char *nonce, const char *mac, const uint8_t *body, size_t body_len,
                         int64_t now_us);

/* What the log and the 401 say about a refusal. */
const char *auth_result_name(auth_result_t r);
