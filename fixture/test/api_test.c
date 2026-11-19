/*
 * forgefixture API handlers held to account on the host: api.c as built
 * for the firmware, with auth.c, sha256.c and policy.c, against a stand-in
 * server and cJSON (stub/) and the fake relays, wifi and clock below; see
 * run.sh. The test plays the server: the routes api_start registers, each
 * request handed to its route's handler with that route's user_ctx.
 *
 * What it holds: GET /nonce answers the nonce the store holds, as the
 * "nonce" member the client reads, and states the life the store gives
 * it; a request signed under the key over
 * that nonce actuates once, and the same request played again does
 * nothing; a nonce the fixture never issued, a MAC under another key, and
 * a nonce older than its life are each refused with nothing actuated; a
 * body longer than the handler's buffer is refused before it is read; no
 * route that actuates, nor the state, answers a request that carries no
 * nonce and no MAC; a
 * loop request acts on its own route's channel; a handler reached with no
 * loop channel touches no relay; a relay call that fails is answered as a
 * failure; the state reports the server task's stack high-water mark.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_http_server.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include "../main/api.h"
#include "../main/auth.h"
#include "../main/relays.h"
#include "../main/wifi.h"

static int failures;

#define CHECK(cond) do { if (!(cond)) { failures++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

static const char *KEY = "0123456789abcdef0123456789abcdef";
static const char *OTHER_KEY = "fedcba9876543210fedcba9876543210";

/* ---- the fakes api.c calls: every actuation counted */
static int loop_calls, pulse_calls, release_calls;
static channel_t loop_ch;
static esp_err_t loop_answer = ESP_OK;
static int64_t now_us = 1000;
static unsigned char random_byte;

esp_err_t relays_set_loop(channel_t ch, bool energize)
{
    (void)energize;
    loop_calls++;
    loop_ch = ch;
    return loop_answer;
}
esp_err_t relays_pulse_button(int ms) { (void)ms; pulse_calls++; return ESP_OK; }
void relays_release(void) { release_calls++; }
relays_state_t relays_state(void) { relays_state_t s; memset(&s, 0, sizeof(s)); return s; }
void wifi_ip(char *buf, size_t n) { snprintf(buf, n, "192.0.2.1"); }
int wifi_rssi(void) { return -50; }
bool wifi_connected(void) { return true; }
int64_t esp_timer_get_time(void) { return now_us; }
void esp_fill_random(void *buf, size_t len) { memset(buf, ++random_byte, len); }
UBaseType_t uxTaskGetStackHighWaterMark(TaskHandle_t task) { (void)task; return 1234; }

static int actuations(void) { return loop_calls + pulse_calls + release_calls; }

/* ---- the server */
static httpd_uri_t routes[16];
static int nroutes;

esp_err_t httpd_start(httpd_handle_t *h, const httpd_config_t *c) { (void)c; *h = (httpd_handle_t)routes; return ESP_OK; }
esp_err_t httpd_register_uri_handler(httpd_handle_t h, const httpd_uri_t *u) { (void)h; routes[nroutes++] = *u; return ESP_OK; }
esp_err_t httpd_register_err_handler(httpd_handle_t h, httpd_err_code_t e, httpd_err_handler_func_t fn)
{
    (void)h; (void)e; (void)fn;
    return ESP_OK;
}
int httpd_req_to_sockfd(httpd_req_t *r) { (void)r; return -1; }
esp_err_t httpd_resp_set_status(httpd_req_t *r, const char *s) { r->status = s; return ESP_OK; }
esp_err_t httpd_resp_set_type(httpd_req_t *r, const char *t) { (void)r; (void)t; return ESP_OK; }
esp_err_t httpd_resp_send_500(httpd_req_t *r) { r->status = "500 Internal Server Error"; return ESP_OK; }
esp_err_t httpd_resp_send(httpd_req_t *r, const char *buf, ssize_t len)
{
    (void)len;
    snprintf(r->resp, sizeof(r->resp), "%s", buf);
    return ESP_OK;
}
const char *http_method_str(int m) { return m == HTTP_GET ? "GET" : m == HTTP_POST ? "POST" : "?"; }

