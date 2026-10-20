/* wisp-dock: the desktop-mode shell. One program draws the dock strip, the application menu, the quick settings, the item-actions list, the on-screen keyboard and the toasts, as layer-shell
 * surfaces on top of ordinary windows, in the language of docs/WISPOS-UI-LANGUAGE.md "Desktop mode" (LOCKED). It tracks the open windows with the foreign-toplevel protocol, takes the pad's
 * commands from wisp-pad over a Unix socket and types for the on-screen keyboard through it.
 *
 * Everything it decides is in ui.c (tested without a compositor); everything it draws is in view.c and render.c (compared with the locked pictures); this file only turns Wayland surfaces,
 * input and sockets into calls to those and carries out the effects they ask for. It is a single thread around one poll() loop and uses no CPU while nothing happens.
 *
 *   wisp-dock                 run (started by the desktop session, wisp-desktop.service)
 *   wisp-dock --ctl CMD       send a command to the running one: summon menu qs settings osk actions state quit, and toast TITLE|TEXT|SECONDS (the sway key bindings and the programs it starts use
 *                             it; `state` prints one line)
 *
 * Environment (all optional; the defaults are the image's paths): WISP_DOCK_APPS (colon-separated directories of desktop files, highest priority first), WISP_DOCK_PINS, WISP_DOCK_FONTS,
 * WISP_DOCK_ICONS (colon-separated), WISP_DOCK_SOCK, WISP_PAD_SOCK, WISP_DOCK_SYSFS (a root for /sys, for tests), WISP_DOCK_BIN (a directory searched first for the tools it runs), WISP_DOCK_NO_WORKSPACES (do not give each window a workspace), WISP_DOCK_DOWNLOADS (/storage/downloads). SWAYSOCK is sway's IPC socket. */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <linux/input-event-codes.h>
#include <poll.h>
#include <signal.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <sys/inotify.h>
#include <sys/wait.h>
#include <time.h>
#include <unistd.h>

#include <wayland-client.h>

#include "cursor-shape-v1-client.h"
#include "wlr-foreign-toplevel-management-unstable-v1-client.h"
#include "wlr-layer-shell-unstable-v1-client.h"

#include "../ui/wisp-ui.h"
#include "downloads.h"
#include "swayipc.h"
#include "sysinfo.h"
#include "ui.h"

#define DEFAULT_APPS "/storage/.local/share/applications:/usr/share/applications"
#define DEFAULT_PINS "/storage/.config/wispos/dock"
#define DEFAULT_FONTS "/usr/share/fonts/liberation"
#define DEFAULT_ICONS "/usr/share/wispos/icons:/usr/share/icons/hicolor:/usr/share/pixmaps"
#define DEFAULT_SOCK_NAME "wisp-dock.sock"
#define DEFAULT_PAD_SOCK "/run/wisp-pad.sock"
#define SEED_PINS { "wisp-browser.desktop", "wisp-terminal.desktop", "wisp-settings.desktop" }
#define FAST_MS 30000                       /* battery and brightness */
#define SLOW_MS 60000                       /* Wi-Fi, volume, Bluetooth, SSH, USB */
#define OSK_H (WISP_UI_H - WISP_UI_OSK_Y)
#define KEY_REPEAT_DELAY_MS 400
#define KEY_REPEAT_RATE 25

