/*
 * forgefixture API (see api.h).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "api.h"

#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/socket.h>

#include "cJSON.h"
#include "esp_http_server.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "lwip/sockets.h"

#include "auth.h"
#include "policy.h"
#include "relays.h"
#include "version.h"
#include "wifi.h"

static const char *TAG = "api";

static const char *s_key;
static const char *s_hostname;
/* The nonces outstanding. The server runs every handler in its one task,
 * so nothing else touches the store. */
static auth_store_t s_auth;

#define BODY_MAX 256

/* A route's channel rides in its user_ctx as the channel plus one, so a
 * route registered without one reads as no channel. */
#define ROUTE_CHANNEL(ch) ((void *)(intptr_t)((ch) + 1))

/* The peer's address for the log: every action is attributed. */
static void peer(httpd_req_t *req, char *buf, size_t n)
{
    struct sockaddr_in addr;            /* IPv4 only: the fixture speaks nothing else */
    socklen_t len = sizeof(addr);
    int fd = httpd_req_to_sockfd(req);
    buf[0] = '\0';
    if (fd >= 0 && getpeername(fd, (struct sockaddr *)&addr, &len) == 0 && addr.sin_family == AF_INET)
        inet_ntoa_r(addr.sin_addr, buf, n);
}

static esp_err_t send_json(httpd_req_t *req, const char *status, cJSON *obj)
{
    char *text = cJSON_PrintUnformatted(obj);
    cJSON_Delete(obj);
    if (text == NULL)
        return httpd_resp_send_500(req);
    httpd_resp_set_status(req, status);
    httpd_resp_set_type(req, "application/json");
    esp_err_t r = httpd_resp_send(req, text, HTTPD_RESP_USE_STRLEN);
    cJSON_free(text);
    return r;
}

static esp_err_t send_error(httpd_req_t *req, const char *status, const char *msg)
{
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "error", msg);
    return send_json(req, status, o);
}

static cJSON *state_json(void)
{
    relays_state_t st = relays_state();
    char ip[40];
    wifi_ip(ip, sizeof(ip));

    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "device", "forgefixture");
    cJSON_AddStringToObject(o, "hostname", s_hostname);
    cJSON_AddStringToObject(o, "version", FIXTURE_VERSION);
    cJSON_AddStringToObject(o, "idf", esp_get_idf_version());
    cJSON_AddNumberToObject(o, "uptime_s", (double)(esp_timer_get_time() / 1000000LL));
    cJSON *ch = cJSON_AddObjectToObject(o, "channels");
    for (int i = 0; i < CH_COUNT; i++)
        cJSON_AddStringToObject(ch, policy_channel_name((channel_t)i),
                                policy_state_name((channel_t)i, st.energized[i]));
    cJSON_AddBoolToObject(o, "button_enabled", st.button_enabled);
    cJSON_AddBoolToObject(o, "button_pulsing", st.button_pulsing);
    cJSON *w = cJSON_AddObjectToObject(o, "wifi");
    cJSON_AddBoolToObject(w, "connected", wifi_connected());
    cJSON_AddStringToObject(w, "ip", ip);
    cJSON_AddNumberToObject(w, "rssi", wifi_rssi());
    /* The least stack the server's task has had free since it started, in
     * bytes (ESP-IDF's FreeRTOS counts a stack in bytes): every handler runs
     * on that task, the request's body, nonce and HMAC state among its
     * locals, so this says how near the deepest request came to the end. */
    cJSON_AddNumberToObject(o, "httpd_stack_free", (double)uxTaskGetStackHighWaterMark(NULL));
    return o;
}

/* The request's body, whole, into buf (BODY_MAX + 1 bytes, NUL ended):
 * false with the error already sent. The MAC covers these bytes, so they
 * are read before anything is judged. */
