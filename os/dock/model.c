/* The dock's logic. See model.h. Plain C11 and libc only: it is built natively for the tests and cross-built for the device. */
#define _GNU_SOURCE
#include "model.h"
#include "../ui/wisp-ui.h"

#include <dirent.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

/* ------------------------------------------------------------------ small helpers */
static int lower(int c) { return (c >= 'A' && c <= 'Z') ? c + ('a' - 'A') : c; }

static int ci_eq(const char *a, const char *b) {
    for (; *a && *b; a++, b++) if (lower((unsigned char)*a) != lower((unsigned char)*b)) return 0;
    return *a == *b;
}

static int ci_eqn(const char *a, const char *b, size_t n) {
    for (size_t i = 0; i < n; i++) if (lower((unsigned char)a[i]) != lower((unsigned char)b[i])) return 0;
    return 1;
}

static int ci_cmp(const char *a, const char *b) {
    for (; *a && *b; a++, b++) {
        int d = lower((unsigned char)*a) - lower((unsigned char)*b);
        if (d) return d;
    }
    return lower((unsigned char)*a) - lower((unsigned char)*b);
}

static const char *ci_find(const char *hay, const char *needle) {
    size_t n = strlen(needle);
    if (!n) return hay;
    for (; *hay; hay++) {
        size_t i = 0;
        while (i < n && hay[i] && lower((unsigned char)hay[i]) == lower((unsigned char)needle[i])) i++;
        if (i == n) return hay;
    }
    return NULL;
}

/* Drop an incomplete UTF-8 sequence from the end of s (a string cut at a byte limit must not end in half a letter). */
static void utf8_trim_tail(char *s, size_t len) {
    size_t i = len;
    int back = 0;
    while (i > 0 && back < 3 && ((unsigned char)s[i - 1] & 0xC0) == 0x80) { i--; back++; }      /* the continuation bytes at the end */
    if (i == 0) return;
    unsigned char lead = (unsigned char)s[i - 1];
    int need = lead >= 0xF0 ? 3 : lead >= 0xE0 ? 2 : lead >= 0xC0 ? 1 : 0;                      /* continuation bytes this lead expects */
    if (lead >= 0xC0 && back < need) s[i - 1] = 0;
}

/* Copy at most cap-1 bytes of src[0..n), never cutting a UTF-8 sequence in half, and always terminate. */
static void copy(char *dst, size_t cap, const char *src, size_t n) {
    if (!cap) return;
    bool cut = n >= cap;
    if (cut) n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = 0;
    if (cut) utf8_trim_tail(dst, n);
}

static void copy_str(char *dst, size_t cap, const char *src) { copy(dst, cap, src, src ? strlen(src) : 0); }

/* The specification's string escapes (\s \n \t \r \\); any other backslash sequence is kept as written (\; is the list syntax and is read later). */
static void unescape(char *dst, size_t cap, const char *s, size_t n) {
    size_t o = 0;
    if (!cap) return;
    for (size_t i = 0; i < n && o + 1 < cap; i++) {
        char c = s[i];
        if (c == '\\' && i + 1 < n) {
            char d = s[i + 1];
            if (d == 's') { dst[o++] = ' '; i++; continue; }
            if (d == 'n') { dst[o++] = '\n'; i++; continue; }
            if (d == 't') { dst[o++] = '\t'; i++; continue; }
            if (d == 'r') { dst[o++] = '\r'; i++; continue; }
            if (d == '\\') { dst[o++] = '\\'; i++; continue; }
        }
        dst[o++] = c;
    }
    dst[o] = 0;
    if (o + 1 >= cap) utf8_trim_tail(dst, o);                                  /* it may have been cut at the limit */
}

/* Does the ';'-separated list (escaped as in the file) contain item? \; inside an item is a literal semicolon. */
static bool list_has(const char *raw, const char *item) {
    char cur[WD_NAME];
    size_t o = 0;
    for (const char *p = raw;; p++) {
        if (*p == '\\' && p[1] == ';') { if (o + 1 < sizeof cur) cur[o++] = ';'; p++; continue; }
        if (*p == ';' || !*p) {
            cur[o] = 0;
            if (o && !strcmp(cur, item)) return true;
            o = 0;
            if (!*p) break;
            continue;
        }
        if (o + 1 < sizeof cur) cur[o++] = *p;
    }
    return false;
}

static bool is_true(const char *v) { return !strcmp(v, "true"); }

/* ------------------------------------------------------------------ the desktop file */
typedef struct { char lang[24], country[24], modifier[24]; } Locale;

/* lang_COUNTRY.ENCODING@MODIFIER, the encoding ignored; "C", "POSIX" and "" mean no locale at all. */
static void locale_parse(const char *s, Locale *l) {
    memset(l, 0, sizeof *l);
    if (!s || !*s || !strcmp(s, "C") || !strcmp(s, "POSIX")) return;
    const char *p = s;
    size_t n = strcspn(p, "_.@");
    copy(l->lang, sizeof l->lang, p, n); p += n;
    if (*p == '_') { p++; n = strcspn(p, ".@"); copy(l->country, sizeof l->country, p, n); p += n; }
    if (*p == '.') { p++; p += strcspn(p, "@"); }
    if (*p == '@') { p++; copy_str(l->modifier, sizeof l->modifier, p); }
}

