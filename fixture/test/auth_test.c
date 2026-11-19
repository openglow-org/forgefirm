/*
 * forgefixture request authentication, held to account on the host with a
 * fake clock: cc -o auth_test auth_test.c ../main/auth.c ../main/sha256.c
 * ../main/policy.c (see run.sh).
 *
 * What it holds: SHA-256 and HMAC-SHA256 give the published vectors
 * (FIPS 180-4, RFC 4231), and a key longer than a block but shorter than
 * two is hashed first (a vector computed independently with Python's hmac);
 * a request's MAC is the one the tool's client computes for the same
 * request (the shared vector below); a nonce answers one request, a replay
 * is refused, a wrong MAC spends its nonce, a nonce expires 10 s after it
 * was issued, one never issued is refused, a nonce cut short matches no
 * nonce issued, and the MAC covers the method, the URI and the body; with
 * every slot live the oldest nonce gives way.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include <stdio.h>
#include <string.h>

#include "../main/auth.h"
#include "../main/sha256.h"

static int failures;

#define CHECK(cond) do { if (!(cond)) { failures++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

static void hex(const uint8_t *in, size_t n, char *out)
{
    static const char H[] = "0123456789abcdef";
    for (size_t i = 0; i < n; i++) {
        out[2 * i] = H[in[i] >> 4];
        out[2 * i + 1] = H[in[i] & 0x0f];
    }
    out[2 * n] = '\0';
}

static int sha_is(const void *msg, size_t len, const char *want)
{
    sha256_t s;
    uint8_t d[SHA256_DIGEST];
    char got[2 * SHA256_DIGEST + 1];
    sha256_init(&s);
    sha256_update(&s, msg, len);
    sha256_final(&s, d);
    hex(d, sizeof(d), got);
    return strcmp(got, want) == 0;
}

static int hmac_is(const void *key, size_t key_len, const void *msg, size_t len, const char *want)
{
    hmac_sha256_t h;
    uint8_t d[SHA256_DIGEST];
    char got[2 * SHA256_DIGEST + 1];
    hmac_sha256_init(&h, key, key_len);
    hmac_sha256_update(&h, msg, len);
    hmac_sha256_final(&h, d);
    hex(d, sizeof(d), got);
    return strcmp(got, want) == 0;
}

static const char *KEY = "0123456789abcdef0123456789abcdef";
static const char *BODY = "{\"ms\": 200}";

/* A nonce from random bytes all equal to `b`, issued at t. */
static void issue(auth_store_t *s, uint8_t b, int64_t t, char out[AUTH_NONCE_HEX + 1])
{
    uint8_t r[AUTH_NONCE_BYTES];
    memset(r, b, sizeof(r));
    auth_issue(s, r, t, out);
}

static auth_result_t press(auth_store_t *s, const char *nonce, int64_t t)
{
    char mac[AUTH_MAC_HEX + 1];
    auth_request_mac(KEY, "POST", "/button", nonce, (const uint8_t *)BODY, strlen(BODY), mac);
    return auth_check(s, KEY, "POST", "/button", nonce, mac, (const uint8_t *)BODY, strlen(BODY), t);
}

