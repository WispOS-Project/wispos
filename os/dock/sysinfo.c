/* What the desktop shell asks the system for: see sysinfo.h. */
#define _GNU_SOURCE
#include "sysinfo.h"

#include <ctype.h>
#include <dirent.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <strings.h>
#include <time.h>

static const char *line_end(const char *s) { const char *e = strchr(s, '\n'); return e ? e : s + strlen(s); }

int sys_parse_wpctl(const char *out, float *volume, bool *muted)
{
    const char *p = strstr(out, "Volume:");
    if (!p) return 0;
    p += 7;
    char *end;
    float v = strtof(p, &end);
    if (end == p || !(v >= 0.f) || v > 10.f) return 0;                          /* the sink may go past 100 %: the dock shows 0..1 */
    *volume = v > 1.f ? 1.f : v;
    *muted = memmem(p, (size_t)(line_end(p) - p), "[MUTED]", 7) != NULL;
    return 1;
}

int sys_parse_nmcli_wifi(const char *out, char *ssid, size_t cap, int *level)
{
    int saw = 0;
    for (const char *line = out; *line; line = *line_end(line) ? line_end(line) + 1 : line_end(line)) {
        saw = 1;
        if (line[0] != '*') continue;
        char fields[3][96];
        int nf = 0;
        size_t len = 0;
        const char *p = line + 1;
        if (*p == ':') p++;
        memset(fields, 0, sizeof fields);
        for (; p < line_end(line) && nf < 3; p++) {                              /* fields are separated by ':', and a ':' inside a name is written "\:" */
            if (*p == '\\' && p + 1 < line_end(line)) { if (len < 95) fields[nf][len++] = p[1]; p++; }
            else if (*p == ':') { nf++; len = 0; }
            else if (len < 95) fields[nf][len++] = *p;
        }
        snprintf(ssid, cap, "%s", fields[0]);
        *level = sys_signal_level(atoi(fields[1]));
        return 1;
    }
    return saw ? 0 : -1;
}

void sys_parse_bluetooth(const char *out, bool *available, bool *on)
{
    *available = strstr(out, "Controller ") != NULL;
    const char *p = strstr(out, "Powered:");
    *on = *available && p && strncmp(p + 8 + strspn(p + 8, " \t"), "yes", 3) == 0;
}

void sys_parse_ssh(const char *out, bool *on, char *addr, size_t cap)
{
    *on = strncmp(out, "ssh: on", 7) == 0;
    addr[0] = 0;
    const char *p = strstr(out, "address: ");
    if (*on && p) {
        p += 9;
        size_t n = strcspn(p, " \t\r\n");
        if (n > 0 && n < 50) snprintf(addr, cap, "wisp@%.*s", (int)n, p);
    }
}

int sys_parse_usb(const char *out)
{
    if (strncmp(out, "usb: ", 5) != 0) return -1;
    const char *m = out + 5;
    if (!strncmp(m, "off", 3)) return 0;
    if (!strncmp(m, "computer", 8)) return 1;
    if (!strncmp(m, "host", 4)) return 2;
    return -1;
}

int sys_parse_screen_timeout(const char *out)
{
    while (*out == ' ' || *out == '\t') out++;
    if (!strncasecmp(out, "off", 3) && (out[3] == 0 || out[3] == '\n' || out[3] == '\r' || out[3] == ' ')) return 0;
    if (*out < '0' || *out > '9') return -1;
    long v = 0;
    while (*out >= '0' && *out <= '9') { v = v * 10 + (*out - '0'); if (v > 86400) return -1; out++; }      /* a day is the most the tool accepts */
    return (*out == 0 || *out == '\n' || *out == '\r' || *out == ' ') ? (int)v : -1;
}

int sys_parse_can_sleep(const char *out)
{
    if (strncmp(out, "sleep: ", 7) != 0) return -1;
    if (!strncmp(out + 7, "yes", 3) && (out[10] == 0 || out[10] == '\n' || out[10] == '\r')) return 1;
    if (!strncmp(out + 7, "no", 2) && (out[9] == 0 || out[9] == '\n' || out[9] == '\r')) return 0;
    return -1;
}