/* How well Name[postfix] matches the wanted locale, by the specification's order: lang_COUNTRY@MODIFIER 4, lang_COUNTRY 3, lang@MODIFIER 2, lang 1; -1 for no match. */
static int locale_rank(const Locale *want, const char *postfix, size_t n) {
    char buf[64];
    Locale have;
    copy(buf, sizeof buf, postfix, n);
    locale_parse(buf, &have);
    if (!want->lang[0] || strcmp(want->lang, have.lang)) return -1;
    if (have.country[0] && have.modifier[0]) return (want->country[0] && want->modifier[0] && !strcmp(have.country, want->country) && !strcmp(have.modifier, want->modifier)) ? 4 : -1;
    if (have.country[0]) return (want->country[0] && !strcmp(have.country, want->country)) ? 3 : -1;
    if (have.modifier[0]) return (want->modifier[0] && !strcmp(have.modifier, want->modifier)) ? 2 : -1;
    return 1;
}

static bool in_path(const char *tryexec, const char *path_env) {
    char full[WD_PATH + 64];
    if (tryexec[0] == '/') return access(tryexec, X_OK) == 0;
    for (const char *p = path_env; p && *p;) {
        size_t n = strcspn(p, ":");
        if (n && n + 1 + strlen(tryexec) < sizeof full) {
            memcpy(full, p, n); full[n] = '/'; strcpy(full + n + 1, tryexec);
            if (access(full, X_OK) == 0) return true;
        }
        p += n; if (*p == ':') p++;
    }
    return false;
}

int wd_desktop_parse(const char *text, size_t len, const char *id, const char *path, const WdLoadOpts *opt, WdApp *out) {
    if (!text || !id || !out) return WD_BAD;
    memset(out, 0, sizeof *out);
    copy_str(out->id, sizeof out->id, id);
    copy_str(out->path, sizeof out->path, path);
    const char *nul = memchr(text, 0, len);
    if (nul) len = (size_t)(nul - text);

    Locale want;
    locale_parse(opt ? opt->lang : NULL, &want);
    char only[512] = "", notin[512] = "", type[32] = "", tryexec[WD_PATH] = "";
    bool has_only = false, has_notin = false, hidden = false;
    bool set_type = false, set_exec = false, set_icon = false, set_hidden = false, set_nodisplay = false, set_only = false, set_notin = false, set_try = false, set_term = false, set_class = false;
    int name_rank = -1;
    bool in_entry = false, seen_entry = false;

    for (size_t pos = 0; pos < len;) {
        size_t ls = pos, le = ls;
        while (le < len && text[le] != '\n') le++;
        pos = le + 1;
        while (le > ls && (text[le - 1] == '\r' || text[le - 1] == ' ' || text[le - 1] == '\t')) le--;
        while (ls < le && (text[ls] == ' ' || text[ls] == '\t')) ls++;
        if (ls >= le || text[ls] == '#') continue;
        if (text[ls] == '[') {
            in_entry = (le - ls == 15 && !memcmp(text + ls, "[Desktop Entry]", 15)) && !seen_entry;
            if (in_entry) seen_entry = true;
            continue;
        }
        if (!in_entry) continue;
        const char *eq = memchr(text + ls, '=', le - ls);
        if (!eq) continue;
        size_t ke = (size_t)(eq - text);
        size_t vs = ke + 1;
        while (ke > ls && (text[ke - 1] == ' ' || text[ke - 1] == '\t')) ke--;
        while (vs < le && (text[vs] == ' ' || text[vs] == '\t')) vs++;
        const char *key = text + ls, *val = text + vs;
        size_t kn = ke - ls, vn = le - vs;
        const char *br = memchr(key, '[', kn);
        size_t base = br ? (size_t)(br - key) : kn;
        bool localised = br != NULL;
        const char *post = br ? br + 1 : NULL;
        size_t postn = 0;
        if (br) { const char *close = memchr(post, ']', (size_t)(key + kn - post)); postn = close ? (size_t)(close - post) : 0; if (!close) continue; }

#define IS(k) (base == sizeof(k) - 1 && !memcmp(key, k, sizeof(k) - 1))
        if (IS("Name")) {
            int r = localised ? locale_rank(&want, post, postn) : 0;
            if (r > name_rank) { name_rank = r; unescape(out->name, sizeof out->name, val, vn); }
        } else if (localised) {
            continue;                               /* only Name is localised here */
        } else if (IS("Type") && !set_type) { set_type = true; unescape(type, sizeof type, val, vn); }
        else if (IS("Exec") && !set_exec) { set_exec = true; unescape(out->exec, sizeof out->exec, val, vn); }
        else if (IS("TryExec") && !set_try) { set_try = true; unescape(tryexec, sizeof tryexec, val, vn); }
        else if (IS("Icon") && !set_icon) { set_icon = true; unescape(out->icon, sizeof out->icon, val, vn); }
        else if (IS("StartupWMClass") && !set_class) { set_class = true; unescape(out->wm_class, sizeof out->wm_class, val, vn); }
        else if (IS("Hidden") && !set_hidden) { char b[8]; set_hidden = true; copy(b, sizeof b, val, vn); hidden = is_true(b); }
        else if (IS("NoDisplay") && !set_nodisplay) { char b[8]; set_nodisplay = true; copy(b, sizeof b, val, vn); out->nodisplay = is_true(b); }
        else if (IS("Terminal") && !set_term) { char b[8]; set_term = true; copy(b, sizeof b, val, vn); out->terminal = is_true(b); }
        else if (IS("OnlyShowIn") && !set_only) { set_only = true; has_only = true; copy(only, sizeof only, val, vn); }
        else if (IS("NotShowIn") && !set_notin) { set_notin = true; has_notin = true; copy(notin, sizeof notin, val, vn); }
#undef IS
    }

    if (strcmp(type, "Application")) return WD_NOT_APP;
    if (hidden) return WD_HIDDEN;
    if (name_rank < 0 || !out->name[0] || !out->exec[0]) return WD_INCOMPLETE;

    /* OnlyShowIn / NotShowIn against the current desktops, in order: the first name found in either list decides; otherwise shown unless OnlyShowIn exists. */
    bool shown = !has_only;
    if (opt && opt->desktops) {
        for (const char *p = opt->desktops; *p;) {
            char d[WD_NAME];
            size_t n = strcspn(p, ":");
            copy(d, sizeof d, p, n);
            if (has_only && list_has(only, d)) { shown = true; break; }
            if (has_notin && list_has(notin, d)) { shown = false; break; }
            p += n; if (*p == ':') p++;
        }
    }
    if (!shown) return WD_NOT_SHOWN;
    if (tryexec[0] && opt && opt->path_env) {
        copy_str(out->tryexec, sizeof out->tryexec, tryexec);
        if (!in_path(tryexec, opt->path_env)) return WD_NO_TRYEXEC;
    }
    return WD_OK;
}

