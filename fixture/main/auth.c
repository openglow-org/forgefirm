/*
 * forgefixture request authentication (see auth.h).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "auth.h"

#include <string.h>

#include "policy.h"

static const char HEX[] = "0123456789abcdef";

static void to_hex(const uint8_t *in, size_t n, char *out)
{
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = HEX[in[i] >> 4];
        out[2 * i + 1] = HEX[in[i] & 0x0f];
    }
    out[2 * n] = '\0';
}

static bool expired(const auth_nonce_t *n, int64_t now_us)
{
    int64_t age = now_us - n->issued_us;
    return age < 0 || age >= AUTH_NONCE_LIFE_US;
}

void auth_init(auth_store_t *s)
{
    memset(s, 0, sizeof(*s));
}

void auth_issue(auth_store_t *s, const uint8_t random[AUTH_NONCE_BYTES], int64_t now_us,
                char out[AUTH_NONCE_HEX + 1])
{
    int pick = -1;
    for (int i = 0; i < AUTH_NONCE_SLOTS && pick < 0; i++)
        if (!s->slot[i].live || expired(&s->slot[i], now_us))
            pick = i;
    if (pick < 0) {
        pick = 0;
        for (int i = 1; i < AUTH_NONCE_SLOTS; i++)
            if (s->slot[i].issued_us < s->slot[pick].issued_us)
                pick = i;
    }
    auth_nonce_t *n = &s->slot[pick];
    to_hex(random, AUTH_NONCE_BYTES, n->hex);
    n->issued_us = now_us;
    n->live = true;
    memcpy(out, n->hex, sizeof(n->hex));
}

void auth_request_mac(const char *key, const char *method, const char *uri, const char *nonce,
                      const uint8_t *body, size_t body_len, char out[AUTH_MAC_HEX + 1])
{
    static const char NL = '\n';
    hmac_sha256_t h;
    hmac_sha256_init(&h, key, strlen(key));
    hmac_sha256_update(&h, method, strlen(method));
    hmac_sha256_update(&h, &NL, 1);
    hmac_sha256_update(&h, uri, strlen(uri));
    hmac_sha256_update(&h, &NL, 1);
    hmac_sha256_update(&h, nonce, strlen(nonce));
    hmac_sha256_update(&h, &NL, 1);
    if (body_len > 0)
        hmac_sha256_update(&h, body, body_len);
    uint8_t tag[SHA256_DIGEST];
    hmac_sha256_final(&h, tag);
    to_hex(tag, SHA256_DIGEST, out);
}

auth_result_t auth_check(auth_store_t *s, const char *key, const char *method, const char *uri,
                         const char *nonce, const char *mac, const uint8_t *body, size_t body_len,
                         int64_t now_us)
{
    if (nonce == NULL || nonce[0] == '\0')
        return AUTH_NO_NONCE;
    auth_nonce_t *found = NULL;
    for (int i = 0; i < AUTH_NONCE_SLOTS; i++)
        if (s->slot[i].live && strcmp(s->slot[i].hex, nonce) == 0)
            found = &s->slot[i];
    if (found == NULL)
        return AUTH_UNKNOWN_NONCE;
    /* Spent on its first presentation, before anything else is judged. */
    found->live = false;
    if (expired(found, now_us))
        return AUTH_UNKNOWN_NONCE;
    if (mac == NULL)
        return AUTH_BAD_MAC;
    char want[AUTH_MAC_HEX + 1];
    auth_request_mac(key, method, uri, nonce, body, body_len, want);
    return policy_key_matches(mac, want) ? AUTH_OK : AUTH_BAD_MAC;
}

const char *auth_result_name(auth_result_t r)
{
    switch (r) {
    case AUTH_OK:
        return "ok";
    case AUTH_NO_NONCE:
        return "no nonce";
    case AUTH_UNKNOWN_NONCE:
        return "nonce unknown, spent or expired";
    case AUTH_BAD_MAC:
        return "wrong MAC";
    }
    return "?";
}
