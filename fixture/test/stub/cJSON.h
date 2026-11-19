/*
 * Host stand-in for cJSON: the calls api.c makes, over a flat object model
 * (stub/cJSON.c). Parse reads one flat object of string and number members;
 * Print writes an object's own string and number members, which is what the
 * host test reads.
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#pragma once

#include <stdbool.h>

enum { CJSON_OBJECT, CJSON_STRING, CJSON_NUMBER, CJSON_BOOL };

typedef struct cJSON {
    struct cJSON *next, *child;
    int type;
    char *string;               /* the member's name */
    char *valuestring;
    double valuedouble;
} cJSON;

cJSON *cJSON_CreateObject(void);
cJSON *cJSON_AddStringToObject(cJSON *o, const char *name, const char *s);
cJSON *cJSON_AddNumberToObject(cJSON *o, const char *name, double n);
cJSON *cJSON_AddBoolToObject(cJSON *o, const char *name, bool b);
cJSON *cJSON_AddObjectToObject(cJSON *o, const char *name);
cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *o, const char *name);
cJSON *cJSON_Parse(const char *text);
char *cJSON_PrintUnformatted(const cJSON *o);
void cJSON_Delete(cJSON *o);
void cJSON_free(void *p);
bool cJSON_IsObject(const cJSON *o);
bool cJSON_IsString(const cJSON *o);
bool cJSON_IsNumber(const cJSON *o);