/* ------------------------------------------------------------------ the application list */
typedef struct { char (*v)[WD_ID]; int n, cap; } Seen;

static bool seen_has(const Seen *s, const char *id) {
    for (int i = 0; i < s->n; i++) if (!strcmp(s->v[i], id)) return true;
    return false;
}

static void seen_add(Seen *s, const char *id) {
    if (s->n == s->cap) {
        int cap = s->cap ? s->cap * 2 : 64;
        char (*nv)[WD_ID] = realloc(s->v, (size_t)cap * sizeof *nv);
        if (!nv) return;
        s->v = nv; s->cap = cap;
    }
    copy_str(s->v[s->n++], WD_ID, id);
}

static int apps_add(WdApps *a, const WdApp *app) {
    if (a->n >= WD_MAX_APPS) return -1;
    if (a->n == a->cap) {
        int cap = a->cap ? a->cap * 2 : 32;
        WdApp *nv = realloc(a->v, (size_t)cap * sizeof *nv);
        if (!nv) return -1;
        a->v = nv; a->cap = cap;
    }
    a->v[a->n++] = *app;
    return 0;
}

static void scan(WdApps *apps, Seen *seen, const char *base, const char *rel, const char *prefix, int depth, const WdLoadOpts *opt) {
    char dir[WD_PATH];
    if (rel[0]) snprintf(dir, sizeof dir, "%s/%s", base, rel); else copy_str(dir, sizeof dir, base);
    DIR *d = opendir(dir);
    if (!d) return;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char full[WD_PATH], relname[WD_PATH], idname[WD_PATH];
        if (snprintf(full, sizeof full, "%s/%s", dir, e->d_name) >= (int)sizeof full) continue;
        if (rel[0]) { if (snprintf(relname, sizeof relname, "%s/%s", rel, e->d_name) >= (int)sizeof relname) continue; }
        else copy_str(relname, sizeof relname, e->d_name);
        if (prefix[0]) { if (snprintf(idname, sizeof idname, "%s-%s", prefix, e->d_name) >= (int)sizeof idname) continue; }   /* foo/bar.desktop has the ID foo-bar.desktop */
        else copy_str(idname, sizeof idname, e->d_name);
        struct stat st;
        if (stat(full, &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) {
            if (depth < 4) scan(apps, seen, base, relname, idname, depth + 1, opt);
            continue;
        }
        size_t nl = strlen(e->d_name);
        if (!S_ISREG(st.st_mode) || nl < 9 || strcmp(e->d_name + nl - 8, ".desktop") || st.st_size > WD_MAX_FILE || strlen(idname) >= WD_ID) continue;
        if (seen_has(seen, idname)) continue;                                  /* an earlier directory already decided this ID */
        seen_add(seen, idname);
        int fd = open(full, O_RDONLY | O_CLOEXEC);
        if (fd < 0) continue;
        char *buf = malloc((size_t)st.st_size + 1);
        if (!buf) { close(fd); continue; }
        ssize_t got = read(fd, buf, (size_t)st.st_size);
        close(fd);
        if (got > 0) {
            WdApp app;
            if (wd_desktop_parse(buf, (size_t)got, idname, full, opt, &app) == WD_OK) apps_add(apps, &app);
        }
        free(buf);
    }
    closedir(d);
}

