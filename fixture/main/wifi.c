/*
 * forgefixture wifi (see wifi.h).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "wifi.h"

#include <string.h>

#include "esp_event.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mdns.h"

static const char *TAG = "wifi";

static esp_netif_t *s_netif;
static volatile bool s_connected;
static esp_ip4_addr_t s_ip;
static char s_hostname[32];

static void on_wifi(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)data;
    if (id == WIFI_EVENT_STA_START) {
        esp_wifi_connect();
    } else if (id == WIFI_EVENT_STA_DISCONNECTED) {
        const wifi_event_sta_disconnected_t *d = data;
        s_connected = false;
        ESP_LOGW(TAG, "disconnected (reason %d), reconnecting", d ? d->reason : -1);
        /* A short pause keeps a wrong passphrase from becoming a storm;
         * the event loop task tolerates it. */
        vTaskDelay(pdMS_TO_TICKS(1000));
        esp_wifi_connect();
    }
}

static void on_ip(void *arg, esp_event_base_t base, int32_t id, void *data)
{
    (void)arg;
    (void)base;
    if (id == IP_EVENT_STA_GOT_IP) {
        const ip_event_got_ip_t *ev = data;
        s_ip = ev->ip_info.ip;
        s_connected = true;
        ESP_LOGI(TAG, "joined: " IPSTR " as %s.local", IP2STR(&ev->ip_info.ip), s_hostname);
    } else if (id == IP_EVENT_STA_LOST_IP) {
        s_connected = false;
        ESP_LOGW(TAG, "address lost");
    }
}

static void mdns_start(const char *hostname)
{
    ESP_ERROR_CHECK(mdns_init());
    ESP_ERROR_CHECK(mdns_hostname_set(hostname));
    ESP_ERROR_CHECK(mdns_instance_name_set("ForgeFIRM bench fixture"));
    mdns_txt_item_t txt[] = {
        {"device", "forgefixture"},
        {"channels", "lid,interlock,button"},
    };
    ESP_ERROR_CHECK(mdns_service_add(NULL, "_forgefixture", "_tcp", 80, txt, 2));
    ESP_ERROR_CHECK(mdns_service_add(NULL, "_http", "_tcp", 80, txt, 2));
}

void wifi_start(const char *hostname, const char *ssid, const char *psk)
{
    strncpy(s_hostname, hostname, sizeof(s_hostname) - 1);
    ESP_ERROR_CHECK(esp_netif_init());
    ESP_ERROR_CHECK(esp_event_loop_create_default());
    s_netif = esp_netif_create_default_wifi_sta();
    ESP_ERROR_CHECK(esp_netif_set_hostname(s_netif, hostname));   /* DHCP option 12 */

    wifi_init_config_t init = WIFI_INIT_CONFIG_DEFAULT();
    ESP_ERROR_CHECK(esp_wifi_init(&init));
    ESP_ERROR_CHECK(esp_event_handler_register(WIFI_EVENT, ESP_EVENT_ANY_ID, &on_wifi, NULL));
    ESP_ERROR_CHECK(esp_event_handler_register(IP_EVENT, ESP_EVENT_ANY_ID, &on_ip, NULL));

    wifi_config_t cfg = {0};
    strncpy((char *)cfg.sta.ssid, ssid, sizeof(cfg.sta.ssid) - 1);
    strncpy((char *)cfg.sta.password, psk, sizeof(cfg.sta.password) - 1);
    cfg.sta.threshold.authmode = psk[0] ? WIFI_AUTH_WPA2_PSK : WIFI_AUTH_OPEN;
    cfg.sta.pmf_cfg.capable = true;
    cfg.sta.pmf_cfg.required = false;
    ESP_ERROR_CHECK(esp_wifi_set_mode(WIFI_MODE_STA));
    ESP_ERROR_CHECK(esp_wifi_set_config(WIFI_IF_STA, &cfg));
    ESP_ERROR_CHECK(esp_wifi_start());
    ESP_ERROR_CHECK(esp_wifi_set_ps(WIFI_PS_NONE));

    mdns_start(hostname);
    ESP_LOGI(TAG, "joining %s", ssid);
}

void wifi_ip(char *buf, size_t n)
{
    if (s_connected)
        snprintf(buf, n, IPSTR, IP2STR(&s_ip));
    else
        snprintf(buf, n, "0.0.0.0");
}

int wifi_rssi(void)
{
    wifi_ap_record_t ap;
    if (!s_connected || esp_wifi_sta_get_ap_info(&ap) != ESP_OK)
        return 0;
    return ap.rssi;
}

bool wifi_connected(void)
{
    return s_connected;
}