/* The body's bytes, as many as the request carries: one that declares more
 * than it carries gets 0 from there on, as from a peer that sent no more.
 * Every call is counted. */
static int recv_calls;

int httpd_req_recv(httpd_req_t *r, char *buf, size_t len)
{
    recv_calls++;
    size_t have = strlen(r->body) - r->body_off;
    size_t n = have < len ? have : len;
    memcpy(buf, r->body + r->body_off, n);
    r->body_off += n;
    return (int)n;
}

/* As the IDF's: the value as much as fits, ESP_ERR_HTTPD_RESULT_TRUNC when
 * that is not all of it. */
esp_err_t httpd_req_get_hdr_value_str(httpd_req_t *r, const char *field, char *val, size_t n)
{
    for (int i = 0; i < 2; i++) {
        if (r->hdr_name[i] && strcmp(r->hdr_name[i], field) == 0) {
            snprintf(val, n, "%s", r->hdr_val[i]);
            return strlen(r->hdr_val[i]) >= n ? ESP_ERR_HTTPD_RESULT_TRUNC : ESP_OK;
        }
    }
    return ESP_ERR_NOT_FOUND;
}

static const httpd_uri_t *route(const char *uri)
{
    for (int i = 0; i < nroutes; i++)
        if (strcmp(routes[i].uri, uri) == 0)
            return &routes[i];
    abort();
}

/* ---- the client */

/* A nonce from the fixture's own GET /nonce, read out of its answer as the
 * tool's client reads it: the "nonce" member. An answer without one fails
 * here and leaves a nonce the fixture never issued. */
static void issue(char out[AUTH_NONCE_HEX + 1])
{
    static const char member[] = "\"nonce\":\"";
    httpd_req_t r = {.method = HTTP_GET, .uri = "/nonce"};
    route("/nonce")->handler(&r);
    /* the life the answer states is the store's: the client counts on it */
    static const char life[] = "\"expires_s\":";
    const char *says = strstr(r.resp, life);
    CHECK(says != NULL && atoll(says + sizeof(life) - 1) == AUTH_NONCE_LIFE_US / 1000000LL);
    const char *at = strstr(r.resp, member);
    bool whole = at != NULL && strlen(at + sizeof(member) - 1) > AUTH_NONCE_HEX;
    CHECK(whole);
    memset(out, '0', AUTH_NONCE_HEX);
    out[AUTH_NONCE_HEX] = '\0';
    if (whole)
        memcpy(out, at + sizeof(member) - 1, AUTH_NONCE_HEX);
}

/* One request handed to uri's route, with user_ctx as the server would
 * pass it: nonce and mac are its two headers (NULL: it carries neither).
 * Returns the status code answered. */
static int deliver(int method, const char *uri, void *user_ctx, const char *body, const char *nonce,
                   const char *mac, httpd_req_t *r)
{
    memset(r, 0, sizeof(*r));
    r->method = method;
    snprintf(r->uri, sizeof(r->uri), "%s", uri);
    r->content_len = strlen(body);
    r->body = body;
    r->user_ctx = user_ctx;
    if (nonce) {
        r->hdr_name[0] = "X-Fixture-Nonce";
        r->hdr_val[0] = nonce;
        r->hdr_name[1] = "X-Fixture-Mac";
        r->hdr_val[1] = mac;
    }
    route(uri)->handler(r);
    return atoi(r->status);
}

/* A request as the tool's client makes one: a fresh nonce, the MAC under
 * the key. */
static int request(int method, const char *uri, void *user_ctx, const char *body, httpd_req_t *r)
{
    char n[AUTH_NONCE_HEX + 1], mac[AUTH_MAC_HEX + 1];
    issue(n);
    auth_request_mac(KEY, http_method_str(method), uri, n, (const uint8_t *)body, strlen(body), mac);
    return deliver(method, uri, user_ctx, body, n, mac, r);
}