static bool read_text(const char *path, char *buf, size_t cap)
{
    FILE *f = fopen(path, "r");
    if (!f) return false;
    size_t n = fread(buf, 1, cap - 1, f);
    fclose(f);
    buf[n] = 0;
    while (n && (buf[n - 1] == '\n' || buf[n - 1] == ' ')) buf[--n] = 0;
    return true;
}

static int name_cmp(const void *a, const void *b) { return strcmp((const char *)a, (const char *)b); }

static int dir_first(const char *root, char *found, size_t cap, bool (*want)(const char *dir))
{
    DIR *d = opendir(root);
    if (!d) return 0;
    struct dirent *e;
    char names[32][64];
    int n = 0;
    while ((e = readdir(d)) && n < 32) if (e->d_name[0] != '.' && strlen(e->d_name) < 64) strcpy(names[n++], e->d_name);
    closedir(d);
    qsort(names, (size_t)n, sizeof names[0], name_cmp);                          /* in name order, so the answer does not depend on the directory's order */
    for (int i = 0; i < n; i++) {
        char p[512];
        snprintf(p, sizeof p, "%.400s/%.63s", root, names[i]);
        if (want(p)) { snprintf(found, cap, "%s", p); return 1; }
    }
    return 0;
}

static bool is_battery(const char *dir)
{
    char p[600], t[32];
    snprintf(p, sizeof p, "%.500s/type", dir);
    return read_text(p, t, sizeof t) && strcmp(t, "Battery") == 0;
}

static bool has_brightness(const char *dir)
{
    char p[600], t[32];
    snprintf(p, sizeof p, "%.500s/max_brightness", dir);
    return read_text(p, t, sizeof t) && atoi(t) > 0;
}

int sys_read_battery(const char *root, float *charge, bool *charging)
{
    char dir[512], p[600], t[64];
    if (!dir_first(root, dir, sizeof dir, is_battery)) return 0;
    snprintf(p, sizeof p, "%.500s/capacity", dir);
    if (!read_text(p, t, sizeof t)) return 0;
    int cap = atoi(t);
    *charge = (float)(cap < 0 ? 0 : cap > 100 ? 100 : cap) / 100.f;
    snprintf(p, sizeof p, "%.500s/status", dir);
    *charging = read_text(p, t, sizeof t) && strcmp(t, "Charging") == 0;
    return 1;
}

int sys_read_backlight(const char *root, float *level)
{
    char dir[512], p[600], t[32];
    if (!dir_first(root, dir, sizeof dir, has_brightness)) return 0;
    snprintf(p, sizeof p, "%.500s/max_brightness", dir);
    read_text(p, t, sizeof t);
    int max = atoi(t);
    snprintf(p, sizeof p, "%.500s/brightness", dir);
    if (!read_text(p, t, sizeof t) || max <= 0) return 0;
    float f = (float)atoi(t) / (float)max;
    *level = f < 0.f ? 0.f : f > 1.f ? 1.f : f;
    return 1;
}

int sys_write_backlight(const char *root, float level, float floor)
{
    char dir[512], p[600], t[32];
    if (!dir_first(root, dir, sizeof dir, has_brightness)) return 0;
    snprintf(p, sizeof p, "%.500s/max_brightness", dir);
    read_text(p, t, sizeof t);
    int max = atoi(t);
    if (level < floor) level = floor;                                          /* a black panel with no way back is not a setting */
    if (level > 1.f) level = 1.f;
    int raw = (int)lroundf(level * (float)max);
    if (raw < 1) raw = 1;
    snprintf(p, sizeof p, "%.500s/brightness", dir);
    FILE *f = fopen(p, "w");
    if (!f) return 0;
    int ok = fprintf(f, "%d\n", raw) > 0;
    return fclose(f) == 0 && ok;
}

int sys_signal_level(int percent) { return percent >= 67 ? 3 : percent >= 34 ? 2 : percent >= 1 ? 1 : 0; }

void sys_clock_text(long epoch, char *out, size_t cap)
{
    time_t t = (time_t)epoch;
    struct tm tm;
    localtime_r(&t, &tm);
    snprintf(out, cap, "%02d:%02d", tm.tm_hour, tm.tm_min);
}
