/*
 * Host stand-in for lwIP's sockets.h: the host's own sockets, and lwIP's
 * inet_ntoa_r over the host's inet_ntoa.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <sys/socket.h>

static inline char *inet_ntoa_r(struct in_addr addr, char *buf, size_t n)
{
    snprintf(buf, n, "%s", inet_ntoa(addr));
    return buf;
}