static int by_name(const void *pa, const void *pb) {
    const WdApp *a = pa, *b = pb;
    int c = ci_cmp(a->name, b->name);
    return c ? c : strcmp(a->id, b->id);
}

int wd_apps_load(WdApps *apps, const char *const *dirs, int ndirs, const WdLoadOpts *opt) {
    if (!apps || (ndirs && !dirs)) return -1;
    Seen seen = {0};
    for (int i = 0; i < ndirs; i++) scan(apps, &seen, dirs[i], "", "", 0, opt);
    free(seen.v);
    if (apps->n > 1) qsort(apps->v, (size_t)apps->n, sizeof *apps->v, by_name);
    return apps->n;
}

void wd_apps_free(WdApps *apps) {
    if (!apps) return;
    free(apps->v);
    apps->v = NULL; apps->n = apps->cap = 0;
}

int wd_app_find(const WdApps *apps, const char *id) {
    if (!apps || !id) return -1;
    for (int i = 0; i < apps->n; i++) if (!strcmp(apps->v[i].id, id)) return i;
    return -1;
}

/* The program Exec starts, lower-cased: its first word that is not env or NAME=value, without the directory. */
static void exec_program(const char *exec, char *out, size_t cap) {
    const char *p = exec;
    out[0] = 0;
    while (*p) {
        while (*p == ' ' || *p == '\t') p++;
        const char *s = p;
        while (*p && *p != ' ' && *p != '\t') p++;
        size_t n = (size_t)(p - s);
        if (!n) break;
        char w[WD_PATH];
        copy(w, sizeof w, s, n);
        if (!strcmp(w, "env") || strchr(w, '=')) continue;
        const char *slash = strrchr(w, '/');
        const char *b = slash ? slash + 1 : w;
        size_t o = 0;
        for (; b[o] && o + 1 < cap; o++) out[o] = (char)lower((unsigned char)b[o]);
        out[o] = 0;
        return;
    }
}

int wd_app_for_window(const WdApps *apps, const char *app_id) {
    if (!apps || !app_id || !app_id[0]) return -1;
    for (int i = 0; i < apps->n; i++) {                                        /* the desktop file ID, with or without its suffix */
        const char *id = apps->v[i].id;
        size_t n = strlen(id);
        if (ci_eq(app_id, id)) return i;
        if (n > 8 && !strcmp(id + n - 8, ".desktop") && strlen(app_id) == n - 8 && ci_eqn(app_id, id, n - 8)) return i;
    }
    for (int i = 0; i < apps->n; i++) if (apps->v[i].wm_class[0] && ci_eq(app_id, apps->v[i].wm_class)) return i;
    char want[WD_ID];
    size_t o = 0;
    for (; app_id[o] && o + 1 < sizeof want; o++) want[o] = (char)lower((unsigned char)app_id[o]);
    want[o] = 0;
    for (int i = 0; i < apps->n; i++) {
        char prog[WD_PATH];
        exec_program(apps->v[i].exec, prog, sizeof prog);
        if (prog[0] && !strcmp(prog, want)) return i;
    }
    return -1;
}

/* ------------------------------------------------------------------ Exec to argv */
typedef struct { char *buf; size_t cap, used; char **argv; int max, argc; } Args;

static bool arg_push(Args *a, const char *s, size_t n) {
    if (a->argc + 1 >= a->max || a->used + n + 1 > a->cap) return false;
    memcpy(a->buf + a->used, s, n);
    a->buf[a->used + n] = 0;
    a->argv[a->argc++] = a->buf + a->used;
    a->used += n + 1;
    return true;
}

