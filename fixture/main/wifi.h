/*
 * forgefixture wifi: a station that joins the bench network and stays
 * joined, announces its hostname over DHCP and mDNS, and never sleeps
 * the radio (the API answers in milliseconds, not on the next beacon).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdbool.h>
#include <stddef.h>

void wifi_start(const char *hostname, const char *ssid, const char *psk);

/* The station's address as text ("0.0.0.0" while not joined). */
void wifi_ip(char *buf, size_t n);

/* Signal strength in dBm, 0 while not joined. */
int wifi_rssi(void);

bool wifi_connected(void);