int main(void)
{
    /* SHA-256: FIPS 180-4's examples, the empty message, a million 'a' */
    CHECK(sha_is("", 0, "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"));
    CHECK(sha_is("abc", 3, "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"));
    const char *two = "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq";
    CHECK(sha_is(two, strlen(two), "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1"));
    {
        static char million[1000000];
        memset(million, 'a', sizeof(million));
        sha256_t s;
        uint8_t d[SHA256_DIGEST];
        char got[2 * SHA256_DIGEST + 1];
        sha256_init(&s);
        for (size_t off = 0; off < sizeof(million); off += 999)      /* uneven pieces across the blocks */
            sha256_update(&s, million + off, sizeof(million) - off < 999 ? sizeof(million) - off : 999);
        sha256_final(&s, d);
        hex(d, sizeof(d), got);
        CHECK(strcmp(got, "cdc76e5c9914fb9281a1c7e284d73e67f1809a48a497200e046d39ccc7112cd0") == 0);
    }

    /* HMAC-SHA256: RFC 4231 test cases 1, 2, 3, 4, 6, 7 */
    uint8_t k[131], m[50];
    memset(k, 0x0b, 20);
    CHECK(hmac_is(k, 20, "Hi There", 8, "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2e32cff7"));
    const char *jefe = "what do ya want for nothing?";
    CHECK(hmac_is("Jefe", 4, jefe, strlen(jefe), "5bdcc146bf60754e6a042426089575c75a003f089d2739839dec58b964ec3843"));
    memset(k, 0xaa, 20);
    memset(m, 0xdd, 50);
    CHECK(hmac_is(k, 20, m, 50, "773ea91e36800e46854db8ebd09181a72959098b3ef8c122d9635514ced565fe"));
    for (int i = 0; i < 25; i++)
        k[i] = (uint8_t)(i + 1);
    memset(m, 0xcd, 50);
    CHECK(hmac_is(k, 25, m, 50, "82558a389a443c0ea4cc819899f2083a85f0faa3e578f8077a2e3ff46729665b"));
    memset(k, 0xaa, 131);
    const char *tc6 = "Test Using Larger Than Block-Size Key - Hash Key First";
    CHECK(hmac_is(k, 131, tc6, strlen(tc6), "60e431591ee0b67f0d8a26aacbf5b77f8e0bc6213728c5140546040f0ee37f54"));
    const char *tc7 = "This is a test using a larger than block-size key and a larger than block-size data. "
                      "The key needs to be hashed before being used by the HMAC algorithm.";
    CHECK(hmac_is(k, 131, tc7, strlen(tc7), "9b09ffa71b942fcb27635fbcd5b0e944bfdc63644f0713938a7f51535c3a35e2"));
    /* a 100-byte key: longer than a block, so hashed first, though shorter
     * than two (hmac.new(b"\xaa" * 100, msg, hashlib.sha256)) */
    const char *k100 = "a 100-byte key: longer than a block, shorter than two";
    CHECK(hmac_is(k, 100, k100, strlen(k100), "db29fdf396e4feecd7169bfd33f1637a5613e68469030e010e95e8b20c6d9769"));

    /* The request's MAC is the client's (forgetest/tests/test_fixture.py
     * holds the client to the same vector). */
    {
        char mac[AUTH_MAC_HEX + 1];
        auth_request_mac(KEY, "POST", "/button", "00112233445566778899aabbccddeeff",
                         (const uint8_t *)BODY, strlen(BODY), mac);
        CHECK(strcmp(mac, "88147db98785e1a8d89d2457eae37b64a5f2fa1143fd6ada4faf3878fede3907") == 0);
    }

    auth_store_t s;
    char n[AUTH_NONCE_HEX + 1], n2[AUTH_NONCE_HEX + 1];
    auth_init(&s);

    /* one nonce, one request; the replay is refused */
    issue(&s, 0x11, 0, n);
    CHECK(strlen(n) == AUTH_NONCE_HEX);
    CHECK(press(&s, n, 1000) == AUTH_OK);
    CHECK(press(&s, n, 2000) == AUTH_UNKNOWN_NONCE);

    /* a wrong MAC spends the nonce: the right one after it is refused */
    issue(&s, 0x22, 0, n);
    char bad[AUTH_MAC_HEX + 1];
    memset(bad, '0', AUTH_MAC_HEX);
    bad[AUTH_MAC_HEX] = '\0';
    CHECK(auth_check(&s, KEY, "POST", "/button", n, bad, (const uint8_t *)BODY, strlen(BODY), 1000) == AUTH_BAD_MAC);
    CHECK(press(&s, n, 2000) == AUTH_UNKNOWN_NONCE);
    issue(&s, 0x23, 0, n);
    CHECK(auth_check(&s, KEY, "POST", "/button", n, NULL, (const uint8_t *)BODY, strlen(BODY), 1000) == AUTH_BAD_MAC);
    CHECK(press(&s, n, 2000) == AUTH_UNKNOWN_NONCE);

    /* good until 10 s after it was issued, refused from then on */
    issue(&s, 0x33, 5000000, n);
    issue(&s, 0x34, 5000000, n2);
    CHECK(press(&s, n, 5000000 + AUTH_NONCE_LIFE_US - 1) == AUTH_OK);
    CHECK(press(&s, n2, 5000000 + AUTH_NONCE_LIFE_US) == AUTH_UNKNOWN_NONCE);

    /* a nonce cut short matches no nonce issued, and spends none */
    {
        char cut[AUTH_NONCE_HEX + 1];
        issue(&s, 0x35, 0, n);
        memcpy(cut, n, sizeof(cut));
        cut[AUTH_NONCE_HEX - 1] = '\0';
        CHECK(press(&s, cut, 1000) == AUTH_UNKNOWN_NONCE);
        CHECK(press(&s, n, 1000) == AUTH_OK);
    }

    /* a nonce never issued, and none at all */
    CHECK(press(&s, "00112233445566778899aabbccddeeff", 0) == AUTH_UNKNOWN_NONCE);
    CHECK(auth_check(&s, KEY, "GET", "/", NULL, bad, NULL, 0, 0) == AUTH_NO_NONCE);
    CHECK(auth_check(&s, KEY, "GET", "/", "", bad, NULL, 0, 0) == AUTH_NO_NONCE);

    /* the MAC covers the method, the URI and the body */
    {
        char mac[AUTH_MAC_HEX + 1];
        issue(&s, 0x44, 0, n);
        auth_request_mac(KEY, "POST", "/button", n, (const uint8_t *)BODY, strlen(BODY), mac);
        CHECK(auth_check(&s, KEY, "GET", "/button", n, mac, (const uint8_t *)BODY, strlen(BODY), 1) == AUTH_BAD_MAC);
        issue(&s, 0x45, 0, n);
        auth_request_mac(KEY, "POST", "/lid", n, (const uint8_t *)BODY, strlen(BODY), mac);
        CHECK(auth_check(&s, KEY, "POST", "/interlock", n, mac, (const uint8_t *)BODY, strlen(BODY), 1) == AUTH_BAD_MAC);
        issue(&s, 0x46, 0, n);
        auth_request_mac(KEY, "POST", "/button", n, (const uint8_t *)BODY, strlen(BODY), mac);
        CHECK(auth_check(&s, KEY, "POST", "/button", n, mac, (const uint8_t *)"{\"ms\": 500}", 11, 1) == AUTH_BAD_MAC);
        issue(&s, 0x47, 0, n);
        auth_request_mac("another key", "POST", "/button", n, (const uint8_t *)BODY, strlen(BODY), mac);
        CHECK(auth_check(&s, KEY, "POST", "/button", n, mac, (const uint8_t *)BODY, strlen(BODY), 1) == AUTH_BAD_MAC);
    }

    /* every slot live: the next issue takes the oldest one's place, wherever
     * it sits. The first slot is spent and issued again, so the oldest
     * nonce is in the second slot and the first holds a newer one. */
    auth_init(&s);
    char oldest[AUTH_NONCE_HEX + 1], newer[AUTH_NONCE_HEX + 1];
    issue(&s, 0x50, 100, n);
    issue(&s, 0x51, 200, oldest);
    CHECK(press(&s, n, 250) == AUTH_OK);
    issue(&s, 0x52, 300, newer);
    for (int i = 2; i < AUTH_NONCE_SLOTS; i++)
        issue(&s, (uint8_t)(0x52 + i), 100 * (i + 2), n);
    issue(&s, 0x5f, 1000, n);
    CHECK(press(&s, oldest, 2000) == AUTH_UNKNOWN_NONCE);
    CHECK(press(&s, newer, 2000) == AUTH_OK);
    CHECK(press(&s, n, 2000) == AUTH_OK);

    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("auth: all checks passed\n");
    return 0;
}
