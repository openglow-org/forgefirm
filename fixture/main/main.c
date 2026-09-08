/*
 * forgefixture: the ForgeFIRM bench actuator. An ESP32-S3 on the bench
 * network drives three relays at the machine's connectors (the lid
 * loop, the interlock loop, the button) so the acceptance tool can open
 * a lid, pull an interlock or press the button without a hand in the
 * room. The firmware's whole job is to do exactly that on request, and
 * nothing at all otherwise: every line low at boot and after any reset,
 * the button only ever pulsed, every request under a key.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "esp_log.h"
#include "nvs_flash.h"

#include "api.h"
#include "fixture_config.h"
#include "relays.h"
#include "version.h"
#include "wifi.h"

static const char *TAG = "forgefixture";

void app_main(void)
{
    /* The relays first: whatever else happens at boot, the lines are low
     * before the radio or the server exist. */
    relays_init();

    esp_err_t r = nvs_flash_init();
    if (r == ESP_ERR_NVS_NO_FREE_PAGES || r == ESP_ERR_NVS_NEW_VERSION_FOUND) {
        ESP_ERROR_CHECK(nvs_flash_erase());
        r = nvs_flash_init();
    }
    ESP_ERROR_CHECK(r);

    ESP_LOGI(TAG, "forgefixture %s, hostname %s", FIXTURE_VERSION, FIXTURE_HOSTNAME);
    wifi_start(FIXTURE_HOSTNAME, FIXTURE_WIFI_SSID, FIXTURE_WIFI_PSK);
    api_start(FIXTURE_API_KEY, FIXTURE_HOSTNAME);
}
