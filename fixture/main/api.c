/*
 * forgefixture API (see api.h).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "api.h"

#include <string.h>
#include <sys/socket.h>

#include "cJSON.h"
#include "esp_http_server.h"
#include "esp_idf_version.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "lwip/sockets.h"

#include "policy.h"
#include "relays.h"
#include "version.h"
#include "wifi.h"

static const char *TAG = "api";

static const char *s_key;
static const char *s_hostname;

#define BODY_MAX 256

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
    return o;
}

/* The key check, before anything else on every path. */
static bool authorized(httpd_req_t *req)
{
    char presented[96] = {0};
    esp_err_t r = httpd_req_get_hdr_value_str(req, "X-Fixture-Key", presented, sizeof(presented));
    bool ok = (r == ESP_OK) && policy_key_matches(presented, s_key);
    if (!ok) {
        char who[48];
        peer(req, who, sizeof(who));
        ESP_LOGW(TAG, "%s %s from %s: refused (%s)", http_method_str(req->method), req->uri, who,
                 r == ESP_OK ? "wrong key" : "no key");
        send_error(req, "401 Unauthorized", "X-Fixture-Key missing or wrong");
    }
    return ok;
}

/* The JSON body, or NULL (an error already sent). An empty body parses
 * as an empty object so "POST /button" alone means the default pulse. */
static cJSON *body_json(httpd_req_t *req)
{
    char buf[BODY_MAX + 1];
    if (req->content_len > BODY_MAX) {
        send_error(req, "413 Payload Too Large", "body over 256 bytes");
        return NULL;
    }
    size_t got = 0;
    while (got < req->content_len) {
        int n = httpd_req_recv(req, buf + got, req->content_len - got);
        if (n <= 0) {
            send_error(req, "400 Bad Request", "body not received");
            return NULL;
        }
        got += (size_t)n;
    }
    buf[got] = '\0';
    cJSON *o = got ? cJSON_Parse(buf) : cJSON_CreateObject();
    if (o == NULL || !cJSON_IsObject(o)) {
        cJSON_Delete(o);
        send_error(req, "400 Bad Request", "body is not a JSON object");
        return NULL;
    }
    return o;
}

static esp_err_t h_state(httpd_req_t *req)
{
    if (!authorized(req))
        return ESP_OK;
    return send_json(req, "200 OK", state_json());
}

static esp_err_t h_loop(httpd_req_t *req)
{
    if (!authorized(req))
        return ESP_OK;
    int ch = policy_channel_from_path(req->uri);
    cJSON *body = body_json(req);
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
    relays_set_loop((channel_t)ch, energize);
    return send_json(req, "200 OK", state_json());
}

static esp_err_t h_button(httpd_req_t *req)
{
    if (!authorized(req))
        return ESP_OK;
    cJSON *body = body_json(req);
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
    if (!authorized(req))
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
    httpd_config_t cfg = HTTPD_DEFAULT_CONFIG();
    cfg.server_port = 80;
    cfg.lru_purge_enable = true;
    cfg.max_open_sockets = 4;
    httpd_handle_t srv = NULL;
    ESP_ERROR_CHECK(httpd_start(&srv, &cfg));

    static const httpd_uri_t routes[] = {
        {.uri = "/", .method = HTTP_GET, .handler = h_state},
        {.uri = "/state", .method = HTTP_GET, .handler = h_state},
        {.uri = "/lid", .method = HTTP_POST, .handler = h_loop},
        {.uri = "/interlock", .method = HTTP_POST, .handler = h_loop},
        {.uri = "/button", .method = HTTP_POST, .handler = h_button},
        {.uri = "/release", .method = HTTP_POST, .handler = h_release},
    };
    for (size_t i = 0; i < sizeof(routes) / sizeof(routes[0]); i++)
        ESP_ERROR_CHECK(httpd_register_uri_handler(srv, &routes[i]));
    httpd_register_err_handler(srv, HTTPD_404_NOT_FOUND, h_404);
    ESP_LOGI(TAG, "listening on :%d", cfg.server_port);
}
