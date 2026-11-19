/*
 * forgefixture SHA-256 and HMAC-SHA256 (FIPS 180-4, RFC 2104), portable C
 * with no ESP-IDF dependency, so the host test holds them to the published
 * vectors (test/auth_test.c).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#define SHA256_BLOCK 64
#define SHA256_DIGEST 32

typedef struct {
    uint32_t h[8];
    uint64_t length;            /* bytes taken so far */
    uint8_t block[SHA256_BLOCK];
    size_t used;                /* bytes waiting in block */
} sha256_t;

void sha256_init(sha256_t *s);
void sha256_update(sha256_t *s, const void *data, size_t len);
void sha256_final(sha256_t *s, uint8_t out[SHA256_DIGEST]);

/* HMAC-SHA256, taken in pieces: init with the key, update with each part
 * of the message, final for the tag. */
typedef struct {
    sha256_t inner;
    uint8_t outer_key[SHA256_BLOCK];    /* the key block XOR opad */
} hmac_sha256_t;

void hmac_sha256_init(hmac_sha256_t *h, const void *key, size_t key_len);
void hmac_sha256_update(hmac_sha256_t *h, const void *data, size_t len);
void hmac_sha256_final(hmac_sha256_t *h, uint8_t out[SHA256_DIGEST]);
