/*
 * forgefixture relays (see relays.h).
 *
 * The safety argument lives here: the lid and interlock contacts are
 * normally closed and the button contact normally open, so a line that
 * is low leaves the machine exactly as it is without the fixture. Every
 * line is driven low first thing at boot, after any reset, and by
 * relays_release(); the button is only ever pulsed, its end set by a
 * one-shot timer that is armed before the line goes high.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "relays.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"

static const char *TAG = "relays";

static const gpio_num_t GPIOS[CH_COUNT] = {RELAY_GPIO_LID, RELAY_GPIO_INTERLOCK, RELAY_GPIO_BUTTON};

static SemaphoreHandle_t s_lock;
static esp_timer_handle_t s_pulse_end;
static bool s_energized[CH_COUNT];
static bool s_pulsing;

static void drive(channel_t ch, bool level)
{
    gpio_set_level(GPIOS[ch], level ? 1 : 0);
    s_energized[ch] = level;
}

static void pulse_end(void *arg)
{
    (void)arg;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    drive(CH_BUTTON, false);
    s_pulsing = false;
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "button released");
}

void relays_init(void)
{
    /* Low before the pins become outputs: the DevKit's pull state at
     * reset is not a relay's idea of off. */
    for (int i = 0; i < CH_COUNT; i++) {
        gpio_reset_pin(GPIOS[i]);
        gpio_set_level(GPIOS[i], 0);
        gpio_set_direction(GPIOS[i], GPIO_MODE_OUTPUT);
        gpio_set_level(GPIOS[i], 0);
        s_energized[i] = false;
    }
    gpio_reset_pin(ENABLE_GPIO_BUTTON);
    gpio_set_direction(ENABLE_GPIO_BUTTON, GPIO_MODE_INPUT);
    gpio_set_pull_mode(ENABLE_GPIO_BUTTON, GPIO_PULLUP_ONLY);

    s_lock = xSemaphoreCreateMutex();
    const esp_timer_create_args_t args = {
        .callback = pulse_end,
        .name = "button-pulse",
    };
    ESP_ERROR_CHECK(esp_timer_create(&args, &s_pulse_end));
    ESP_LOGI(TAG, "lid GPIO%d, interlock GPIO%d, button GPIO%d (enable jumper GPIO%d): all released",
             RELAY_GPIO_LID, RELAY_GPIO_INTERLOCK, RELAY_GPIO_BUTTON, ENABLE_GPIO_BUTTON);
}

static bool button_enabled(void)
{
    return gpio_get_level(ENABLE_GPIO_BUTTON) == 0;
}

esp_err_t relays_set_loop(channel_t ch, bool energize)
{
    if (ch != CH_LID && ch != CH_INTERLOCK)
        return ESP_ERR_INVALID_ARG;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    drive(ch, energize);
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "%s %s", policy_channel_name(ch), policy_state_name(ch, energize));
    return ESP_OK;
}

esp_err_t relays_pulse_button(int ms)
{
    if (!button_enabled())
        return ESP_ERR_NOT_ALLOWED;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    if (s_pulsing) {
        xSemaphoreGive(s_lock);
        return ESP_ERR_INVALID_STATE;
    }
    s_pulsing = true;
    /* The end is armed before the line rises. */
    ESP_ERROR_CHECK(esp_timer_start_once(s_pulse_end, (uint64_t)ms * 1000ULL));
    drive(CH_BUTTON, true);
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "button pressed for %d ms", ms);
    return ESP_OK;
}

void relays_release(void)
{
    xSemaphoreTake(s_lock, portMAX_DELAY);
    esp_timer_stop(s_pulse_end);       /* harmless when not running */
    for (int i = 0; i < CH_COUNT; i++)
        drive((channel_t)i, false);
    s_pulsing = false;
    xSemaphoreGive(s_lock);
    ESP_LOGI(TAG, "all released");
}

relays_state_t relays_state(void)
{
    relays_state_t st;
    xSemaphoreTake(s_lock, portMAX_DELAY);
    for (int i = 0; i < CH_COUNT; i++)
        st.energized[i] = s_energized[i];
    st.button_pulsing = s_pulsing;
    xSemaphoreGive(s_lock);
    st.button_enabled = button_enabled();
    return st;
}