int wd_exec_argv(const WdApp *app, const char *terminal, char *buf, size_t cap, char **argv, int maxargs) {
    Args a = {buf, cap, 0, argv, maxargs, 0};
    if (!app || !buf || !argv || maxargs < 2) return 0;
    if (app->terminal && terminal && *terminal && !arg_push(&a, terminal, strlen(terminal))) return 0;
    const char *s = app->exec;
    while (*s) {
        while (*s == ' ' || *s == '\t') s++;
        if (!*s) break;
        if (s[0] == '%' && s[1] == 'i' && (!s[2] || s[2] == ' ' || s[2] == '\t')) {        /* %i, alone: --icon and the icon (nothing without one) */
            if (app->icon[0] && (!arg_push(&a, "--icon", 6) || !arg_push(&a, app->icon, strlen(app->icon)))) return 0;
            s += 2;
            continue;
        }
        char *arg = a.buf + a.used;
        size_t len = 0;
        bool inq = false, ins = false, quoted = false;
        while (*s && (inq || ins || (*s != ' ' && *s != '\t'))) {
            char c = *s;
            const char *ins_s = NULL;
            size_t ins_n = 0;
            if (ins) {
                if (c == '\'') { ins = false; s++; continue; }
                if (a.used + len + 2 > a.cap) return 0;
                arg[len++] = c; s++; continue;
            }
            if (inq && c == '"') { inq = false; s++; continue; }
            if (inq && c == '\\' && s[1] && strchr("\"`$\\", s[1])) { ins_s = s + 1; ins_n = 1; s += 2; }
            else if (!inq && c == '"') { inq = true; quoted = true; s++; continue; }
            else if (!inq && c == '\'') { ins = true; quoted = true; s++; continue; }
            else if (!inq && c == '\\' && s[1]) { ins_s = s + 1; ins_n = 1; s += 2; }       /* outside quotes the backslash escapes the next character, as GLib reads it */
            else if (c == '%') {
                char n = s[1];
                s += n ? 2 : 1;
                if (!n) return 0;
                if (n == '%') { ins_s = "%"; ins_n = 1; }
                else if (strchr("fFuUdDnNvmi", n)) { /* nothing is opened: the code vanishes */ }
                else if (n == 'c') { ins_s = app->name; ins_n = strlen(app->name); }
                else if (n == 'k') { ins_s = app->path; ins_n = strlen(app->path); }
                else return 0;                                                              /* an unknown field code makes the whole line invalid */
            } else { ins_s = s; ins_n = 1; s++; }
            if (ins_n) {
                if (a.used + len + ins_n + 1 > a.cap) return 0;
                memcpy(arg + len, ins_s, ins_n); len += ins_n;
            }
        }
        if (inq || ins) return 0;                                                           /* an unterminated quote */
        if (len || quoted) {
            if (a.argc + 1 >= a.max || a.used + len + 1 > a.cap) return 0;
            arg[len] = 0;
            a.argv[a.argc++] = arg;
            a.used += len + 1;
        }
    }
    if (!a.argc) return 0;
    a.argv[a.argc] = NULL;
    return a.argc;
}

/* ------------------------------------------------------------------ icons and tiles */
static bool is_png_file(const char *path) {
    struct stat st;
    size_t n = strlen(path);
    return n > 4 && ci_eq(path + n - 4, ".png") && stat(path, &st) == 0 && S_ISREG(st.st_mode);
}

bool wd_icon_find(const char *icon, int want, const char *const *roots, int nroots, char *out, size_t cap) {
    static const int sizes[] = {16, 22, 24, 32, 36, 48, 64, 72, 96, 128, 192, 256};
    if (!icon || !icon[0] || !out || !cap) return false;
    if (icon[0] == '/') {
        if (!is_png_file(icon) || strlen(icon) >= cap) return false;
        copy_str(out, cap, icon);
        return true;
    }
    if (strchr(icon, '/') || strstr(icon, "..")) return false;                        /* a name, never a path into somewhere else */
    char name[WD_ICON];
    copy_str(name, sizeof name, icon);
    size_t nl = strlen(name);
    if (nl > 4 && ci_eq(name + nl - 4, ".png")) name[nl - 4] = 0;
    int order[12], cnt = (int)(sizeof sizes / sizeof sizes[0]);
    for (int i = 0; i < cnt; i++) order[i] = sizes[i];
    for (int i = 1; i < cnt; i++) {                                                   /* nearest to `want` first; a larger one before a smaller one at equal distance */
        int v = order[i], j = i - 1;
        int dv = v > want ? v - want : want - v;
        while (j >= 0) {
            int dj = order[j] > want ? order[j] - want : want - order[j];
            if (dj > dv || (dj == dv && order[j] < v)) { order[j + 1] = order[j]; j--; } else break;
        }
        order[j + 1] = v;
    }
    char path[WD_PATH];
    for (int r = 0; r < nroots; r++) {
        for (int i = 0; i < cnt; i++) {
            if (snprintf(path, sizeof path, "%s/%dx%d/apps/%s.png", roots[r], order[i], order[i], name) < (int)sizeof path && is_png_file(path) && strlen(path) < cap) {
                copy_str(out, cap, path);
                return true;
            }
        }
        if (snprintf(path, sizeof path, "%s/%s.png", roots[r], name) < (int)sizeof path && is_png_file(path) && strlen(path) < cap) {
            copy_str(out, cap, path);
            return true;
        }
    }
    return false;
}