int main(void)
{
    api_start(KEY, "forgefixture");
    void *lid = route("/lid")->user_ctx, *ilk = route("/interlock")->user_ctx;
    char n[AUTH_NONCE_HEX + 1], mac[AUTH_MAC_HEX + 1];
    httpd_req_t r;

    /* a signed press actuates once; the same request again does nothing */
    issue(n);
    auth_request_mac(KEY, "POST", "/button", n, NULL, 0, mac);
    CHECK(deliver(HTTP_POST, "/button", NULL, "", n, mac, &r) == 200);
    CHECK(pulse_calls == 1);
    CHECK(deliver(HTTP_POST, "/button", NULL, "", n, mac, &r) == 401);
    CHECK(pulse_calls == 1);

    /* a nonce the fixture never issued, signed under the key */
    memset(n, 'a', AUTH_NONCE_HEX);
    n[AUTH_NONCE_HEX] = '\0';
    auth_request_mac(KEY, "POST", "/button", n, NULL, 0, mac);
    CHECK(deliver(HTTP_POST, "/button", NULL, "", n, mac, &r) == 401);
    CHECK(pulse_calls == 1);

    /* an issued nonce, signed under another key */
    issue(n);
    auth_request_mac(OTHER_KEY, "POST", "/button", n, NULL, 0, mac);
    CHECK(deliver(HTTP_POST, "/button", NULL, "", n, mac, &r) == 401);
    CHECK(pulse_calls == 1);

    /* an issued nonce, signed under the key, presented after its life */
    issue(n);
    auth_request_mac(KEY, "POST", "/button", n, NULL, 0, mac);
    now_us += AUTH_NONCE_LIFE_US + 1;
    CHECK(deliver(HTTP_POST, "/button", NULL, "", n, mac, &r) == 401);
    CHECK(pulse_calls == 1);

    /* a body one byte longer than the handler's buffer holds (256 and the
     * NUL), carried whole, is refused before any of it is read, and before
     * anything is judged */
    {
        char big[258];
        memset(big, 'x', sizeof(big) - 1);
        big[sizeof(big) - 1] = '\0';
        int read_before = recv_calls;
        CHECK(deliver(HTTP_POST, "/button", NULL, big, NULL, NULL, &r) == 413);
        CHECK(recv_calls == read_before);
        CHECK(pulse_calls == 1);
    }

    /* no route does anything for a request with no nonce and no MAC: none
     * actuates, and the state is not told */
    CHECK(deliver(HTTP_GET, "/", NULL, "", NULL, NULL, &r) == 401);
    CHECK(deliver(HTTP_POST, "/button", NULL, "", NULL, NULL, &r) == 401);
    CHECK(deliver(HTTP_POST, "/lid", lid, "{\"state\":\"open\"}", NULL, NULL, &r) == 401);
    CHECK(deliver(HTTP_POST, "/interlock", ilk, "{\"state\":\"open\"}", NULL, NULL, &r) == 401);
    CHECK(deliver(HTTP_POST, "/release", NULL, "", NULL, NULL, &r) == 401);
    CHECK(actuations() == 1);

    /* signed, each loop route acts on its own channel, and a release releases */
    CHECK(request(HTTP_POST, "/lid", lid, "{\"state\":\"open\"}", &r) == 200);
    CHECK(loop_calls == 1 && loop_ch == CH_LID);
    CHECK(request(HTTP_POST, "/interlock", ilk, "{\"state\":\"close\"}", &r) == 200);
    CHECK(loop_calls == 2 && loop_ch == CH_INTERLOCK);
    CHECK(request(HTTP_POST, "/release", NULL, "", &r) == 200);
    CHECK(release_calls == 1);

    /* the loop handler reached with no channel: refused, no relay */
    CHECK(request(HTTP_POST, "/lid", NULL, "{\"state\":\"open\"}", &r) == 400);
    CHECK(loop_calls == 2);

    /* a relay call that fails is answered as a failure */
    loop_answer = ESP_ERR_INVALID_ARG;
    CHECK(request(HTTP_POST, "/lid", lid, "{\"state\":\"open\"}", &r) == 400);
    loop_answer = ESP_OK;

    /* the state carries the stack high-water mark */
    CHECK(request(HTTP_GET, "/", NULL, "", &r) == 200);
    CHECK(strstr(r.resp, "\"httpd_stack_free\":1234") != NULL);

    if (failures) {
        printf("%d failure(s)\n", failures);
        return 1;
    }
    printf("api: all checks passed\n");
    return 0;
}