static bool body_read(httpd_req_t *req, char *buf, size_t *len)
{
    if (req->content_len > BODY_MAX) {
        send_error(req, "413 Payload Too Large", "body over 256 bytes");
        return false;
    }
    size_t got = 0;
    while (got < req->content_len) {
        int n = httpd_req_recv(req, buf + got, req->content_len - got);
        if (n <= 0) {
            send_error(req, "400 Bad Request", "body not received");
            return false;
        }
        got += (size_t)n;
    }
    buf[got] = '\0';
    *len = got;
    return true;
}

/* A header's value into buf, or NULL when it is absent or longer than
 * buf holds (never a truncated value). */
static const char *header(httpd_req_t *req, const char *field, char *buf, size_t n)
{
    return httpd_req_get_hdr_value_str(req, field, buf, n) == ESP_OK ? buf : NULL;
}

/* The nonce and the MAC, before anything is done on any path but
 * /nonce. */
static bool authorized(httpd_req_t *req, const char *body, size_t body_len)
{
    char nonce_buf[AUTH_NONCE_HEX + 2];
    char mac_buf[AUTH_MAC_HEX + 2];
    const char *nonce = header(req, "X-Fixture-Nonce", nonce_buf, sizeof(nonce_buf));
    const char *mac = header(req, "X-Fixture-Mac", mac_buf, sizeof(mac_buf));
    auth_result_t r = auth_check(&s_auth, s_key, http_method_str(req->method), req->uri, nonce, mac,
                                 (const uint8_t *)body, body_len, esp_timer_get_time());
    if (r == AUTH_OK)
        return true;
    char who[48];
    peer(req, who, sizeof(who));
    ESP_LOGW(TAG, "%s %s from %s: refused (%s)", http_method_str(req->method), req->uri, who,
             auth_result_name(r));
    char msg[64];
    snprintf(msg, sizeof(msg), "not authorized: %s", auth_result_name(r));
    send_error(req, "401 Unauthorized", msg);
    return false;
}

/* The body read and the request authorized: true, with the body in buf.
 * Otherwise false, the refusal already sent. */
static bool take_request(httpd_req_t *req, char *buf, size_t *len)
{
    return body_read(req, buf, len) && authorized(req, buf, *len);
}

/* The body as a JSON object, or NULL (an error already sent). An empty
 * body parses as an empty object so "POST /button" alone means the
 * default pulse. */
static cJSON *body_json(httpd_req_t *req, const char *buf, size_t len)
{
    cJSON *o = len ? cJSON_Parse(buf) : cJSON_CreateObject();
    if (o == NULL || !cJSON_IsObject(o)) {
        cJSON_Delete(o);
        send_error(req, "400 Bad Request", "body is not a JSON object");
        return NULL;
    }
    return o;
}

/* A nonce for the next request. Asking needs no key: a nonce is worth
 * nothing without one. */
static esp_err_t h_nonce(httpd_req_t *req)
{
    uint8_t random[AUTH_NONCE_BYTES];
    char hex[AUTH_NONCE_HEX + 1];
    esp_fill_random(random, sizeof(random));
    auth_issue(&s_auth, random, esp_timer_get_time(), hex);
    cJSON *o = cJSON_CreateObject();
    cJSON_AddStringToObject(o, "nonce", hex);
    cJSON_AddNumberToObject(o, "expires_s", (double)(AUTH_NONCE_LIFE_US / 1000000LL));
    return send_json(req, "200 OK", o);
}

static esp_err_t h_state(httpd_req_t *req)
{
    char buf[BODY_MAX + 1];
    size_t len;
    if (!take_request(req, buf, &len))
        return ESP_OK;
    return send_json(req, "200 OK", state_json());
}

