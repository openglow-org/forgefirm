/*
 * forgefixture relays, held to account on the host against a stand-in
 * GPIO, timer and mutex layer (stub/) that records every call in order:
 * cc -Istub -o relays_test relays_test.c ../main/relays.c ../main/policy.c
 * (see run.sh).
 *
 * What it holds: the last level written to every relay line before its pin
 * becomes an output is low, no relay line is written high during init, and
 * the line is low after init; the button refused without the enable jumper and while
 * a pulse is still on, with the line untouched; the pulse's end armed
 * before the line rises, the line dropped when it fires; the button never
 * held as a loop; a release that stops the pulse's timer and drops every
 * line.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include <stdio.h>
#include <string.h>

#include "../main/relays.h"
#include "driver/gpio.h"
#include "esp_timer.h"
#include "freertos/semphr.h"

static int failures;

#define CHECK(cond) do { if (!(cond)) { failures++; printf("FAIL %s:%d: %s\n", __FILE__, __LINE__, #cond); } } while (0)

/* ---- the stand-in layer: one trace of every call -------------------- */

enum kind { RESET, LEVEL, DIR, PULL, START, STOP };

static struct ev { enum kind kind; int gpio; long long value; } trace[256];
static int n_trace;
static int levels[64];          /* a pin's last written level */
static int enable_level = 1;    /* the jumper pin: 1 = no jumper (pulled up) */
static esp_timer_cb_t pulse_cb;
static void *pulse_arg;
static int lock_depth, lock_misuse;

static void note(enum kind k, int gpio, long long value)
{
    if (n_trace < (int)(sizeof(trace) / sizeof(trace[0])))
        trace[n_trace++] = (struct ev){ k, gpio, value };
}

esp_err_t gpio_reset_pin(gpio_num_t gpio) { note(RESET, gpio, 0); return ESP_OK; }
esp_err_t gpio_set_level(gpio_num_t gpio, uint32_t level)
{
    note(LEVEL, gpio, level);
    levels[gpio] = (int)level;
    return ESP_OK;
}
int gpio_get_level(gpio_num_t gpio) { return gpio == ENABLE_GPIO_BUTTON ? enable_level : levels[gpio]; }
esp_err_t gpio_set_direction(gpio_num_t gpio, gpio_mode_t mode) { note(DIR, gpio, mode); return ESP_OK; }
esp_err_t gpio_set_pull_mode(gpio_num_t gpio, gpio_pull_mode_t pull) { note(PULL, gpio, pull); return ESP_OK; }

static struct esp_timer { int unused; } the_timer;

esp_err_t esp_timer_create(const esp_timer_create_args_t *args, esp_timer_handle_t *out)
{
    pulse_cb = args->callback;
    pulse_arg = args->arg;
    *out = &the_timer;
    return ESP_OK;
}
esp_err_t esp_timer_start_once(esp_timer_handle_t timer, uint64_t timeout_us)
{
    (void)timer;
    note(START, -1, (long long)timeout_us);
    return ESP_OK;
}
esp_err_t esp_timer_stop(esp_timer_handle_t timer) { (void)timer; note(STOP, -1, 0); return ESP_OK; }

static int the_mutex;
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return &the_mutex; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t sem, TickType_t wait)
{
    (void)wait;
    if (sem != &the_mutex || lock_depth != 0)
        lock_misuse++;
    lock_depth++;
    return pdTRUE;
}
BaseType_t xSemaphoreGive(SemaphoreHandle_t sem)
{
    if (sem != &the_mutex || lock_depth != 1)
        lock_misuse++;
    lock_depth--;
    return pdTRUE;
}

/* ---- reading the trace --------------------------------------------- */

/* The place of the first event of a kind on a pin (value < 0: any value)
 * at or after `from`, or -1. */
static int find(int from, enum kind k, int gpio, long long value)
{
    for (int i = from; i < n_trace; i++)
        if (trace[i].kind == k && trace[i].gpio == gpio && (value < 0 || trace[i].value == value))
            return i;
    return -1;
}

/* The place of the last event of a kind on a pin before `before`, or -1. */
static int last_before(int before, enum kind k, int gpio)
{
    for (int i = before - 1; i >= 0; i--)
        if (trace[i].kind == k && trace[i].gpio == gpio)
            return i;
    return -1;
}

static const int RELAY_PINS[CH_COUNT] = { RELAY_GPIO_LID, RELAY_GPIO_INTERLOCK, RELAY_GPIO_BUTTON };

