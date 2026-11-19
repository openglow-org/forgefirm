/*
 * Host stand-in for cJSON (see cJSON.h).
 *
 * Copyright 2026 514 LLC d/b/a OpenGlow
 * Written by Scott Wiederhold
 * SPDX-License-Identifier: MIT
 */
#include "cJSON.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static char *copy(const char *s, size_t n)
{
    char *p = malloc(n + 1);
    memcpy(p, s, n);
    p[n] = '\0';
    return p;
}

cJSON *cJSON_CreateObject(void)
{
    return calloc(1, sizeof(cJSON));
}

static cJSON *add(cJSON *o, const char *name, int type)
{
    cJSON *it = cJSON_CreateObject(), **at = &o->child;
    it->type = type;
    it->string = copy(name, strlen(name));
    while (*at)
        at = &(*at)->next;
    *at = it;
    return it;
}

cJSON *cJSON_AddStringToObject(cJSON *o, const char *name, const char *s)
{
    cJSON *it = add(o, name, CJSON_STRING);
    it->valuestring = copy(s, strlen(s));
    return it;
}

cJSON *cJSON_AddNumberToObject(cJSON *o, const char *name, double n)
{
    cJSON *it = add(o, name, CJSON_NUMBER);
    it->valuedouble = n;
    return it;
}

cJSON *cJSON_AddBoolToObject(cJSON *o, const char *name, bool b)
{
    cJSON *it = add(o, name, CJSON_BOOL);
    it->valuedouble = b;
    return it;
}

cJSON *cJSON_AddObjectToObject(cJSON *o, const char *name)
{
    return add(o, name, CJSON_OBJECT);
}

cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *o, const char *name)
{
    for (cJSON *it = o ? o->child : NULL; it; it = it->next)
        if (strcmp(it->string, name) == 0)
            return it;
    return NULL;
}

/* {"name": "value" | number, ...}, no nesting, no escapes. */
cJSON *cJSON_Parse(const char *p)
{
    while (*p == ' ')
        p++;
    if (*p++ != '{')
        return NULL;
    cJSON *o = cJSON_CreateObject();
    for (;;) {
        while (*p == ' ' || *p == ',')
            p++;
        if (*p == '}')
            return o;
        const char *name = p + 1, *end = *p == '"' ? strchr(name, '"') : NULL;
        if (end == NULL || end[1] != ':')
            break;
        char *key = copy(name, (size_t)(end - name));
        for (p = end + 2; *p == ' '; p++)
            ;
        if (*p == '"' && strchr(p + 1, '"')) {
            const char *v = p + 1;
            p = strchr(v, '"');
            char *val = copy(v, (size_t)(p - v));
            cJSON_AddStringToObject(o, key, val);
            free(val);
            p++;
        } else {
            char *num_end;
            double n = strtod(p, &num_end);
            if (num_end == p) {
                free(key);
                break;
            }
            cJSON_AddNumberToObject(o, key, n);
            p = num_end;
        }
        free(key);
    }
    cJSON_Delete(o);
    return NULL;
}

char *cJSON_PrintUnformatted(const cJSON *o)
{
    char buf[512] = "{";
    for (cJSON *it = o->child; it; it = it->next) {
        if (it->type == CJSON_STRING)
            snprintf(buf + strlen(buf), sizeof(buf) - strlen(buf), "\"%s\":\"%s\",", it->string, it->valuestring);
        else if (it->type == CJSON_NUMBER)
            snprintf(buf + strlen(buf), sizeof(buf) - strlen(buf), "\"%s\":%g,", it->string, it->valuedouble);
    }
    snprintf(buf + strlen(buf), sizeof(buf) - strlen(buf), "}");
    return copy(buf, strlen(buf));
}

void cJSON_Delete(cJSON *o)
{
    while (o) {
        cJSON *next = o->next;
        cJSON_Delete(o->child);
        free(o->string);
        free(o->valuestring);
        free(o);
        o = next;
    }
}

void cJSON_free(void *p)
{
    free(p);
}

bool cJSON_IsObject(const cJSON *o)
{
    return o != NULL && o->type == CJSON_OBJECT;
}

bool cJSON_IsString(const cJSON *o)
{
    return o != NULL && o->type == CJSON_STRING;
}

bool cJSON_IsNumber(const cJSON *o)
{
    return o != NULL && o->type == CJSON_NUMBER;
}