static esp_err_t h_loop(httpd_req_t *req)
{
    char buf[BODY_MAX + 1];
    size_t len;
    if (!take_request(req, buf, &len))
        return ESP_OK;
    /* The channel is the matched route's own (ROUTE_CHANNEL), never parsed
     * from the URI; a route that names none is refused. */
    int ch = (int)(intptr_t)req->user_ctx - 1;
    if (ch != CH_LID && ch != CH_INTERLOCK)
        return send_error(req, "400 Bad Request", "not a loop channel");
    cJSON *body = body_json(req, buf, len);
    if (body == NULL)
        return ESP_OK;
    const cJSON *state = cJSON_GetObjectItemCaseSensitive(body, "state");
    bool energize;
    if (!cJSON_IsString(state) || !policy_parse_loop_state(state->valuestring, &energize)) {
        cJSON_Delete(body);
        return send_error(req, "400 Bad Request", "state must be \"open\" or \"close\"");
    }
    cJSON_Delete(body);
    char who[48];
    peer(req, who, sizeof(who));
    ESP_LOGI(TAG, "%s %s by %s", policy_channel_name((channel_t)ch), energize ? "open" : "close", who);
    if (relays_set_loop((channel_t)ch, energize) != ESP_OK)
        return send_error(req, "400 Bad Request", "not a loop channel");
    return send_json(req, "200 OK", state_json());
}

static esp_err_t h_button(httpd_req_t *req)
{
    char buf[BODY_MAX + 1];
    size_t len;
    if (!take_request(req, buf, &len))
        return ESP_OK;
    cJSON *body = body_json(req, buf, len);
    if (body == NULL)
        return ESP_OK;
    const cJSON *ms = cJSON_GetObjectItemCaseSensitive(body, "ms");
    int want = cJSON_IsNumber(ms) ? (int)ms->valuedouble : 0;
    cJSON_Delete(body);
    int pulse = policy_button_ms(want);
    char who[48];
    peer(req, who, sizeof(who));
    esp_err_t r = relays_pulse_button(pulse);
    if (r == ESP_ERR_NOT_ALLOWED) {
        ESP_LOGW(TAG, "button press by %s refused: enable jumper out", who);
        return send_error(req, "409 Conflict", "button disabled: the enable jumper is out");
    }
    if (r == ESP_ERR_INVALID_STATE) {
        ESP_LOGW(TAG, "button press by %s refused: a pulse is in progress", who);
        return send_error(req, "409 Conflict", "a button pulse is in progress");
    }
    ESP_LOGI(TAG, "button %d ms by %s", pulse, who);
    cJSON *o = state_json();
    cJSON_AddNumberToObject(o, "pulse_ms", pulse);
    return send_json(req, "200 OK", o);
}

static esp_err_t h_release(httpd_req_t *req)
{
    char buf[BODY_MAX + 1];
    size_t len;
    if (!take_request(req, buf, &len))
        return ESP_OK;
    char who[48];
    peer(req, who, sizeof(who));
    ESP_LOGI(TAG, "release by %s", who);
    relays_release();
    return send_json(req, "200 OK", state_json());
}

static esp_err_t h_404(httpd_req_t *req, httpd_err_code_t err)
{
    (void)err;
    return send_error(req, "404 Not Found", "no such path");
}

void api_start(const char *api_key, const char *hostname)
{
    s_key = api_key;
    s_hostname = hostname;
    auth_init(&s_auth);
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.lru_purge_enable = true;
    cfg.max_open_sockets = 4;
    httpd_handle_t srv = NULL;
    ESP_ERROR_CHECK(httpd_start(&srv, &cfg));

    static const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = h_state},
        {.uri = "/state", .method = HTTP_GET, .handler = h_state},
        {.uri = "/nonce", .method = HTTP_GET, .handler = h_nonce},
        {.uri = "/lid", .method = HTTP_POST, .handler = h_loop, .user_ctx = ROUTE_CHANNEL(CH_LID)},
        {.uri = "/interlock", .method = HTTP_POST, .handler = h_loop, .user_ctx = ROUTE_CHANNEL(CH_INTERLOCK)},
        {.uri = "/button", .method = HTTP_POST, .handler = h_button},
        {.uri = "/release", .method = HTTP_POST, .handler = h_release},
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); i++)
        ESP_ERROR_CHECK(httpd_register_uri_handler(srv, &routes[i]));
    httpd_register_err_handler(srv, HTTPD_404_NOT_FOUND, h_404);
    ESP_LOGI(TAG, "listening on :%d", cfg.server_port);
}