int main(void)
{
    /* init: the last level before output low, low after; the jumper an input with its pull-up */
    for (int i = 0; i < CH_COUNT; i++)
        levels[RELAY_PINS[i]] = 1;          /* what the pins might read before init */
    relays_init();
    int init_end = n_trace;
    for (int i = 0; i < CH_COUNT; i++) {
        int p = RELAY_PINS[i];
        int out = find(0, DIR, p, GPIO_MODE_OUTPUT);
        int set = out >= 0 ? last_before(out, LEVEL, p) : -1;   /* the level the pin drives as it becomes an output */
        CHECK(out >= 0 && set >= 0 && trace[set].value == 0);
        int high = find(0, LEVEL, p, 1);                         /* never high during init: the button's is a press */
        CHECK(high < 0 || high >= init_end);
        CHECK(levels[p] == 0);
    }
    CHECK(find(0, DIR, ENABLE_GPIO_BUTTON, GPIO_MODE_INPUT) >= 0);
    CHECK(find(0, PULL, ENABLE_GPIO_BUTTON, GPIO_PULLUP_ONLY) >= 0);
    relays_state_t st = relays_state();
    for (int i = 0; i < CH_COUNT; i++)
        CHECK(!st.energized[i]);
    CHECK(!st.button_pulsing && !st.button_enabled);

    /* no jumper: the button is refused and its line never moves */
    int mark = n_trace;
    CHECK(relays_pulse_button(200) == ESP_ERR_NOT_ALLOWED);
    CHECK(find(mark, LEVEL, RELAY_GPIO_BUTTON, -1) < 0 && find(mark, START, -1, -1) < 0);

    /* jumper in: the pulse's end is armed (in microseconds) before the line rises */
    enable_level = 0;
    mark = n_trace;
    CHECK(relays_pulse_button(200) == ESP_OK);
    int armed = find(mark, START, -1, 200000), rise = find(mark, LEVEL, RELAY_GPIO_BUTTON, 1);
    CHECK(armed >= 0 && rise >= 0 && armed < rise);
    st = relays_state();
    CHECK(st.button_enabled && st.button_pulsing && st.energized[CH_BUTTON]);

    /* a pulse still on: a second is refused, the line and the timer untouched */
    mark = n_trace;
    CHECK(relays_pulse_button(300) == ESP_ERR_INVALID_STATE);
    CHECK(n_trace == mark && levels[RELAY_GPIO_BUTTON] == 1);

    /* the end fires: the line drops and a new pulse may start */
    CHECK(pulse_cb != NULL);
    if (pulse_cb)
        pulse_cb(pulse_arg);
    st = relays_state();
    CHECK(levels[RELAY_GPIO_BUTTON] == 0 && !st.button_pulsing && !st.energized[CH_BUTTON]);
    CHECK(relays_pulse_button(200) == ESP_OK);

    /* the button is never held as a loop; the loops are */
    mark = n_trace;
    CHECK(relays_set_loop(CH_BUTTON, true) == ESP_ERR_INVALID_ARG);
    CHECK(find(mark, LEVEL, RELAY_GPIO_BUTTON, -1) < 0);
    CHECK(relays_set_loop(CH_LID, true) == ESP_OK && levels[RELAY_GPIO_LID] == 1);
    CHECK(relays_set_loop(CH_INTERLOCK, true) == ESP_OK && levels[RELAY_GPIO_INTERLOCK] == 1);
    st = relays_state();
    CHECK(st.energized[CH_LID] && st.energized[CH_INTERLOCK]);

    /* a release mid-pulse: the timer stopped before the lines drop, every line low */
    mark = n_trace;
    relays_release();
    int stop = find(mark, STOP, -1, -1);
    CHECK(stop >= 0);
    for (int i = 0; i < CH_COUNT; i++) {
        int drop = find(mark, LEVEL, RELAY_PINS[i], 0);
        CHECK(drop > stop && levels[RELAY_PINS[i]] == 0);
    }
    st = relays_state();
    for (int i = 0; i < CH_COUNT; i++)
        CHECK(!st.energized[i]);
    CHECK(!st.button_pulsing);

    /* every take of the lock had its give, never nested */
    CHECK(lock_depth == 0 && lock_misuse == 0);

    if (failures) {
        printf("relays: %d check(s) failed\n", failures);
        return 1;
    }
    printf("relays: all checks passed\n");
    return 0;
}
