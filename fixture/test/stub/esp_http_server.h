/*
 * Host stand-in for ESP-IDF's esp_http_server.h: the server calls api.c
 * makes, defined by the host test (api_test.c), which plays the server: it
 * registers the routes, hands a handler a request with the matched route's
 * user_ctx, and keeps what the handler answered.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>
#include <sys/types.h>

#include "esp_err.h"

#define ESP_ERR_NOT_FOUND          0x105
#define ESP_ERR_HTTPD_RESULT_TRUNC 0xb003
#define HTTPD_RESP_USE_STRLEN      -1

enum { HTTP_GET = 1, HTTP_POST = 3 };
typedef enum { HTTPD_404_NOT_FOUND = 404 } httpd_err_code_t;

typedef void *httpd_handle_t;

typedef struct httpd_req {
    int method;
    char uri[64];
    size_t content_len;
    void *user_ctx;
    /* the host harness's own: what the request carries, what it was answered */
    const char *hdr_name[2], *hdr_val[2];
    const char *body;
    size_t body_off;
    const char *status;
    char resp[512];
} httpd_req_t;

typedef struct {
    const char *uri;
    int method;
    esp_err_t (*handler)(httpd_req_t *req);
    void *user_ctx;
} httpd_uri_t;

typedef struct {
    int server_port;
    bool lru_purge_enable;
    int max_open_sockets;
} httpd_config_t;

#define HTTPD_DEFAULT_CONFIG() { .server_port = 80 }

typedef esp_err_t (*httpd_err_handler_func_t)(httpd_req_t *req, httpd_err_code_t err);

esp_err_t httpd_start(httpd_handle_t *handle, const httpd_config_t *config);
esp_err_t httpd_register_uri_handler(httpd_handle_t handle, const httpd_uri_t *uri);
esp_err_t httpd_register_err_handler(httpd_handle_t handle, httpd_err_code_t error, httpd_err_handler_func_t fn);
int httpd_req_to_sockfd(httpd_req_t *req);
int httpd_req_recv(httpd_req_t *req, char *buf, size_t len);
esp_err_t httpd_req_get_hdr_value_str(httpd_req_t *req, const char *field, char *val, size_t val_size);
esp_err_t httpd_resp_set_status(httpd_req_t *req, const char *status);
esp_err_t httpd_resp_set_type(httpd_req_t *req, const char *type);
esp_err_t httpd_resp_send(httpd_req_t *req, const char *buf, ssize_t len);
esp_err_t httpd_resp_send_500(httpd_req_t *req);
const char *http_method_str(int method);