static void note(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void note(const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    fputs("wisp-dock: ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

static int64_t now_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (int64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* ------------------------------------------------------------------ state */
typedef struct { struct wl_buffer *b; uint32_t *px; size_t size; int w, h; bool busy; } Buf;

enum { P_DOCK, P_MENU, P_OSK, P_TOAST, P_SETTINGS, P_N };

typedef struct {
    const char *name;
    struct wl_surface *surf;
    struct zwlr_layer_surface_v1 *ls;
    struct wl_callback *frame;
    Buf buf[2];
    int w, h;                       /* the size the compositor configured */
    int req_w, req_h, kb, excl;     /* what was last asked for */
    bool alive, configured, dirty, frame_pending, no_input;
} Panel;

typedef struct Top {
    struct zwlr_foreign_toplevel_handle_v1 *h;
    unsigned id;
    char app_id[WD_ID], title[WD_TITLE];
    bool activated;
    struct Top *next;
} Top;

typedef struct Job Job;
struct Job { pid_t pid; int fd; char out[4096]; size_t len; int tag; };
enum { J_VOLUME, J_WIFI, J_BT, J_SSH, J_USB, J_TIMEOUT, J_VOLSET, J_FIRE };
#define JOBS 10

static struct {
    struct wl_display *dpy;
    struct wl_registry *reg;
    struct wl_compositor *comp;
    struct wl_shm *shm;
    struct wl_seat *seat;
    struct wl_pointer *ptr;
    struct wl_keyboard *kbd;
    struct zwlr_layer_shell_v1 *lshell;
    struct zwlr_foreign_toplevel_manager_v1 *ftm;
    struct wp_cursor_shape_manager_v1 *cursor_mgr;
    struct wp_cursor_shape_device_v1 *cursor;
    Panel panels[P_N];
    int ptr_panel;
    Top *tops;
    unsigned next_id;
    bool running;

    Ui ui;
    UiViews views;
    ViewFonts fonts;
    WdApps apps;
    char pins_path[512];
    char sysfs[256];

    unsigned mods;                  /* UI_SHIFT ... as tracked from the key events */
    int rep_code;
    char rep_text[8];
    int64_t rep_next;
    int rep_delay, rep_rate;
    int wheel_acc;

    int dl_fd;                      /* inotify on the downloads folder: a finished download is offered in a toast */
    char dl_dir[256], dl_last[400];
    int64_t dl_last_t;
    int sway_fd;                    /* sway's IPC: every new window gets a workspace of its own */
    int64_t sway_retry;
    IpcBuf sway_in;
    struct { long id; int ws; } placed[64];
    int ctl_listen;
    int ctl_clients[4];
    int pad_fd;
    int64_t pad_retry;
    char pad_sock[256], ctl_path[256];
    char pad_in[256];
    size_t pad_in_len;

    Job jobs[JOBS];
    int64_t next_fast, next_slow, next_clock;
    float pending_volume;
    bool volume_pending;
    char tool_dir[256];
} S;

/* ------------------------------------------------------------------ small helpers */
static void send_pad(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void pad_connect(void);

static void send_pad(const char *fmt, ...)
{
    char line[200];
    va_list ap;
    va_start(ap, fmt);
    int n = vsnprintf(line, sizeof line - 1, fmt, ap);
    va_end(ap);
    if (n <= 0 || S.pad_fd < 0) return;
    line[n++] = '\n';
    if (send(S.pad_fd, line, (size_t)n, MSG_NOSIGNAL | MSG_DONTWAIT) != n) { close(S.pad_fd); S.pad_fd = -1; S.pad_retry = now_ms() + 1000; }
}

static void tool_path(char *out, size_t cap, const char *name)
{
    if (S.tool_dir[0]) snprintf(out, cap, "%s/%s", S.tool_dir, name);
    else snprintf(out, cap, "%s", name);
}

/* a program started on the user's behalf: two forks, so it is nobody's child and leaves no zombie */
static void spawn(char *const argv[])
{
    pid_t p = fork();
    if (p < 0) { note("fork failed: %s", strerror(errno)); return; }
    if (p == 0) {
        setsid();
        pid_t q = fork();
        if (q != 0) _exit(0);
        int null = open("/dev/null", O_RDONLY);
        if (null >= 0) { dup2(null, 0); close(null); }
        const char *home = getenv("HOME");
        if (chdir(home && *home ? home : "/") != 0) { /* the directory is only a nicety */ }
        execvp(argv[0], argv);
        _exit(127);
    }
    int st;
    waitpid(p, &st, 0);
}

static void spawn_cmd(const char *a, const char *b, const char *c)
{
    char path[300];
    tool_path(path, sizeof path, a);
    char *argv[5] = { path, (char *)b, (char *)c, NULL, NULL };
    spawn(argv);
}

/* ------------------------------------------------------------------ jobs: a command whose output we want, run without waiting for it */
static Job *job_slot(void)
{
    for (int i = 0; i < JOBS; i++) if (S.jobs[i].pid == 0) return &S.jobs[i];
    return NULL;
}

static bool job_running(int tag)
{
    for (int i = 0; i < JOBS; i++) if (S.jobs[i].pid && S.jobs[i].tag == tag) return true;
    return false;
}

static bool job_start(int tag, const char *const *args)        /* args[0] is the program; the list ends with NULL */
{
    Job *j = job_slot();
    if (!j || (tag != J_FIRE && job_running(tag))) return false;
    int pipefd[2];
    if (pipe2(pipefd, O_CLOEXEC | O_NONBLOCK) != 0) return false;
    pid_t p = fork();
    if (p < 0) { close(pipefd[0]); close(pipefd[1]); return false; }
    if (p == 0) {
        int fd = open("/dev/null", O_RDONLY);
        if (fd >= 0) { dup2(fd, 0); close(fd); }
        dup2(pipefd[1], 1);
        dup2(pipefd[1], 2);
        char path[300], *argv[10];
        tool_path(path, sizeof path, args[0]);
        int n = 0;
        argv[n++] = path;
        for (int i = 1; args[i] && n < 9; i++) argv[n++] = (char *)args[i];
        argv[n] = NULL;
        execvp(path, argv);
        _exit(127);
    }
    close(pipefd[1]);
    memset(j, 0, sizeof *j);
    j->pid = p;
    j->fd = pipefd[0];
    j->tag = tag;
    return true;
}

static void job_done(Job *j, int status);

static void job_read(Job *j)
{
    char buf[1024];
    for (;;) {
        ssize_t n = read(j->fd, buf, sizeof buf);
        if (n > 0) { size_t room = sizeof j->out - 1 - j->len, take = (size_t)n < room ? (size_t)n : room; memcpy(j->out + j->len, buf, take); j->len += take; continue; }
        if (n < 0 && (errno == EAGAIN || errno == EINTR)) return;
        break;                                                   /* EOF or an error: the command is done */
    }
    close(j->fd);
    int status = 0;
    waitpid(j->pid, &status, 0);
    j->out[j->len] = 0;
    Job copy = *j;
    memset(j, 0, sizeof *j);
    job_done(&copy, status);
}

/* ------------------------------------------------------------------ system values */
static void refresh_fast(void)
{
    char sysfs[300];
    float c, b;
    bool ch;
    snprintf(sysfs, sizeof sysfs, "%s/class/power_supply", S.sysfs);
    if (sys_read_battery(sysfs, &c, &ch)) { S.ui.sys.battery = c; S.ui.sys.charging = ch; } else S.ui.sys.battery = -1.f;
    snprintf(sysfs, sizeof sysfs, "%s/class/backlight", S.sysfs);
    if (sys_read_backlight(sysfs, &b)) S.ui.sys.brightness = b;
    S.ui.dirty |= DIRTY_DOCK;
}

static void refresh_slow(void)
{
    static const char *const volume[] = { "wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@", NULL };
    static const char *const wifi[] = { "nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list", NULL };
    static const char *const bt[] = { "bluetoothctl", "show", NULL };
    static const char *const ssh[] = { "wisp-ssh", "status", NULL };
    static const char *const usb[] = { "wisp-usb", "status", NULL };
    static const char *const timeout[] = { "wisp-screen", "timeout", "status", NULL };
    job_start(J_VOLUME, volume);
    job_start(J_WIFI, wifi);
    job_start(J_BT, bt);
    job_start(J_SSH, ssh);
    job_start(J_USB, usb);
    job_start(J_TIMEOUT, timeout);
}

static void job_done(Job *j, int status)
{
    (void)status;
    UiSys *s = &S.ui.sys;
    switch (j->tag) {
    case J_VOLUME: { float v; bool m; if (sys_parse_wpctl(j->out, &v, &m)) s->volume = m ? 0.f : v; break; }
    case J_WIFI: { char ssid[48] = ""; int lv = 0; int r = sys_parse_nmcli_wifi(j->out, ssid, sizeof ssid, &lv); if (r == 1) { snprintf(s->ssid, sizeof s->ssid, "%s", ssid); s->wifi = lv; } else if (r == 0) { s->ssid[0] = 0; s->wifi = 0; } break; }
    case J_BT: sys_parse_bluetooth(j->out, &s->bt_available, &s->bt_on); break;
    case J_SSH: sys_parse_ssh(j->out, &s->ssh_on, s->ssh_addr, sizeof s->ssh_addr); break;
    case J_USB: { int m = sys_parse_usb(j->out); if (m >= 0) s->usb = m; break; }
    case J_TIMEOUT: { int t = sys_parse_screen_timeout(j->out); if (t >= 0) s->screen_timeout = t; S.ui.dirty |= DIRTY_SETTINGS; break; }
    default: break;
    }
    S.ui.dirty |= DIRTY_DOCK;
}

static void volume_flush(void)
{
    if (!S.volume_pending) return;
    char v[16];
    snprintf(v, sizeof v, "%.2f", (double)S.pending_volume);
    const char *const args[] = { "wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", v, NULL };
    if (job_start(J_VOLSET, args)) S.volume_pending = false;               /* one at a time; the latest value waits its turn */
}

/* ------------------------------------------------------------------ icons */
typedef struct { char path[300]; RdImage *img; } IconCache;
static IconCache icon_cache[64];
static int icon_cache_next;
static char icon_roots_buf[600];
static const char *icon_roots[8];
static int n_icon_roots;

static const RdImage *icon_image(const char *path)
{
    for (int i = 0; i < 64; i++) if (icon_cache[i].img && !strcmp(icon_cache[i].path, path)) return icon_cache[i].img;
    RdImage *im = rd_png_load(path);
    if (!im) return NULL;
    IconCache *c = &icon_cache[icon_cache_next++ & 63];
    rd_image_free(c->img);
    snprintf(c->path, sizeof c->path, "%s", path);
    c->img = im;
    return im;
}

static RdColor glyph_ink(const char *g)
{
    if (!strcmp(g, "browser") || !strcmp(g, "game")) return RD_RGB(WISP_UI_ACCENT);
    if (!strcmp(g, "terminal")) return RD_RGB(WISP_UI_OK);
    if (!strcmp(g, "apps")) return RD_RGB(WISP_UI_TEXT);
    return RD_RGB(WISP_UI_GLYPH);
}

static IconSpec shell_icon(void *ctx, const WdApp *app, const char *key)
{
    (void)ctx;
    IconSpec s = { 0 };
    if (!strcmp(key, "apps") || !strcmp(key, "game")) { s.glyph = key; s.ink = glyph_ink(key); return s; }
    if (app) {
        const char *g = wd_glyph_name(app->icon);                                          /* our own set: tinted masks */
        if (g) {
            s.glyph = g;
            s.ink = glyph_ink(g);
            return s;
        }
        char path[300];
        if (app->icon[0] && wd_icon_find(app->icon, WISP_UI_MENU_ICON, icon_roots, n_icon_roots, path, sizeof path)) {
            s.image = icon_image(path);
            if (s.image) return s;
        }
        s.letter = wd_tile_letter(app->name);
        static const uint32_t pal[4] = { WISP_UI_ACCENT, WISP_UI_OK, WISP_UI_GLYPH, WISP_UI_DIM };
        s.letter_ink = RD_RGB(pal[wd_tile_colour(app->name) & 3]);
        return s;
    }
    s.letter = '?';
    s.letter_ink = RD_RGB(WISP_UI_GLYPH);
    return s;
}

/* ------------------------------------------------------------------ buffers and panels */
static void buf_release(void *d, struct wl_buffer *b) { (void)b; ((Buf *)d)->busy = false; }
static const struct wl_buffer_listener buf_listener = { buf_release };

static void buf_free(Buf *b)
{
    if (b->b) wl_buffer_destroy(b->b);
    if (b->px) munmap(b->px, b->size);
    memset(b, 0, sizeof *b);
}

static bool buf_alloc(Buf *b, int w, int h)
{
    size_t size = (size_t)w * (size_t)h * 4;
    int fd = memfd_create("wisp-dock", MFD_CLOEXEC);
    if (fd < 0 || ftruncate(fd, (off_t)size) != 0) { if (fd >= 0) close(fd); return false; }
    void *px = mmap(NULL, size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    if (px == MAP_FAILED) { close(fd); return false; }
    struct wl_shm_pool *pool = wl_shm_create_pool(S.shm, fd, (int32_t)size);
    b->b = wl_shm_pool_create_buffer(pool, 0, w, h, w * 4, WL_SHM_FORMAT_ARGB8888);
    wl_shm_pool_destroy(pool);
    close(fd);
    b->px = px;
    b->size = size;
    b->w = w;
    b->h = h;
    b->busy = false;
    wl_buffer_add_listener(b->b, &buf_listener, b);
    return true;
}

static void panel_draw(Panel *p);

static void frame_done(void *d, struct wl_callback *cb, uint32_t t)
{
    (void)t;
    Panel *p = d;
    wl_callback_destroy(cb);
    if (p->frame == cb) p->frame = NULL;
    p->frame_pending = false;
    if (p->dirty && p->alive) panel_draw(p);
}
static const struct wl_callback_listener frame_listener = { frame_done };

static void panel_draw(Panel *p)
{
    if (!p->alive || !p->configured || p->w <= 0 || p->h <= 0) return;
    if (p->frame_pending) { p->dirty = true; return; }                    /* once a frame: the next callback draws what changed meanwhile */
    Buf *b = NULL;
    for (int i = 0; i < 2 && !b; i++) if (!p->buf[i].busy) b = &p->buf[i];
    if (!b) { p->dirty = true; return; }                                  /* both are with the compositor: a release redraws */
    if (b->b && (b->w != p->w || b->h != p->h)) buf_free(b);
    if (!b->b && !buf_alloc(b, p->w, p->h)) { note("out of memory for a %dx%d buffer", p->w, p->h); return; }
    RdCanvas c = { p->w, p->h, p->w, b->px };
    rd_clear(&c, 0);
    int idx = (int)(p - S.panels);
    ui_make_views(&S.ui, &S.views);
    Vctx v = { &c, 0, 0, &S.fonts };
    switch (idx) {
    case P_DOCK: v.oy = p->h - WISP_UI_H; ui_draw_dock(&S.ui, &v, &S.views); break;
    case P_MENU: view_menu(&v, &S.views.menu); break;
    case P_OSK: v.oy = p->h - WISP_UI_H; view_osk(&v, &S.views.osk); view_osk_hints(&v); break;
    case P_TOAST: view_toast(&v, &S.views.toast); break;
    case P_SETTINGS: ui_draw_settings(&S.ui, &v, &S.views); break;
    }
    p->dirty = false;
    p->frame = wl_surface_frame(p->surf);
    wl_callback_add_listener(p->frame, &frame_listener, p);
    p->frame_pending = true;
    b->busy = true;
    wl_surface_attach(p->surf, b->b, 0, 0);
    wl_surface_damage_buffer(p->surf, 0, 0, p->w, p->h);
    wl_surface_commit(p->surf);
}

static void ls_configure(void *d, struct zwlr_layer_surface_v1 *ls, uint32_t serial, uint32_t w, uint32_t h)
{
    Panel *p = d;
    zwlr_layer_surface_v1_ack_configure(ls, serial);
    p->w = w ? (int)w : WISP_UI_W;
    p->h = h ? (int)h : p->req_h;
    if (p->w != WISP_UI_W && !p->configured) note("the output is %d px wide; the desktop shell is drawn for %d", p->w, WISP_UI_W);
    p->configured = true;
    p->dirty = true;
    panel_draw(p);
}

static void ls_closed(void *d, struct zwlr_layer_surface_v1 *ls)
{
    (void)ls;
    Panel *p = d;
    note("the compositor closed the %s surface", p->name);
    p->alive = false;                                  /* destroyed below, with its buffers, before the next frame */
}
static const struct zwlr_layer_surface_v1_listener ls_listener = { ls_configure, ls_closed };

static void panel_destroy(Panel *p)
{
    if (p->frame) wl_callback_destroy(p->frame);
    if (p->ls) zwlr_layer_surface_v1_destroy(p->ls);
    if (p->surf) wl_surface_destroy(p->surf);
    buf_free(&p->buf[0]);
    buf_free(&p->buf[1]);
    const char *name = p->name;
    memset(p, 0, sizeof *p);
    p->name = name;
}

typedef struct { uint32_t layer, anchor; int w, h, excl, kb; int margin_top, margin_right; bool no_input; } PanelCfg;

static void panel_ensure(int idx, bool want, const PanelCfg *cfg)
{
    Panel *p = &S.panels[idx];
    if (!want) { if (p->surf) panel_destroy(p); return; }
    if (p->surf && !p->alive) panel_destroy(p);                          /* the compositor took it away: start again */
    if (p->surf && p->no_input != cfg->no_input) panel_destroy(p);       /* a toast that starts or stops taking clicks is a different surface */
    if (!p->surf) {
        p->surf = wl_compositor_create_surface(S.comp);
        p->ls = zwlr_layer_shell_v1_get_layer_surface(S.lshell, p->surf, NULL, cfg->layer, p->name);
        zwlr_layer_surface_v1_add_listener(p->ls, &ls_listener, p);
        zwlr_layer_surface_v1_set_anchor(p->ls, cfg->anchor);
        zwlr_layer_surface_v1_set_size(p->ls, (uint32_t)cfg->w, (uint32_t)cfg->h);
        zwlr_layer_surface_v1_set_exclusive_zone(p->ls, cfg->excl);
        zwlr_layer_surface_v1_set_keyboard_interactivity(p->ls, (uint32_t)cfg->kb);
        zwlr_layer_surface_v1_set_margin(p->ls, cfg->margin_top, cfg->margin_right, 0, 0);
        if (cfg->no_input) { struct wl_region *r = wl_compositor_create_region(S.comp); wl_surface_set_input_region(p->surf, r); wl_region_destroy(r); }
        p->alive = true;
        p->no_input = cfg->no_input;
        p->req_w = cfg->w;
        p->req_h = cfg->h;
        p->kb = cfg->kb;
        p->excl = cfg->excl;
        p->dirty = true;
        wl_surface_commit(p->surf);                                       /* the first commit has no buffer: the compositor answers with the size */
        return;
    }
    bool changed = false;
    if (p->req_h != cfg->h || p->req_w != cfg->w) { zwlr_layer_surface_v1_set_size(p->ls, (uint32_t)cfg->w, (uint32_t)cfg->h); p->req_w = cfg->w; p->req_h = cfg->h; changed = true; }
    if (p->kb != cfg->kb) { zwlr_layer_surface_v1_set_keyboard_interactivity(p->ls, (uint32_t)cfg->kb); p->kb = cfg->kb; changed = true; }
    if (p->excl != cfg->excl) { zwlr_layer_surface_v1_set_exclusive_zone(p->ls, cfg->excl); p->excl = cfg->excl; changed = true; }
    if (changed) wl_surface_commit(p->surf);
}

/* the surfaces the state calls for */
static void sync_surfaces(void)
{
    UiGeom g = ui_geometry(&S.ui);
    PanelCfg dock = { ZWLR_LAYER_SHELL_V1_LAYER_TOP, ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, 0, g.dock_h,
                      WISP_UI_DOCK_H, g.dock_modal ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE, 0, 0, false };
    PanelCfg menu = { ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT,
                      0, 0, -1, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE, 0, 0, false };
    PanelCfg osk = { ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, ZWLR_LAYER_SURFACE_V1_ANCHOR_BOTTOM | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, 0, OSK_H, OSK_H,
                     ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE, 0, 0, false };
    PanelCfg toast = { ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT, g.toast_w, g.toast_h, 0,
                       g.toast_modal ? ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE : ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_NONE, 8, 8, !g.toast_modal };      /* an offer holds the keyboard and takes clicks; a plain toast does neither */
    panel_ensure(P_DOCK, g.dock_visible, &dock);
    panel_ensure(P_MENU, g.menu_visible, &menu);
    panel_ensure(P_OSK, g.osk_visible, &osk);
    panel_ensure(P_TOAST, g.toast_visible && g.toast_w > 0, &toast);
    PanelCfg settings = { ZWLR_LAYER_SHELL_V1_LAYER_OVERLAY, ZWLR_LAYER_SURFACE_V1_ANCHOR_TOP | ZWLR_LAYER_SURFACE_V1_ANCHOR_LEFT | ZWLR_LAYER_SURFACE_V1_ANCHOR_RIGHT,
                           0, g.settings_h, -1, ZWLR_LAYER_SURFACE_V1_KEYBOARD_INTERACTIVITY_EXCLUSIVE, 0, 0, false };           /* above the strip, which keeps its own space */
    panel_ensure(P_SETTINGS, g.settings_visible, &settings);
    if (S.ptr_panel >= 0 && !S.panels[S.ptr_panel].alive) S.ptr_panel = -1;
}

/* ------------------------------------------------------------------ foreign toplevels */
static void top_title(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, const char *t) { (void)h; snprintf(((Top *)d)->title, WD_TITLE, "%s", t); }
static void top_app_id(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, const char *a) { (void)h; snprintf(((Top *)d)->app_id, WD_ID, "%s", a); }
static void top_output(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_output *o) { (void)d; (void)h; (void)o; }
static void top_state(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, struct wl_array *st)
{
    (void)h;
    Top *t = d;
    t->activated = false;
    uint32_t *s;
    wl_array_for_each(s, st) if (*s == ZWLR_FOREIGN_TOPLEVEL_HANDLE_V1_STATE_ACTIVATED) t->activated = true;
}
static void top_done(void *d, struct zwlr_foreign_toplevel_handle_v1 *h)
{
    (void)h;
    Top *t = d;
    wd_windows_update(&S.ui.ws, t->id, t->app_id, t->title, t->activated ? 1 : 0);
    ui_windows_changed(&S.ui);
}
static void top_closed(void *d, struct zwlr_foreign_toplevel_handle_v1 *h)
{
    Top *t = d;
    wd_windows_remove(&S.ui.ws, t->id);
    for (Top **p = &S.tops; *p; p = &(*p)->next) if (*p == t) { *p = t->next; break; }
    zwlr_foreign_toplevel_handle_v1_destroy(h);
    free(t);
    ui_windows_changed(&S.ui);
}
static void top_parent(void *d, struct zwlr_foreign_toplevel_handle_v1 *h, struct zwlr_foreign_toplevel_handle_v1 *p) { (void)d; (void)h; (void)p; }
static const struct zwlr_foreign_toplevel_handle_v1_listener top_listener = { top_title, top_app_id, top_output, top_output, top_state, top_done, top_closed, top_parent };

static void ftm_toplevel(void *d, struct zwlr_foreign_toplevel_manager_v1 *m, struct zwlr_foreign_toplevel_handle_v1 *h)
{
    (void)d; (void)m;
    Top *t = calloc(1, sizeof *t);
    if (!t) return;
    t->h = h;
    t->id = ++S.next_id;
    t->next = S.tops;
    S.tops = t;
    zwlr_foreign_toplevel_handle_v1_add_listener(h, &top_listener, t);
}
static void ftm_finished(void *d, struct zwlr_foreign_toplevel_manager_v1 *m) { (void)d; (void)m; }
static const struct zwlr_foreign_toplevel_manager_v1_listener ftm_listener = { ftm_toplevel, ftm_finished };

static Top *top_by_id(unsigned id)
{
    for (Top *t = S.tops; t; t = t->next) if (t->id == id) return t;
    return NULL;
}

/* ------------------------------------------------------------------ input */
static int panel_of(struct wl_surface *s)
{
    for (int i = 0; i < P_N; i++) if (S.panels[i].alive && S.panels[i].surf == s) return i;
    return -1;
}

static UiSurf surf_of(int panel) { return panel == P_SETTINGS ? SURF_SETTINGS : panel == P_MENU ? SURF_MENU : panel == P_OSK ? SURF_OSK : panel == P_TOAST ? SURF_TOAST : SURF_DOCK; }

static void ptr_to_ui(int panel, wl_fixed_t fx, wl_fixed_t fy, UiPtr ev, int button)
{
    Panel *p = &S.panels[panel];
    int x = wl_fixed_to_int(fx), y = wl_fixed_to_int(fy) + (WISP_UI_H - p->h);          /* a surface anchored to the bottom: its top is at screen y = 480 - h */
    if (panel == P_MENU || panel == P_SETTINGS) y = wl_fixed_to_int(fy);                   /* surfaces anchored to the top */
    ui_pointer(&S.ui, surf_of(panel), x, y, ev, button);
}

static wl_fixed_t last_x, last_y;

static void ptr_enter(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s, wl_fixed_t x, wl_fixed_t y)
{
    (void)d; (void)p;
    S.ptr_panel = panel_of(s);
    if (S.cursor) wp_cursor_shape_device_v1_set_shape(S.cursor, serial, WP_CURSOR_SHAPE_DEVICE_V1_SHAPE_DEFAULT);
    last_x = x; last_y = y;
    if (S.ptr_panel >= 0) ptr_to_ui(S.ptr_panel, x, y, PTR_MOVE, 0);
}
static void ptr_leave(void *d, struct wl_pointer *p, uint32_t serial, struct wl_surface *s)
{
    (void)d; (void)p; (void)serial;
    int i = panel_of(s);
    if (i >= 0) ptr_to_ui(i, 0, 0, PTR_LEAVE, 0);
    if (S.ptr_panel == i) S.ptr_panel = -1;
}
static void ptr_motion(void *d, struct wl_pointer *p, uint32_t t, wl_fixed_t x, wl_fixed_t y)
{
    (void)d; (void)p; (void)t;
    last_x = x; last_y = y;
    if (S.ptr_panel >= 0) ptr_to_ui(S.ptr_panel, x, y, PTR_MOVE, 0);
}
static void ptr_button(void *d, struct wl_pointer *p, uint32_t serial, uint32_t t, uint32_t button, uint32_t state)
{
    (void)d; (void)p; (void)serial; (void)t;
    if (S.ptr_panel >= 0) ptr_to_ui(S.ptr_panel, last_x, last_y, state == WL_POINTER_BUTTON_STATE_PRESSED ? PTR_DOWN : PTR_UP, (int)button);
}
static void ptr_axis(void *d, struct wl_pointer *p, uint32_t t, uint32_t axis, wl_fixed_t value)
{
    (void)d; (void)p; (void)t;
    if (axis != WL_POINTER_AXIS_VERTICAL_SCROLL || S.ptr_panel < 0) return;
    S.wheel_acc += wl_fixed_to_int(value * 10) / 10;                      /* 10 units a notch; positive is down */
    int steps = S.wheel_acc / 10;
    if (steps) { S.wheel_acc -= steps * 10; ui_wheel(&S.ui, surf_of(S.ptr_panel), -steps); }
}
static void ptr_frame(void *d, struct wl_pointer *p) { (void)d; (void)p; }
static void ptr_axis_source(void *d, struct wl_pointer *p, uint32_t s) { (void)d; (void)p; (void)s; }
static void ptr_axis_stop(void *d, struct wl_pointer *p, uint32_t t, uint32_t a) { (void)d; (void)p; (void)t; (void)a; }
static void ptr_axis_discrete(void *d, struct wl_pointer *p, uint32_t a, int32_t v) { (void)d; (void)p; (void)a; (void)v; }
static const struct wl_pointer_listener ptr_listener = { .enter = ptr_enter, .leave = ptr_leave, .motion = ptr_motion, .button = ptr_button, .axis = ptr_axis, .frame = ptr_frame,
                                                          .axis_source = ptr_axis_source, .axis_stop = ptr_axis_stop, .axis_discrete = ptr_axis_discrete };

static void kbd_keymap(void *d, struct wl_keyboard *k, uint32_t fmt, int32_t fd, uint32_t size) { (void)d; (void)k; (void)fmt; (void)size; close(fd); }
static void kbd_enter(void *d, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s, struct wl_array *keys) { (void)d; (void)k; (void)serial; (void)s; (void)keys; }
static int held_mods[4];                                                  /* a modifier is held while any of its keys is */
static void kbd_leave(void *d, struct wl_keyboard *k, uint32_t serial, struct wl_surface *s) { (void)d; (void)k; (void)serial; (void)s; S.rep_code = 0; S.mods = 0; memset(held_mods, 0, sizeof held_mods); }
static unsigned mod_of(uint32_t code)
{
    switch (code) {
    case KEY_LEFTSHIFT: case KEY_RIGHTSHIFT: return UI_SHIFT;
    case KEY_LEFTCTRL: case KEY_RIGHTCTRL: return UI_CTRL;
    case KEY_LEFTALT: case KEY_RIGHTALT: return UI_ALT;
    case KEY_LEFTMETA: case KEY_RIGHTMETA: return UI_SUPER;
    default: return 0;
    }
}

static bool repeats(int code, const char *text)
{
    if (text[0]) return true;
    switch (code) {
    case KEY_LEFT: case KEY_RIGHT: case KEY_UP: case KEY_DOWN: case KEY_BACKSPACE: case KEY_TAB: case KEY_SPACE: return true;
    default: return false;
    }
}

static void kbd_key(void *d, struct wl_keyboard *k, uint32_t serial, uint32_t t, uint32_t key, uint32_t state)
{
    (void)d; (void)k; (void)serial; (void)t;
    bool down = state == WL_KEYBOARD_KEY_STATE_PRESSED;
    unsigned m = mod_of(key);
    if (m) {
        int bit = m == UI_SHIFT ? 0 : m == UI_CTRL ? 1 : m == UI_ALT ? 2 : 3;
        held_mods[bit] += down ? 1 : (held_mods[bit] > 0 ? -1 : 0);
        S.mods = (held_mods[0] > 0 ? UI_SHIFT : 0) | (held_mods[1] > 0 ? UI_CTRL : 0) | (held_mods[2] > 0 ? UI_ALT : 0) | (held_mods[3] > 0 ? UI_SUPER : 0);
        return;
    }
    if (!down) { if ((int)key == S.rep_code) S.rep_code = 0; return; }
    char text[8] = "";
    if (!(S.mods & (UI_CTRL | UI_ALT | UI_SUPER))) { char c = ui_key_char((int)key, (S.mods & UI_SHIFT) != 0); if (c) { text[0] = c; text[1] = 0; } }
    ui_key(&S.ui, (int)key, text, S.mods);
    if (repeats((int)key, text)) { S.rep_code = (int)key; snprintf(S.rep_text, sizeof S.rep_text, "%s", text); S.rep_next = now_ms() + S.rep_delay; }
    else S.rep_code = 0;
}
static void kbd_modifiers(void *d, struct wl_keyboard *k, uint32_t serial, uint32_t a, uint32_t b, uint32_t c, uint32_t g) { (void)d; (void)k; (void)serial; (void)a; (void)b; (void)c; (void)g; }
static void kbd_repeat(void *d, struct wl_keyboard *k, int32_t rate, int32_t delay)
{
    (void)d; (void)k;
    S.rep_rate = rate > 0 ? rate : 0;
    S.rep_delay = delay > 0 ? delay : KEY_REPEAT_DELAY_MS;
}
static const struct wl_keyboard_listener kbd_listener = { kbd_keymap, kbd_enter, kbd_leave, kbd_key, kbd_modifiers, kbd_repeat };

static void seat_caps(void *d, struct wl_seat *seat, uint32_t caps)
{
    (void)d;
    if ((caps & WL_SEAT_CAPABILITY_POINTER) && !S.ptr) {
        S.ptr = wl_seat_get_pointer(seat);
        wl_pointer_add_listener(S.ptr, &ptr_listener, NULL);
        if (S.cursor_mgr) S.cursor = wp_cursor_shape_manager_v1_get_pointer(S.cursor_mgr, S.ptr);
    }
    if ((caps & WL_SEAT_CAPABILITY_KEYBOARD) && !S.kbd) { S.kbd = wl_seat_get_keyboard(seat); wl_keyboard_add_listener(S.kbd, &kbd_listener, NULL); }
}
static void seat_name(void *d, struct wl_seat *s, const char *n) { (void)d; (void)s; (void)n; }
static const struct wl_seat_listener seat_listener = { seat_caps, seat_name };

static void reg_global(void *d, struct wl_registry *r, uint32_t name, const char *iface, uint32_t ver)
{
    (void)d;
    if (!strcmp(iface, wl_compositor_interface.name)) S.comp = wl_registry_bind(r, name, &wl_compositor_interface, ver < 4 ? ver : 4);
    else if (!strcmp(iface, wl_shm_interface.name)) S.shm = wl_registry_bind(r, name, &wl_shm_interface, 1);
    else if (!strcmp(iface, wl_seat_interface.name)) { S.seat = wl_registry_bind(r, name, &wl_seat_interface, ver < 5 ? ver : 5); wl_seat_add_listener(S.seat, &seat_listener, NULL); }
    else if (!strcmp(iface, zwlr_layer_shell_v1_interface.name)) S.lshell = wl_registry_bind(r, name, &zwlr_layer_shell_v1_interface, ver < 4 ? ver : 4);
    else if (!strcmp(iface, zwlr_foreign_toplevel_manager_v1_interface.name)) S.ftm = wl_registry_bind(r, name, &zwlr_foreign_toplevel_manager_v1_interface, ver < 3 ? ver : 3);
    else if (!strcmp(iface, wp_cursor_shape_manager_v1_interface.name)) S.cursor_mgr = wl_registry_bind(r, name, &wp_cursor_shape_manager_v1_interface, 1);
}
static void reg_remove(void *d, struct wl_registry *r, uint32_t name) { (void)d; (void)r; (void)name; }
static const struct wl_registry_listener reg_listener = { reg_global, reg_remove };

/* ------------------------------------------------------------------ effects */
static void launch_app(int idx)
{
    if (idx < 0 || idx >= S.apps.n) return;
    const WdApp *a = &S.apps.v[idx];
    char buf[1024];
    char *argv[64];
    int n = wd_exec_argv(a, "foot", buf, sizeof buf, argv, 63);
    if (n <= 0) { ui_toast(&S.ui, "Cannot start", a->name, 4000); return; }
    argv[n] = NULL;
    spawn(argv);
}

static void run_effects(void)
{
    UiEffect e[UI_EFFECTS];
    int n;
    while ((n = ui_take_effects(&S.ui, e, UI_EFFECTS)) > 0) {
        for (int i = 0; i < n; i++) {
            const UiEffect *f = &e[i];
            switch (f->kind) {
            case EFF_LAUNCH: launch_app(f->a); break;
            case EFF_ACTIVATE: { Top *t = top_by_id((unsigned)f->a); if (t && S.seat) zwlr_foreign_toplevel_handle_v1_activate(t->h, S.seat); break; }
            case EFF_CLOSE: { Top *t = top_by_id((unsigned)f->a); if (t) zwlr_foreign_toplevel_handle_v1_close(t->h); break; }
            case EFF_SAVE_PINS: if (wd_pins_save(&S.ui.pins, S.pins_path) < 0) note("cannot save the pins to %s: %s", S.pins_path, strerror(errno)); break;
            case EFF_GAME_MODE: spawn_cmd("wisp-session-select", "game", NULL); break;
            case EFF_TYPE: {
                char mods[24] = "";
                if (f->b) strcat(mods, " shift");
                if (f->c) strcat(mods, " ctrl");
                send_pad("key %d%s", f->a, mods);
                break;
            }
            case EFF_PAD_OSK: send_pad("osk %s", f->a ? "on" : "off"); break;
            case EFF_PAD_FOCUS: send_pad("focus %s", f->s); break;
            case EFF_SET_VOLUME: S.pending_volume = f->f; S.volume_pending = true; volume_flush(); break;
            case EFF_SET_BRIGHTNESS: { char sysfs[300]; snprintf(sysfs, sizeof sysfs, "%s/class/backlight", S.sysfs); if (!sys_write_backlight(sysfs, f->f, 0.05f)) note("cannot set the brightness"); break; }
            case EFF_SET_BLUETOOTH: { const char *const a[] = { "bluetoothctl", "power", f->a ? "on" : "off", NULL }; job_start(J_FIRE, a); S.next_slow = now_ms() + 2500; break; }
            case EFF_SET_SSH: { const char *const a[] = { "wisp-ssh", f->a ? "on" : "off", NULL }; job_start(J_FIRE, a); S.next_slow = now_ms() + 2500; break; }
            case EFF_SET_SCREEN_TIMEOUT: { char n[16]; snprintf(n, sizeof n, "%d", f->a); const char *const a[] = { "wisp-screen", "timeout", f->a > 0 ? n : "off", NULL }; job_start(J_FIRE, a); S.next_slow = now_ms() + 2500; break; }       /* as for the other settings: read it back only after it was written */
            case EFF_SET_USB: { const char *const a[] = { "wisp-usb", f->a == 0 ? "off" : f->a == 1 ? "computer" : "host", NULL }; job_start(J_FIRE, a); S.next_slow = now_ms() + 2500; break; }
            case EFF_WIFI: ui_toast(&S.ui, "Wi-Fi networks", "Choose one in Game mode: Settings, Internet.", 5000); break;
            case EFF_REFRESH: refresh_fast(); refresh_slow(); break;
            case EFF_BROWSER_ACTIONS: ui_toast(&S.ui, "Browser actions", "Not available yet.", 2500); break;
            case EFF_DOWNLOAD: spawn_cmd("wisp-download-action", f->a == DL_ACT_INSTALL ? "install" : f->a == DL_ACT_EXTRACT ? "extract" : f->a == DL_ACT_OPEN ? "open" : "none", f->s); break;
            }
        }
    }
}

/* ------------------------------------------------------------------ control socket and the pad's */
static bool sock_addr(struct sockaddr_un *a, const char *path)
{
    memset(a, 0, sizeof *a);
    a->sun_family = AF_UNIX;
    if (strlen(path) >= sizeof a->sun_path) { errno = ENAMETOOLONG; return false; }
    memcpy(a->sun_path, path, strlen(path));
    return true;
}

static int listen_unix(const char *path)
{
    struct sockaddr_un a;
    if (!sock_addr(&a, path)) return -1;
    struct stat st;
    if (lstat(path, &st) == 0 && S_ISSOCK(st.st_mode)) unlink(path);
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return -1;
    if (bind(fd, (struct sockaddr *)&a, sizeof a) < 0 || chmod(path, 0600) < 0 || listen(fd, 4) < 0) { int e = errno; close(fd); errno = e; return -1; }
    return fd;
}

static const char *dock_name(UiDock d) { return d == UI_IDLE ? "idle" : d == UI_SUMMONED ? "summoned" : d == UI_ACTIONS_OPEN ? "actions" : "quick-settings"; }

static void ctl_state(char *out, size_t cap)
{
    const Ui *u = &S.ui;
    snprintf(out, cap, "dock=%s focus=%s menu=%d osk=%d items=%d windows=%d pins=%d query=%s sel=%d qs=%d toast=%d toast_modal=%d settings=%d panels=", dock_name(u->dock), u->focus >= 0 ? u->items[u->focus].key : "-", u->menu, u->osk,
             u->n, u->ws.n, u->pins.n, u->query, u->menu ? u->msel : u->orow * 100 + u->ocol, u->qs_sel, u->toast.on, u->toast.on && u->toast.modal, u->settings);
    for (int i = 0; i < P_N; i++) {
        size_t o = strlen(out);
        if (S.panels[i].alive) snprintf(out + o, cap - o, "%s%s:%dx%d%s", o && out[o - 1] != '=' ? "," : "", S.panels[i].name, S.panels[i].w, S.panels[i].h, S.panels[i].configured ? "" : "?");
    }
    strncat(out, "\n", cap - strlen(out) - 1);
}

static void ctl_line(int fd, char *line)
{
    if (!strcmp(line, "state")) { char s[512]; ctl_state(s, sizeof s); if (send(fd, s, strlen(s), MSG_NOSIGNAL | MSG_DONTWAIT) < 0) { /* the client left */ } }
    else if (!strcmp(line, "quit")) S.running = false;
    else if (!strncmp(line, "toast ", 6)) {                                  /* toast TITLE|TEXT|SECONDS: what a program started from the dock has to say */
        char *title = line + 6, *text = strchr(title, '|'), *secs = NULL;
        if (text) { *text++ = 0; secs = strchr(text, '|'); if (secs) *secs++ = 0; }
        int sec = secs ? atoi(secs) : 4;
        ui_toast(&S.ui, title, text ? text : "", (sec < 1 ? 1 : sec > 60 ? 60 : sec) * 1000);
    }
    else if (!strncmp(line, "key ", 4)) { int code = atoi(line + 4); if (code > 0) ui_key(&S.ui, code, "", 0); }
    else if (!strncmp(line, "text ", 5)) { for (const char *p = line + 5; *p; p++) { int c; bool sh; if (ui_char_key(*p, &c, &sh)) { char t[2] = { *p, 0 }; ui_key(&S.ui, c, t, sh ? UI_SHIFT : 0); } } }
    else if (!strcmp(line, "summon") || !strcmp(line, "menu") || !strcmp(line, "qs") || !strcmp(line, "settings") || !strcmp(line, "osk") || !strcmp(line, "actions") || !strncmp(line, "nav-", 4) || !strncmp(line, "osk-", 4))
        ui_pad(&S.ui, line);
    else note("unknown command: %.40s", line);
}

static void clients_read(void)
{
    for (int i = 0; i < 4; i++) {
        if (S.ctl_clients[i] < 0) continue;
        char buf[256];
        ssize_t n = recv(S.ctl_clients[i], buf, sizeof buf - 1, MSG_DONTWAIT);
        if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(S.ctl_clients[i]); S.ctl_clients[i] = -1; continue; }
        if (n < 0) continue;
        buf[n] = 0;
        for (char *line = strtok(buf, "\n"); line; line = strtok(NULL, "\n")) ctl_line(S.ctl_clients[i], line);
    }
}

static void pad_connect(void)
{
    if (S.pad_fd >= 0 || now_ms() < S.pad_retry) return;
    S.pad_retry = now_ms() + 2000;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_NONBLOCK | SOCK_CLOEXEC, 0);
    if (fd < 0) return;
    struct sockaddr_un a;
    if (!sock_addr(&a, S.pad_sock) || connect(fd, (struct sockaddr *)&a, sizeof a) != 0) { close(fd); return; }
    S.pad_fd = fd;
    S.pad_in_len = 0;
    send_pad("focus %s", S.ui.pad_focus);                                    /* a daemon that came back learns where things stand */
    send_pad("osk %s", S.ui.osk ? "on" : "off");
}

static void pad_read(void)
{
    char buf[256];
    ssize_t n = recv(S.pad_fd, buf, sizeof buf, MSG_DONTWAIT);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(S.pad_fd); S.pad_fd = -1; S.pad_retry = now_ms() + 1000; return; }
    for (ssize_t i = 0; i < n; i++) {
        if (buf[i] == '\n') { S.pad_in[S.pad_in_len] = 0; if (S.pad_in_len) ui_pad(&S.ui, S.pad_in); S.pad_in_len = 0; }
        else if (S.pad_in_len < sizeof S.pad_in - 1) S.pad_in[S.pad_in_len++] = buf[i];
        else S.pad_in_len = 0;
    }
}

/* ------------------------------------------------------------------ one window to a workspace
 * A 720 x 480 panel has room for one window at a time, and the dock is what lists them. Tiled windows would split the screen and a tabbed layout costs a bar of its own, so each new
 * tiled window is moved to the lowest workspace number no other window of ours has, and that workspace is shown: it fills the space the dock leaves, with nothing around it. Closing it
 * lets sway fall back to the workspace before. Floating windows (dialogs) stay with their parent. The foreign-toplevel protocol then brings any of them forward, whichever workspace it is on. */
static bool sway_send(uint32_t type, const char *payload)
{
    unsigned char msg[512];
    size_t n = ipc_frame(type, payload, msg, sizeof msg);
    if (!n || S.sway_fd < 0) return false;
    if (send(S.sway_fd, msg, n, MSG_NOSIGNAL) != (ssize_t)n) { close(S.sway_fd); S.sway_fd = -1; S.sway_retry = now_ms() + 2000; return false; }
    return true;
}

static void sway_connect(void)
{
    if (S.sway_fd >= 0 || now_ms() < S.sway_retry) return;
    S.sway_retry = now_ms() + 2000;
    char path[256];
    const char *e = getenv("SWAYSOCK");
    const char *rt = getenv("XDG_RUNTIME_DIR");
    if (e && *e) snprintf(path, sizeof path, "%s", e);
    else snprintf(path, sizeof path, "%s/sway-ipc.%d.sock", rt && *rt ? rt : "/run", (int)getuid());
    struct sockaddr_un a;
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    if (fd < 0 || !sock_addr(&a, path) || connect(fd, (struct sockaddr *)&a, sizeof a) != 0) { if (fd >= 0) close(fd); return; }
    fcntl(fd, F_SETFL, fcntl(fd, F_GETFL) | O_NONBLOCK);
    S.sway_fd = fd;
    S.sway_in.len = 0;
    memset(S.placed, 0, sizeof S.placed);
    sway_send(IPC_SUBSCRIBE, "[\"window\"]");
}

static int free_workspace(void)
{
    for (int n = 1; n < 100; n++) {
        bool used = false;
        for (size_t i = 0; i < sizeof S.placed / sizeof *S.placed; i++) if (S.placed[i].id && S.placed[i].ws == n) used = true;
        if (!used) return n;
    }
    return 1;
}

static void sway_window(const IpcWindow *w)
{
    if (!strcmp(w->change, "close")) {
        for (size_t i = 0; i < sizeof S.placed / sizeof *S.placed; i++) if (S.placed[i].id == w->id) S.placed[i].id = 0;
        if (w->focused) sway_send(IPC_RUN_COMMAND, "workspace back_and_forth");      /* sway leaves you on the empty workspace: go back to where you came from */
        return;
    }
    if (strcmp(w->change, "new") != 0 || !w->has_container || w->floating || w->id <= 0) return;
    size_t slot = sizeof S.placed / sizeof *S.placed;
    for (size_t i = 0; i < sizeof S.placed / sizeof *S.placed; i++) if (!S.placed[i].id) { slot = i; break; }
    if (slot == sizeof S.placed / sizeof *S.placed) return;                       /* sixty-four windows: the rest stay where sway put them */
    int n = free_workspace();
    S.placed[slot].id = w->id;
    S.placed[slot].ws = n;
    char cmd[200];
    snprintf(cmd, sizeof cmd, "[con_id=%ld] move container to workspace number %d; workspace number %d", w->id, n, n);
    sway_send(IPC_RUN_COMMAND, cmd);
}

static void sway_read(void)
{
    unsigned char buf[2048];
    ssize_t n = recv(S.sway_fd, buf, sizeof buf, MSG_DONTWAIT);
    if (n == 0 || (n < 0 && errno != EAGAIN && errno != EINTR)) { close(S.sway_fd); S.sway_fd = -1; S.sway_retry = now_ms() + 2000; return; }
    if (n < 0) return;
    if (!ipc_feed(&S.sway_in, buf, (size_t)n)) { close(S.sway_fd); S.sway_fd = -1; S.sway_retry = now_ms() + 2000; return; }
    char payload[4096];
    uint32_t type;
    int r;
    while ((r = ipc_next(&S.sway_in, &type, payload, sizeof payload)) == 1) {
        IpcWindow w;
        if (type == IPC_EVENT_WINDOW && ipc_parse_window(payload, &w)) sway_window(&w);
    }
    if (r < 0) { close(S.sway_fd); S.sway_fd = -1; S.sway_retry = now_ms() + 2000; }
}

/* ------------------------------------------------------------------ finished downloads
 * A folder the browser saves into, watched with inotify (nothing runs while no file arrives). A browser writes name.part and renames it when done, so the rename is the finish; a program that
 * writes the final name directly is seen when it closes the file. The first bytes of the file are read (never followed through a link) and downloads.c decides what it is and what to offer. */
static void download_complete(const char *name)
{
    if (dl_ignore(name)) return;
    char path[400];
    snprintf(path, sizeof path, "%.255s/%.130s", S.dl_dir, name);
    if (!strcmp(path, S.dl_last) && now_ms() - S.dl_last_t < 3000) return;               /* the rename and the close of the same file */
    snprintf(S.dl_last, sizeof S.dl_last, "%s", path);
    S.dl_last_t = now_ms();
    int fd = open(path, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (fd < 0) return;
    struct stat st;
    if (fstat(fd, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size == 0) { close(fd); return; }
    unsigned char head[300];
    ssize_t n = read(fd, head, sizeof head);
    close(fd);
    DlOffer o;
    dl_offer(dl_classify(name, head, n > 0 ? (size_t)n : 0), name, &o);
    if (o.action != DL_ACT_NONE) ui_toast_action(&S.ui, o.title, o.text, o.label, (int)o.action, path, 12000);
    else ui_toast(&S.ui, o.title, o.text, 6000);
}

static void downloads_read(void)
{
    char buf[4096] __attribute__((aligned(__alignof__(struct inotify_event))));
    ssize_t n = read(S.dl_fd, buf, sizeof buf);
    for (ssize_t i = 0; n > 0 && i < n;) {
        const struct inotify_event *e = (const struct inotify_event *)(buf + i);
        if ((e->mask & (IN_MOVED_TO | IN_CLOSE_WRITE)) && !(e->mask & IN_ISDIR) && e->len) download_complete(e->name);
        i += (ssize_t)sizeof *e + e->len;
    }
}

/* ------------------------------------------------------------------ the loop */
static void flush_state(void)
{
    run_effects();
    sync_surfaces();
    unsigned d = ui_take_dirty(&S.ui);
    if (d & DIRTY_DOCK) S.panels[P_DOCK].dirty = true;
    if (d & DIRTY_MENU) S.panels[P_MENU].dirty = true;
    if (d & DIRTY_OSK) S.panels[P_OSK].dirty = true;
    if (d & DIRTY_TOAST) S.panels[P_TOAST].dirty = true;
    if (d & DIRTY_SETTINGS) S.panels[P_SETTINGS].dirty = true;
    for (int i = 0; i < P_N; i++) if (S.panels[i].alive && S.panels[i].dirty) panel_draw(&S.panels[i]);
}

static void tick_clock(void)
{
    char t[8];
    long now = (long)time(NULL);
    sys_clock_text(now, t, sizeof t);
    ui_set_clock(&S.ui, t, rd_text_width(S.fonts.small, t));
    S.next_clock = now_ms() + (60 - now % 60) * 1000 + 50;
}

static int next_timeout(void)
{
    int64_t now = now_ms(), soon = S.next_clock;
    if (S.next_fast < soon) soon = S.next_fast;
    if (S.next_slow < soon) soon = S.next_slow;
    if (S.pad_fd < 0 && S.pad_retry < soon) soon = S.pad_retry;
    if (S.sway_fd < 0 && S.sway_retry < soon) soon = S.sway_retry;
    if (S.rep_code && S.rep_next < soon) soon = S.rep_next;
    int w = ui_next_wake(&S.ui);
    if (w >= 0 && now + w < soon) soon = now + w;
    int64_t d = soon - now;
    return d < 0 ? 0 : d > 60000 ? 60000 : (int)d;
}

static void run_timers(void)
{
    int64_t now = now_ms();
    ui_tick(&S.ui, now);
    if (now >= S.next_clock) tick_clock();
    if (now >= S.next_fast) { refresh_fast(); S.next_fast = now + FAST_MS; }
    if (now >= S.next_slow) { refresh_slow(); S.next_slow = now + SLOW_MS; }
    if (S.pad_fd < 0) pad_connect();
    if (S.sway_fd < 0 && getenv("WISP_DOCK_NO_WORKSPACES") == NULL) sway_connect();
    if (S.rep_code && now >= S.rep_next) {
        ui_key(&S.ui, S.rep_code, S.rep_text, S.mods);
        S.rep_next = now + (S.rep_rate > 0 ? 1000 / S.rep_rate : 1000 / KEY_REPEAT_RATE);
    }
    if (S.volume_pending && !job_running(J_VOLSET)) volume_flush();
}

static void on_signal(int s) { (void)s; S.running = false; }

static int ctl_client(const char *path, int argc, char **argv)
{
    int fd = socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC, 0);
    struct sockaddr_un a;
    if (fd < 0 || !sock_addr(&a, path) || connect(fd, (struct sockaddr *)&a, sizeof a) != 0) { fprintf(stderr, "wisp-dock: cannot reach %s: %s\n", path, strerror(errno)); return 1; }
    char line[256] = "";
    for (int i = 0; i < argc; i++) { strncat(line, argv[i], sizeof line - strlen(line) - 2); if (i + 1 < argc) strcat(line, " "); }
    strcat(line, "\n");
    if (write(fd, line, strlen(line)) < 0) return 1;
    if (!strncmp(line, "state", 5)) {
        struct pollfd pf = { fd, POLLIN, 0 };
        if (poll(&pf, 1, 3000) > 0) { char out[512]; ssize_t n = read(fd, out, sizeof out - 1); if (n > 0) { out[n] = 0; fputs(out, stdout); } }
    }
    close(fd);
    return 0;
}

static void split_dirs(char *buf, const char **out, int *n, int cap)
{
    *n = 0;
    for (char *p = strtok(buf, ":"); p && *n < cap; p = strtok(NULL, ":")) out[(*n)++] = p;
}

int main(int argc, char **argv)
{
    char sockpath[256];
    const char *rt = getenv("XDG_RUNTIME_DIR");
    const char *envsock = getenv("WISP_DOCK_SOCK");
    if (envsock && *envsock) snprintf(sockpath, sizeof sockpath, "%s", envsock);
    else snprintf(sockpath, sizeof sockpath, "%s/%s", rt && *rt ? rt : "/run", DEFAULT_SOCK_NAME);
    if (argc >= 2 && !strcmp(argv[1], "--ctl")) return argc >= 3 ? ctl_client(sockpath, argc - 2, argv + 2) : 2;
    if (argc != 1) { fprintf(stderr, "usage: wisp-dock | wisp-dock --ctl COMMAND\n"); return 2; }

    signal(SIGPIPE, SIG_IGN);
    struct sigaction sa;
    memset(&sa, 0, sizeof sa);
    sa.sa_handler = on_signal;
    sigaction(SIGTERM, &sa, NULL);
    sigaction(SIGINT, &sa, NULL);

    S.ptr_panel = -1;
    S.pad_fd = -1;
    S.sway_fd = -1;
    S.dl_fd = -1;
    S.rep_delay = KEY_REPEAT_DELAY_MS;
    S.rep_rate = KEY_REPEAT_RATE;
    for (int i = 0; i < 4; i++) S.ctl_clients[i] = -1;
    S.panels[P_DOCK].name = "wisp-dock";
    S.panels[P_MENU].name = "wisp-menu";
    S.panels[P_OSK].name = "wisp-osk";
    S.panels[P_TOAST].name = "wisp-toast";
    S.panels[P_SETTINGS].name = "wisp-settings";
    const char *e;
    snprintf(S.sysfs, sizeof S.sysfs, "%s", (e = getenv("WISP_DOCK_SYSFS")) && *e ? e : "/sys");
    snprintf(S.tool_dir, sizeof S.tool_dir, "%s", (e = getenv("WISP_DOCK_BIN")) && *e ? e : "");
    snprintf(S.pad_sock, sizeof S.pad_sock, "%s", (e = getenv("WISP_PAD_SOCK")) && *e ? e : DEFAULT_PAD_SOCK);
    snprintf(S.pins_path, sizeof S.pins_path, "%s", (e = getenv("WISP_DOCK_PINS")) && *e ? e : DEFAULT_PINS);

    char fonts_dir[256];
    snprintf(fonts_dir, sizeof fonts_dir, "%s", (e = getenv("WISP_DOCK_FONTS")) && *e ? e : DEFAULT_FONTS);
    if (view_fonts_open(&S.fonts, fonts_dir) != 0) return 1;

    snprintf(icon_roots_buf, sizeof icon_roots_buf, "%s", (e = getenv("WISP_DOCK_ICONS")) && *e ? e : DEFAULT_ICONS);
    split_dirs(icon_roots_buf, icon_roots, &n_icon_roots, 8);

    char apps_buf[600];
    const char *app_dirs[8];
    int n_app_dirs;
    snprintf(apps_buf, sizeof apps_buf, "%s", (e = getenv("WISP_DOCK_APPS")) && *e ? e : DEFAULT_APPS);
    split_dirs(apps_buf, app_dirs, &n_app_dirs, 8);
    WdLoadOpts opt = { getenv("LANG"), "WispOS:sway", getenv("PATH") };
    int napps = wd_apps_load(&S.apps, app_dirs, n_app_dirs, &opt);
    note("%d applications", napps < 0 ? 0 : napps);

    ui_init(&S.ui, &S.apps, &S.fonts);
    S.ui.icon_fn = shell_icon;
    int loaded = wd_pins_load(&S.ui.pins, S.pins_path);
    if (loaded == 0) {                                                       /* first run: the seeds that exist; nothing is written until the user changes something */
        const char *seed[] = SEED_PINS;
        wd_pins_seed(&S.ui.pins, &S.apps, seed, 3);
    }
    tick_clock();
    refresh_fast();
    refresh_slow();
    S.next_fast = now_ms() + FAST_MS;
    S.next_slow = now_ms() + SLOW_MS;
    ui_rebuild(&S.ui);

    S.dpy = wl_display_connect(NULL);
    if (!S.dpy) { note("cannot connect to the Wayland display: %s", strerror(errno)); return 1; }
    S.reg = wl_display_get_registry(S.dpy);
    wl_registry_add_listener(S.reg, &reg_listener, NULL);
    wl_display_roundtrip(S.dpy);
    wl_display_roundtrip(S.dpy);                                             /* the seat's capabilities */
    if (!S.comp || !S.shm || !S.seat || !S.lshell) { note("the compositor lacks wl_compositor, wl_shm, wl_seat or wlr-layer-shell"); return 1; }
    if (!S.ftm) note("the compositor has no foreign-toplevel management: open windows will not show in the dock");
    else zwlr_foreign_toplevel_manager_v1_add_listener(S.ftm, &ftm_listener, NULL);

    snprintf(S.dl_dir, sizeof S.dl_dir, "%s", (e = getenv("WISP_DOCK_DOWNLOADS")) && *e ? e : "/storage/downloads");
    if (mkdir(S.dl_dir, 0755) != 0 && errno != EEXIST) note("cannot create %s: %s", S.dl_dir, strerror(errno));
    S.dl_fd = inotify_init1(IN_NONBLOCK | IN_CLOEXEC);
    if (S.dl_fd >= 0 && inotify_add_watch(S.dl_fd, S.dl_dir, IN_MOVED_TO | IN_CLOSE_WRITE) < 0) { note("cannot watch %s: %s", S.dl_dir, strerror(errno)); close(S.dl_fd); S.dl_fd = -1; }

    S.ctl_listen = listen_unix(sockpath);
    snprintf(S.ctl_path, sizeof S.ctl_path, "%s", sockpath);
    if (S.ctl_listen < 0) note("cannot listen on %s: %s (key bindings and tests will not reach the shell)", sockpath, strerror(errno));
    pad_connect();
    if (!getenv("WISP_DOCK_NO_WORKSPACES")) sway_connect();

    S.running = true;
    sync_surfaces();
    flush_state();
    wl_display_flush(S.dpy);

    while (S.running) {
        while (wl_display_prepare_read(S.dpy) != 0) wl_display_dispatch_pending(S.dpy);
        wl_display_flush(S.dpy);
        struct pollfd pf[1 + 1 + 4 + 1 + 1 + 1 + JOBS];
        int n = 0, kind[1 + 1 + 4 + 1 + 1 + 1 + JOBS], idx[1 + 1 + 4 + 1 + 1 + 1 + JOBS];
        pf[n] = (struct pollfd){ wl_display_get_fd(S.dpy), POLLIN, 0 }; kind[n] = 0; idx[n++] = 0;
        if (S.ctl_listen >= 0) { pf[n] = (struct pollfd){ S.ctl_listen, POLLIN, 0 }; kind[n] = 1; idx[n++] = 0; }
        for (int i = 0; i < 4; i++) if (S.ctl_clients[i] >= 0) { pf[n] = (struct pollfd){ S.ctl_clients[i], POLLIN, 0 }; kind[n] = 2; idx[n++] = i; }
        if (S.pad_fd >= 0) { pf[n] = (struct pollfd){ S.pad_fd, POLLIN, 0 }; kind[n] = 3; idx[n++] = 0; }
        if (S.sway_fd >= 0) { pf[n] = (struct pollfd){ S.sway_fd, POLLIN, 0 }; kind[n] = 5; idx[n++] = 0; }
        if (S.dl_fd >= 0) { pf[n] = (struct pollfd){ S.dl_fd, POLLIN, 0 }; kind[n] = 6; idx[n++] = 0; }
        for (int i = 0; i < JOBS; i++) if (S.jobs[i].pid) { pf[n] = (struct pollfd){ S.jobs[i].fd, POLLIN, 0 }; kind[n] = 4; idx[n++] = i; }
        int r = poll(pf, (nfds_t)n, next_timeout());
        if (r < 0 && errno == EINTR) { wl_display_cancel_read(S.dpy); continue; }
        if (r > 0 && (pf[0].revents & POLLIN)) { if (wl_display_read_events(S.dpy) < 0) { note("lost the Wayland connection"); break; } }
        else wl_display_cancel_read(S.dpy);
        if (wl_display_dispatch_pending(S.dpy) < 0) { note("lost the Wayland connection"); break; }
        for (int k = 1; r > 0 && k < n; k++) {
            if (!pf[k].revents) continue;
            switch (kind[k]) {
            case 1: { int c = accept4(S.ctl_listen, NULL, NULL, SOCK_NONBLOCK | SOCK_CLOEXEC); if (c >= 0) { int slot = -1; for (int i = 0; i < 4 && slot < 0; i++) if (S.ctl_clients[i] < 0) slot = i; if (slot < 0) close(c); else S.ctl_clients[slot] = c; } break; }
            case 2: clients_read(); break;
            case 3: pad_read(); break;
            case 5: sway_read(); break;
            case 6: downloads_read(); break;
            case 4: job_read(&S.jobs[idx[k]]); break;
            default: break;
            }
        }
        run_timers();
        flush_state();
        for (int i = 0; i < P_N; i++) if (S.panels[i].surf && !S.panels[i].alive) panel_destroy(&S.panels[i]);
    }
    note("stopping");
    for (int i = 0; i < P_N; i++) if (S.panels[i].surf) panel_destroy(&S.panels[i]);
    if (S.ctl_listen >= 0) { close(S.ctl_listen); unlink(S.ctl_path); }
    if (S.sway_fd >= 0) close(S.sway_fd);
    if (S.dl_fd >= 0) close(S.dl_fd);
    wl_display_disconnect(S.dpy);
    view_fonts_close(&S.fonts);
    return 0;
}
