/*
 * forgefixture SHA-256 and HMAC-SHA256 (see sha256.h).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "sha256.h"

#include <string.h>

static const uint32_t K[64] = {
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
};

static uint32_t rotr(uint32_t x, unsigned n)
{
    return (x >> n) | (x << (32u - n));
}

static void compress(uint32_t h[8], const uint8_t block[SHA256_BLOCK])
{
    uint32_t w[64];
    for (int i = 0; i < 16; i++)
        w[i] = ((uint32_t)block[4 * i] << 24) | ((uint32_t)block[4 * i + 1] << 16) |
               ((uint32_t)block[4 * i + 2] << 8) | (uint32_t)block[4 * i + 3];
    for (int i = 16; i < 64; i++) {
        uint32_t s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >> 3);
        uint32_t s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4], f = h[5], g = h[6], hh = h[7];
    for (int i = 0; i < 64; i++) {
        uint32_t S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        uint32_t ch = (e & f) ^ (~e & g);
        uint32_t t1 = hh + S1 + ch + K[i] + w[i];
        uint32_t S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        uint32_t maj = (a & b) ^ (a & c) ^ (b & c);
        uint32_t t2 = S0 + maj;
        hh = g;
        g = f;
        f = e;
        e = d + t1;
        d = c;
        c = b;
        b = a;
        a = t1 + t2;
    }
    h[0] += a;
    h[1] += b;
    h[2] += c;
    h[3] += d;
    h[4] += e;
    h[5] += f;
    h[6] += g;
    h[7] += hh;
}

void sha256_init(sha256_t *s)
{
    static const uint32_t H0[8] = {
        0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
    };
    memcpy(s->h, H0, sizeof(H0));
    s->length = 0;
    s->used = 0;
}

void sha256_update(sha256_t *s, const void *data, size_t len)
{
    const uint8_t *p = data;
    s->length += len;
    while (len > 0) {
        size_t take = SHA256_BLOCK - s->used;
        if (take > len)
            take = len;
        memcpy(s->block + s->used, p, take);
        s->used += take;
        p += take;
        len -= take;
        if (s->used == SHA256_BLOCK) {
            compress(s->h, s->block);
            s->used = 0;
        }
    }
}

void sha256_final(sha256_t *s, uint8_t out[SHA256_DIGEST])
{
    uint64_t bits = s->length * 8u;
    uint8_t pad = 0x80;
    sha256_update(s, &pad, 1);
    pad = 0;
    while (s->used != SHA256_BLOCK - 8)
        sha256_update(s, &pad, 1);
    uint8_t len_be[8];
    for (int i = 0; i < 8; i++)
        len_be[i] = (uint8_t)(bits >> (56 - 8 * i));
    sha256_update(s, len_be, 8);
    for (int i = 0; i < 8; i++) {
        out[4 * i] = (uint8_t)(s->h[i] >> 24);
        out[4 * i + 1] = (uint8_t)(s->h[i] >> 16);
        out[4 * i + 2] = (uint8_t)(s->h[i] >> 8);
        out[4 * i + 3] = (uint8_t)s->h[i];
    }
}

void hmac_sha256_init(hmac_sha256_t *h, const void *key, size_t key_len)
{
    uint8_t block[SHA256_BLOCK] = {0};
    if (key_len > SHA256_BLOCK) {
        sha256_t k;
        sha256_init(&k);
        sha256_update(&k, key, key_len);
        sha256_final(&k, block);
    } else if (key_len > 0) {
        memcpy(block, key, key_len);
    }
    uint8_t inner_key[SHA256_BLOCK];
    for (int i = 0; i < SHA256_BLOCK; i++) {
        inner_key[i] = block[i] ^ 0x36;
        h->outer_key[i] = block[i] ^ 0x5c;
    }
    sha256_init(&h->inner);
    sha256_update(&h->inner, inner_key, SHA256_BLOCK);
}

void hmac_sha256_update(hmac_sha256_t *h, const void *data, size_t len)
{
    sha256_update(&h->inner, data, len);
}

void hmac_sha256_final(hmac_sha256_t *h, uint8_t out[SHA256_DIGEST])
{
    uint8_t inner_digest[SHA256_DIGEST];
    sha256_final(&h->inner, inner_digest);
    sha256_t outer;
    sha256_init(&outer);
    sha256_update(&outer, h->outer_key, SHA256_BLOCK);
    sha256_update(&outer, inner_digest, SHA256_DIGEST);
    sha256_final(&outer, out);
}