char wd_tile_letter(const char *name) {
    for (int i = 0; name && name[i] && i < 8; i++) {
        unsigned char c = (unsigned char)name[i];
        if (c >= 'a' && c <= 'z') return (char)(c - 32);
        if ((c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9')) return (char)c;
    }
    return '?';
}

const char *wd_glyph_name(const char *icon) {
    static const char *const known[] = { "apps", "browser", "terminal", "settings", "game", "images", "monitor", "about", "folder" };
    if (!icon || strncmp(icon, "wisp-", 5)) return NULL;
    for (size_t i = 0; i < sizeof known / sizeof *known; i++) if (!strcmp(icon + 5, known[i])) return known[i];
    return NULL;
}

int wd_tile_colour(const char *name) {
    unsigned h = 5381;
    for (; name && *name; name++) h = h * 33u + (unsigned)lower((unsigned char)*name);
    return (int)(h % 4u);
}

/* ------------------------------------------------------------------ pins */
int wd_pins_find(const WdPins *p, const char *id) {
    for (int i = 0; p && id && i < p->n; i++) if (!strcmp(p->id[i], id)) return i;
    return -1;
}

int wd_pins_parse(const char *text, WdPins *p) {
    p->n = 0;
    for (const char *s = text; s && *s;) {
        const char *e = s + strcspn(s, "\n");
        const char *a = s, *b = e;
        while (a < b && (*a == ' ' || *a == '\t' || *a == '\r')) a++;
        while (b > a && (b[-1] == ' ' || b[-1] == '\t' || b[-1] == '\r')) b--;
        if (b > a && *a != '#' && (size_t)(b - a) < WD_ID && p->n < WD_MAX_PINS) {
            char id[WD_ID];
            copy(id, sizeof id, a, (size_t)(b - a));
            if (wd_pins_find(p, id) < 0) copy_str(p->id[p->n++], WD_ID, id);
        }
        s = *e ? e + 1 : e;
    }
    return p->n;
}

int wd_pins_format(const WdPins *p, char *out, size_t cap) {
    int o = snprintf(out, cap, "# WispOS dock: one desktop file ID per line, in order\n");
    if (o < 0 || (size_t)o >= cap) return -1;
    for (int i = 0; i < p->n; i++) {
        int w = snprintf(out + o, cap - (size_t)o, "%s\n", p->id[i]);
        if (w < 0 || (size_t)w >= cap - (size_t)o) return -1;
        o += w;
    }
    return o;
}

int wd_pins_toggle(WdPins *p, const char *id) {
    int i = wd_pins_find(p, id);
    if (i >= 0) {
        for (int j = i; j + 1 < p->n; j++) memcpy(p->id[j], p->id[j + 1], WD_ID);
        p->n--;
        return 0;
    }
    if (p->n >= WD_MAX_PINS || !id || !id[0] || strlen(id) >= WD_ID) return -1;
    copy_str(p->id[p->n++], WD_ID, id);
    return 1;
}

int wd_pins_move(WdPins *p, const char *id, int delta) {
    int i = wd_pins_find(p, id);
    if (i < 0) return -1;
    int j = i + (delta < 0 ? -1 : delta > 0 ? 1 : 0);
    if (j < 0 || j >= p->n || j == i) return i;
    char tmp[WD_ID];
    memcpy(tmp, p->id[i], WD_ID); memcpy(p->id[i], p->id[j], WD_ID); memcpy(p->id[j], tmp, WD_ID);
    return j;
}

int wd_pins_seed(WdPins *p, const WdApps *apps, const char *const *preferred, int n) {
    for (int i = 0; i < n; i++)
        if (wd_app_find(apps, preferred[i]) >= 0 && wd_pins_find(p, preferred[i]) < 0 && p->n < WD_MAX_PINS) copy_str(p->id[p->n++], WD_ID, preferred[i]);
    return p->n;
}

int wd_pins_load(WdPins *p, const char *path) {
    char buf[8192];
    p->n = 0;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd < 0) return errno == ENOENT ? 0 : -1;
    ssize_t n = read(fd, buf, sizeof buf - 1);
    close(fd);
    if (n < 0) return -1;
    buf[n] = 0;
    wd_pins_parse(buf, p);
    return 1;
}

int wd_pins_save(const WdPins *p, const char *path) {
    char want[WD_MAX_PINS * WD_ID + 128], have[sizeof want];
    int len = wd_pins_format(p, want, sizeof want);
    if (len < 0) return -1;
    int fd = open(path, O_RDONLY | O_CLOEXEC);
    if (fd >= 0) {
        ssize_t n = read(fd, have, sizeof have);
        close(fd);
        if (n == len && !memcmp(have, want, (size_t)len)) return 1;                  /* nothing changed: the card is not touched */
    }
    char tmp[WD_PATH + 8], dir[WD_PATH];
    if (snprintf(tmp, sizeof tmp, "%s.tmp", path) >= (int)sizeof tmp) return -1;
    copy_str(dir, sizeof dir, path);
    char *slash = strrchr(dir, '/');
    if (slash) {                                                                      /* the directory and its parents, as mkdir -p */
        *slash = 0;
        for (char *q = dir + 1; *q; q++) {
            if (*q != '/') continue;
            *q = 0; mkdir(dir, 0755); *q = '/';
        }
        if (dir[0]) mkdir(dir, 0755);
    }
    fd = open(tmp, O_WRONLY | O_CREAT | O_TRUNC | O_CLOEXEC, 0644);
    if (fd < 0) return -1;
    ssize_t w = write(fd, want, (size_t)len);
    int ok = w == len && fsync(fd) == 0;
    close(fd);
    if (!ok || rename(tmp, path) != 0) { unlink(tmp); return -1; }
    return 0;
}

/* ------------------------------------------------------------------ windows */
void wd_windows_update(WdWindows *ws, unsigned long id, const char *app_id, const char *title, int activated) {
    int i = 0;
    while (i < ws->n && ws->w[i].id != id) i++;
    if (i == ws->n) {
        if (ws->n >= WD_MAX_WINDOWS) return;
        memset(&ws->w[i], 0, sizeof ws->w[i]);
        ws->w[i].id = id;
        ws->n++;
    }
    if (app_id) copy_str(ws->w[i].app_id, sizeof ws->w[i].app_id, app_id);
    if (title) copy_str(ws->w[i].title, sizeof ws->w[i].title, title);
    if (activated == 1) { for (int j = 0; j < ws->n; j++) ws->w[j].activated = false; ws->w[i].activated = true; }
    else if (activated == 0) ws->w[i].activated = false;
}

void wd_windows_remove(WdWindows *ws, unsigned long id) {
    for (int i = 0; i < ws->n; i++) {
        if (ws->w[i].id == id) {
            for (int j = i; j + 1 < ws->n; j++) ws->w[j] = ws->w[j + 1];
            ws->n--;
            return;
        }
    }
}

/* ------------------------------------------------------------------ the dock */
static WdItem *add_item(WdItem *out, int cap, int *n, WdKind kind, const char *key, const char *label) {
    if (*n >= cap) return NULL;
    WdItem *it = &out[(*n)++];
    memset(it, 0, sizeof *it);
    it->kind = kind;
    it->app = -1;
    copy_str(it->key, sizeof it->key, key);
    copy_str(it->label, sizeof it->label, label);
    return it;
}

int wd_dock_build(const WdApps *apps, const WdPins *pins, const WdWindows *ws, WdItem *out, int cap) {
    int n = 0;
    int matched[WD_MAX_WINDOWS];
    for (int i = 0; i < ws->n; i++) matched[i] = wd_app_for_window(apps, ws->w[i].app_id);
    add_item(out, cap, &n, WD_APPS, "apps", "Applications");
    add_item(out, cap, &n, WD_SEP, "", "");
    for (int i = 0; i < pins->n; i++) {
        int ai = wd_app_find(apps, pins->id[i]);
        if (ai < 0) continue;                                                      /* a pin whose application is gone is not shown (and not forgotten) */
        WdItem *it = add_item(out, cap, &n, WD_PIN, pins->id[i], apps->v[ai].name);
        if (!it) break;
        copy_str(it->icon, sizeof it->icon, apps->v[ai].icon);
        it->app = ai;
        for (int w = 0; w < ws->n; w++) {
            if (matched[w] != ai) continue;
            if (!it->nwin || (ws->w[w].activated && !it->active)) it->window = ws->w[w].id;
            it->nwin++;
            if (ws->w[w].activated) it->active = true;
        }
        it->running = it->nwin > 0;
    }
    bool first_other = true;
    for (int w = 0; w < ws->n; w++) {
        if (matched[w] >= 0 && wd_pins_find(pins, apps->v[matched[w]].id) >= 0) continue;   /* folded into its pin */
        if (first_other) { add_item(out, cap, &n, WD_SEP, "", ""); first_other = false; }
        char key[WD_ID];
        snprintf(key, sizeof key, "w%lu", ws->w[w].id);
        const char *label = matched[w] >= 0 ? apps->v[matched[w]].name : ws->w[w].title[0] ? ws->w[w].title : ws->w[w].app_id;
        WdItem *it = add_item(out, cap, &n, WD_WINDOW, key, label);
        if (!it) break;
        if (matched[w] >= 0) { copy_str(it->icon, sizeof it->icon, apps->v[matched[w]].icon); it->app = matched[w]; }
        it->nwin = 1; it->window = ws->w[w].id; it->running = true; it->active = ws->w[w].activated;
    }
    add_item(out, cap, &n, WD_GAME, "game", "Game mode");
    add_item(out, cap, &n, WD_SEP, "", "");
    add_item(out, cap, &n, WD_STATUS, "status", "Quick settings");
    return n;
}

void wd_dock_layout(WdItem *items, int n, int width, int clock_w) {
    const int tile = WISP_UI_DOCK_TILE, pitch = WISP_UI_DOCK_PITCH, inset = WISP_UI_DOCK_INSET, st = WISP_UI_DOCK_STATUS;
    const int ty = (WISP_UI_DOCK_H - tile) / 2 + 1, sep_y = 11, sep_h = WISP_UI_DOCK_H - 20;
    int x = inset, right = n;
    for (int i = 0; i < n; i++) if (items[i].kind == WD_GAME) { right = i; break; }
    for (int i = 0; i < right; i++) {                                                  /* the left cluster: tiles on the pitch, a divider is a line in the gap and 4 px more */
        if (items[i].kind == WD_SEP) { items[i].x = x - 4; items[i].y = sep_y; items[i].w = 1; items[i].h = sep_h; x += 4; continue; }
        items[i].x = x; items[i].y = ty; items[i].w = tile; items[i].h = tile;
        x += pitch;
    }
    int group_w = 10 + 3 * st + 2 * 8 + 12 + clock_w + 10;                            /* the status group: three glyphs and the clock, one item */
    int gx1 = width - inset + 2, gx0 = gx1 - group_w;
    for (int i = right; i < n; i++) {
        if (items[i].kind == WD_STATUS) { items[i].x = gx0; items[i].y = ty; items[i].w = group_w; items[i].h = tile; }
        else if (items[i].kind == WD_SEP) { items[i].x = gx0 - 7; items[i].y = sep_y; items[i].w = 1; items[i].h = sep_h; }
        else { items[i].x = gx0 - 7 - 5 - tile; items[i].y = ty; items[i].w = tile; items[i].h = tile; }      /* Game mode */
    }
}

int wd_dock_hit(const WdItem *items, int n, int x, int y) {
    for (int i = 0; i < n; i++) {
        if (items[i].kind == WD_SEP) continue;
        if (x >= items[i].x && x < items[i].x + items[i].w && y >= items[i].y && y < items[i].y + items[i].h) return i;
    }
    return -1;
}

int wd_dock_move(const WdItem *items, int n, int cur, int dir) {
    int step = dir < 0 ? -1 : 1;
    for (int i = cur + step; i >= 0 && i < n; i += step) if (items[i].kind != WD_SEP) return i;
    return cur;
}

int wd_dock_first_focus(const WdItem *items, int n) {
    int first_pin = -1;
    for (int i = 0; i < n; i++) {
        if ((items[i].kind == WD_PIN || items[i].kind == WD_WINDOW) && items[i].active) return i;
        if (items[i].kind == WD_PIN && first_pin < 0) first_pin = i;
    }
    return first_pin >= 0 ? first_pin : 0;
}

int wd_item_actions(const WdItem *it, const WdPins *pins, WdAction *out) {
    int n = 0;
    if (it->kind == WD_PIN) {
        int idx = wd_pins_find(pins, it->key);
        out[n++] = WD_ACT_OPEN;
        out[n++] = WD_ACT_UNPIN;
        if (idx > 0) out[n++] = WD_ACT_LEFT;
        if (idx >= 0 && idx < pins->n - 1) out[n++] = WD_ACT_RIGHT;
        if (it->running) out[n++] = WD_ACT_CLOSE;
    } else if (it->kind == WD_WINDOW) {
        out[n++] = WD_ACT_OPEN;
        if (it->app >= 0 && pins->n < WD_MAX_PINS) out[n++] = WD_ACT_PIN;              /* only an application with a desktop file can be pinned */
        out[n++] = WD_ACT_CLOSE;
    }
    return n;
}

const char *wd_action_label(WdAction a) {
    switch (a) {
    case WD_ACT_OPEN: return "Open";
    case WD_ACT_PIN: return "Pin to dock";
    case WD_ACT_UNPIN: return "Unpin from dock";
    case WD_ACT_LEFT: return "Move left";
    case WD_ACT_RIGHT: return "Move right";
    case WD_ACT_CLOSE: return "Close window";
    }
    return "";
}

/* ------------------------------------------------------------------ the application menu */
int wd_menu_list(const WdApps *apps, const char *query, int *out, int cap) {
    int n = 0;
    for (int i = 0; i < apps->n && n < cap; i++) {
        if (apps->v[i].nodisplay) continue;
        if (query && query[0] && !ci_find(apps->v[i].name, query)) continue;
        out[n++] = i;
    }
    return n;
}

int wd_menu_move(int cur, int count, int cols, int dir) {
    if (count <= 0 || cols <= 0) return 0;
    if (cur < 0) cur = 0;
    if (cur >= count) cur = count - 1;
    int rows = (count + cols - 1) / cols, r = cur / cols;
    switch (dir) {
    case 0: if (r > 0) cur -= cols; break;
    case 1: if (r < rows - 1) { cur += cols; if (cur >= count) cur = count - 1; } break;
    case 2: if (cur > 0) cur--; break;
    case 3: if (cur < count - 1) cur++; break;
    default: break;
    }
    return cur;
}

int wd_menu_top_row(int cur, int cols, int rows_visible, int top_row) {
    if (cols <= 0 || rows_visible <= 0) return 0;
    int row = cur / cols;
    if (row < top_row) top_row = row;
    else if (row >= top_row + rows_visible) top_row = row - rows_visible + 1;
    return top_row < 0 ? 0 : top_row;
}
