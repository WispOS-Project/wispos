/* The desktop shell's UI state machine: see ui.h. */
#define _GNU_SOURCE
#include "ui.h"

#include <linux/input-event-codes.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../ui/wisp-ui.h"

#define TOAST_MS 4000
#define VOLUME_STEP 0.05f
#define BRIGHTNESS_MIN 0.05f                 /* a black panel with no way back is not a setting */

/* ------------------------------------------------------------------ small helpers */
static void dirty(Ui *u, unsigned bits) { u->dirty |= bits; }

static void eff(Ui *u, UiEffKind kind, int a, int b, int c, float f, const char *s)
{
    if (u->neff >= UI_EFFECTS) { u->dropped++; return; }
    UiEffect *e = &u->eff[u->neff++];
    memset(e, 0, sizeof *e);
    e->kind = kind;
    e->a = a; e->b = b; e->c = c; e->f = f;
    if (s) snprintf(e->s, sizeof e->s, "%s", s);
}

static float clampf(float v, float lo, float hi) { return v < lo ? lo : v > hi ? hi : v; }
static int clampi(int v, int lo, int hi) { return v < lo ? lo : v > hi ? hi : v; }

static const WdApp *item_app(const Ui *u, const WdItem *it) { return it->app >= 0 && it->app < u->apps->n ? &u->apps->v[it->app] : NULL; }

/* ------------------------------------------------------------------ US keyboard */
static const char LOWER[] = "abcdefghijklmnopqrstuvwxyz";
static const int LOWER_KEY[26] = { KEY_A, KEY_B, KEY_C, KEY_D, KEY_E, KEY_F, KEY_G, KEY_H, KEY_I, KEY_J, KEY_K, KEY_L, KEY_M, KEY_N, KEY_O, KEY_P, KEY_Q, KEY_R, KEY_S, KEY_T, KEY_U, KEY_V, KEY_W, KEY_X, KEY_Y, KEY_Z };
static const char DIGITS[] = "1234567890";
static const char SYM_PLAIN[] = "-=[];'`\\,./";
static const char SYM_SHIFT[] = "_+{}:\"~|<>?";
static const int SYM_KEY[11] = { KEY_MINUS, KEY_EQUAL, KEY_LEFTBRACE, KEY_RIGHTBRACE, KEY_SEMICOLON, KEY_APOSTROPHE, KEY_GRAVE, KEY_BACKSLASH, KEY_COMMA, KEY_DOT, KEY_SLASH };
static const char DIGIT_SHIFT[] = "!@#$%^&*()";

bool ui_char_key(char ch, int *code, bool *shift)
{
    const char *p;
    *shift = false;
    if (ch >= 'a' && ch <= 'z') { *code = LOWER_KEY[ch - 'a']; return true; }
    if (ch >= 'A' && ch <= 'Z') { *code = LOWER_KEY[ch - 'A']; *shift = true; return true; }
    if (ch == ' ') { *code = KEY_SPACE; return true; }
    if (ch && (p = strchr(DIGITS, ch))) { *code = KEY_1 + (int)(p - DIGITS); return true; }
    if (ch && (p = strchr(DIGIT_SHIFT, ch))) { *code = KEY_1 + (int)(p - DIGIT_SHIFT); *shift = true; return true; }
    if (ch && (p = strchr(SYM_PLAIN, ch))) { *code = SYM_KEY[p - SYM_PLAIN]; return true; }
    if (ch && (p = strchr(SYM_SHIFT, ch))) { *code = SYM_KEY[p - SYM_SHIFT]; *shift = true; return true; }
    return false;
}

char ui_key_char(int code, bool shift)
{
    for (int i = 0; i < 26; i++) if (LOWER_KEY[i] == code) return shift ? (char)('A' + i) : LOWER[i];
    if (code >= KEY_1 && code <= KEY_0) return shift ? DIGIT_SHIFT[code - KEY_1] : DIGITS[code - KEY_1];
    for (int i = 0; i < 11; i++) if (SYM_KEY[i] == code) return shift ? SYM_SHIFT[i] : SYM_PLAIN[i];
    return code == KEY_SPACE ? ' ' : 0;
}

/* ------------------------------------------------------------------ setup and rebuilding */
void ui_init(Ui *u, WdApps *apps, const ViewFonts *fonts)
{
    memset(u, 0, sizeof *u);
    u->apps = apps;
    u->fonts = fonts;
    u->focus = u->hover = u->press = -1;
    u->orow = 2;
    u->ocol = 4;
    u->sys.battery = -1.f;
    u->sys.screen_timeout = -1;
    u->sys.volume = 0.5f;
    u->sys.brightness = 0.5f;
    snprintf(u->clock, sizeof u->clock, "--:--");
}

static void menu_refilter(Ui *u)
{
    u->mn = wd_menu_list(u->apps, u->query, u->mlist, WD_MAX_APPS);
    if (u->mn < 0) u->mn = 0;
    u->msel = u->mn ? clampi(u->msel, 0, u->mn - 1) : 0;
    u->mtop = wd_menu_top_row(u->msel, WISP_UI_MENU_COLS, view_menu_rows_visible(), u->mtop);
}

void ui_rebuild(Ui *u)
{
    u->n = wd_dock_build(u->apps, &u->pins, &u->ws, u->items, WD_MAX_ITEMS);
    wd_dock_layout(u->items, u->n, WISP_UI_W, u->clock_w);
    if (u->dock != UI_IDLE && u->focus >= 0) {
        int found = -1;
        for (int i = 0; i < u->n; i++) if (u->focus_key[0] && !strcmp(u->items[i].key, u->focus_key)) found = i;
        if (found < 0) found = clampi(u->focus, 0, u->n - 1);
        if (found >= 0 && u->items[found].kind == WD_SEP) found = wd_dock_move(u->items, u->n, found, -1);
        if (found < 0 || u->items[found].kind == WD_SEP) found = wd_dock_first_focus(u->items, u->n);
        u->focus = found;
        snprintf(u->focus_key, sizeof u->focus_key, "%s", found >= 0 ? u->items[found].key : "");
    }
    u->hover = u->press = -1;
    if (u->menu) menu_refilter(u);
    dirty(u, DIRTY_DOCK | DIRTY_MENU);
}

void ui_set_clock(Ui *u, const char *text, int width)
{
    if (!strcmp(u->clock, text) && u->clock_w == width) return;
    snprintf(u->clock, sizeof u->clock, "%s", text);
    u->clock_w = width;
    ui_rebuild(u);
}

void ui_windows_changed(Ui *u)
{
    ui_rebuild(u);
    const char *front = "";
    for (int i = 0; i < u->ws.n; i++) if (u->ws.w[i].activated) front = u->ws.w[i].app_id;
    if (strcmp(front, u->pad_focus) != 0) {
        snprintf(u->pad_focus, sizeof u->pad_focus, "%s", front);
        eff(u, EFF_PAD_FOCUS, 0, 0, 0, 0, front);
    }
}

/* ------------------------------------------------------------------ the dock */
static void dock_close(Ui *u)
{
    u->dock = UI_IDLE;
    u->focus = u->hover = u->press = -1;
    u->focus_key[0] = 0;
    u->nacts = 0;
    u->dragging = false;
    dirty(u, DIRTY_DOCK);
}

static void set_focus(Ui *u, int i)
{
    u->focus = i;
    snprintf(u->focus_key, sizeof u->focus_key, "%s", i >= 0 ? u->items[i].key : "");
    dirty(u, DIRTY_DOCK);
}

static void menu_close(Ui *u)
{
    if (!u->menu) return;
    u->menu = false;
    u->query[0] = 0;
    dirty(u, DIRTY_MENU);
}

static void osk_set(Ui *u, bool on)
{
    if (u->osk == on) return;
    u->osk = on;                                                          /* (the dock is hidden while it is open; it is never open when this is called: the callers close it first) */
    eff(u, EFF_PAD_OSK, on ? 1 : 0, 0, 0, 0, NULL);
    dirty(u, DIRTY_OSK | DIRTY_DOCK);
}

static void menu_open(Ui *u)
{
    dock_close(u);
    if (u->osk) osk_set(u, false);
    u->menu = true;
    u->query[0] = 0;
    u->msel = u->mtop = 0;
    menu_refilter(u);
    dirty(u, DIRTY_MENU);
}

static void dock_summon(Ui *u, bool from_pad)
{
    menu_close(u);
    if (u->osk) osk_set(u, false);
    u->dock = UI_SUMMONED;
    u->from_pad = from_pad;
    set_focus(u, wd_dock_first_focus(u->items, u->n));
}

static void qs_open(Ui *u, bool from_pad)
{
    menu_close(u);
    if (u->osk) osk_set(u, false);
    u->dock = UI_QS;
    u->from_pad = from_pad;
    u->qs_sel = 0;
    u->qs_page = 0;
    u->dragging = false;
    for (int i = 0; i < u->n; i++) if (u->items[i].kind == WD_STATUS) set_focus(u, i);
    eff(u, EFF_REFRESH, 0, 0, 0, 0, NULL);
    dirty(u, DIRTY_DOCK);
}

/* a panel is closed: back to the summoned dock when the pad opened it, else to the plain strip */
static void panel_back(Ui *u)
{
    if (u->from_pad && u->dock != UI_SUMMONED) { u->dock = UI_SUMMONED; u->nacts = 0; dirty(u, DIRTY_DOCK); }
    else dock_close(u);
}

static void item_open(Ui *u, int i)
{
    const WdItem *it = &u->items[i];
    if ((it->kind == WD_PIN || it->kind == WD_WINDOW) && it->running && it->window) eff(u, EFF_ACTIVATE, (int)it->window, 0, 0, 0, NULL);
    else if (it->app >= 0) eff(u, EFF_LAUNCH, it->app, 0, 0, 0, NULL);
}

static void item_activate(Ui *u, int i)
{
    if (i < 0 || i >= u->n) return;
    switch (u->items[i].kind) {
    case WD_APPS: menu_open(u); break;
    case WD_PIN: case WD_WINDOW: item_open(u, i); dock_close(u); break;
    case WD_GAME: eff(u, EFF_GAME_MODE, 0, 0, 0, 0, NULL); dock_close(u); break;
    case WD_STATUS: qs_open(u, u->dock == UI_SUMMONED); break;
    default: break;
    }
}

static void toast_text(Ui *u, const char *title, const char *text, int ttl) { ui_toast(u, title, text, ttl); }

static void pin_toggle_app(Ui *u, int app)
{
    if (app < 0 || app >= u->apps->n) return;
    int r = wd_pins_toggle(&u->pins, u->apps->v[app].id);
    if (r < 0) { toast_text(u, "The dock is full", "Unpin something first.", TOAST_MS); return; }
    eff(u, EFF_SAVE_PINS, 0, 0, 0, 0, NULL);
}

static void item_pin_toggle(Ui *u, int i)
{
    if (i < 0 || i >= u->n) return;
    const WdItem *it = &u->items[i];
    if ((it->kind != WD_PIN && it->kind != WD_WINDOW) || it->app < 0) return;
    int app = it->app, old = i;
    pin_toggle_app(u, app);
    ui_rebuild(u);
    if (u->dock != UI_IDLE) {                                               /* the focus follows the application (a window becomes a pin and back), or stays near when it is gone */
        int at = -1;
        for (int k = 0; k < u->n && at < 0; k++) if (u->items[k].app == app && (u->items[k].kind == WD_PIN || u->items[k].kind == WD_WINDOW)) at = k;
        set_focus(u, at >= 0 ? at : clampi(old, 0, u->n - 1));
        if (u->items[u->focus].kind == WD_SEP) set_focus(u, wd_dock_first_focus(u->items, u->n));
    }
}

static void actions_open(Ui *u, int i)
{
    if (i < 0 || i >= u->n) return;
    int n = wd_item_actions(&u->items[i], &u->pins, u->acts);
    if (n <= 0) return;
    u->nacts = n;
    u->act_sel = 0;
    u->dock = UI_ACTIONS_OPEN;
    set_focus(u, i);
    dirty(u, DIRTY_DOCK);
}

static void action_perform(Ui *u, WdAction a)
{
    int i = u->focus;
    if (i < 0 || i >= u->n) return;
    const WdItem *it = &u->items[i];
    switch (a) {
    case WD_ACT_OPEN: item_open(u, i); dock_close(u); break;
    case WD_ACT_PIN: case WD_ACT_UNPIN: u->dock = u->from_pad ? UI_SUMMONED : UI_IDLE; item_pin_toggle(u, i); if (!u->from_pad) dock_close(u); break;
    case WD_ACT_LEFT: case WD_ACT_RIGHT:
        if (it->app >= 0) {
            wd_pins_move(&u->pins, u->apps->v[it->app].id, a == WD_ACT_LEFT ? -1 : 1);
            eff(u, EFF_SAVE_PINS, 0, 0, 0, 0, NULL);
        }
        u->dock = u->from_pad ? UI_SUMMONED : UI_IDLE;
        ui_rebuild(u);
        if (!u->from_pad) dock_close(u);
        break;
    case WD_ACT_CLOSE:
        if (it->window) eff(u, EFF_CLOSE, (int)it->window, 0, 0, 0, NULL);
        dock_close(u);
        break;
    }
    dirty(u, DIRTY_DOCK);
}

/* ------------------------------------------------------------------ the menu */
static void menu_launch(Ui *u)
{
    if (u->msel < 0 || u->msel >= u->mn) return;
    eff(u, EFF_LAUNCH, u->mlist[u->msel], 0, 0, 0, NULL);
    menu_close(u);
}

static void menu_move(Ui *u, int dir)
{
    if (!u->mn) return;
    u->msel = wd_menu_move(u->msel, u->mn, WISP_UI_MENU_COLS, dir);
    u->mtop = wd_menu_top_row(u->msel, WISP_UI_MENU_COLS, view_menu_rows_visible(), u->mtop);
    dirty(u, DIRTY_MENU);
}

static void menu_text(Ui *u, const char *utf8)
{
    size_t len = strlen(u->query), add = strlen(utf8);
    if (len + add >= UI_QUERY) return;
    memcpy(u->query + len, utf8, add + 1);
    u->msel = u->mtop = 0;
    menu_refilter(u);
    dirty(u, DIRTY_MENU);
}

static void menu_backspace(Ui *u)
{
    size_t len = strlen(u->query);
    if (!len) return;
    while (len > 0 && ((unsigned char)u->query[len - 1] & 0xc0) == 0x80) len--;          /* a whole code point */
    u->query[len ? len - 1 : 0] = 0;
    u->msel = u->mtop = 0;
    menu_refilter(u);
    dirty(u, DIRTY_MENU);
}

static void menu_key(Ui *u, int code, const char *utf8, unsigned mods)
{
    switch (code) {
    case KEY_LEFT: menu_move(u, 2); return;
    case KEY_RIGHT: menu_move(u, 3); return;
    case KEY_UP: menu_move(u, 0); return;
    case KEY_DOWN: menu_move(u, 1); return;
    case KEY_TAB: menu_move(u, (mods & UI_SHIFT) ? 2 : 3); return;
    case KEY_ENTER: case KEY_KPENTER: menu_launch(u); return;
    case KEY_ESC:
        if (u->query[0]) { u->query[0] = 0; u->msel = u->mtop = 0; menu_refilter(u); dirty(u, DIRTY_MENU); }
        else menu_close(u);
        return;
    case KEY_BACKSPACE: menu_backspace(u); return;
    case KEY_SPACE:
        if (!u->query[0]) { osk_set(u, true); return; }                       /* Y on the pad is Space: with nothing typed it is "Search" */
        menu_text(u, " ");
        return;
    default: break;
    }
    if (utf8 && (unsigned char)utf8[0] >= 0x20 && utf8[0] != 0x7f && !(mods & (UI_CTRL | UI_ALT | UI_SUPER))) menu_text(u, utf8);
}

/* ------------------------------------------------------------------ the quick settings */
enum { QS_PAGE_LIST = 0, QS_PAGE_USB = 1 };
/* The battery row of the list is information; in the USB submenu the selectable rows are the back row and the three modes (the note under them is text). */
static bool qs_selectable(const Ui *u, int row) { return u->qs_page == QS_PAGE_USB ? (row >= 0 && row <= 3) : (row >= 0 && row <= 5); }
static void qs_build(const Ui *u, QsRow rows[UI_QS_ROWS], char text[UI_QS_ROWS][64], Hint hints[2], QsView *out);

static void qs_adjust(Ui *u, int dir)
{
    if (u->qs_page != QS_PAGE_LIST) return;
    switch (u->qs_sel) {
    case 2: u->sys.volume = clampf(u->sys.volume + (float)dir * VOLUME_STEP, 0.f, 1.f); eff(u, EFF_SET_VOLUME, 0, 0, 0, u->sys.volume, NULL); break;
    case 3: u->sys.brightness = clampf(u->sys.brightness + (float)dir * VOLUME_STEP, BRIGHTNESS_MIN, 1.f); eff(u, EFF_SET_BRIGHTNESS, 0, 0, 0, u->sys.brightness, NULL); break;
    case 1: if (u->sys.bt_available && (dir > 0) != u->sys.bt_on) { u->sys.bt_on = dir > 0; eff(u, EFF_SET_BLUETOOTH, u->sys.bt_on, 0, 0, 0, NULL); } break;
    case 4: if ((dir > 0) != u->sys.ssh_on) { u->sys.ssh_on = dir > 0; eff(u, EFF_SET_SSH, u->sys.ssh_on, 0, 0, 0, NULL); } break;
    default: break;
    }
    dirty(u, DIRTY_DOCK);
}

static void qs_page_set(Ui *u, int page, int sel)
{
    u->qs_page = page;
    u->qs_sel = sel;
    u->dragging = false;
    dirty(u, DIRTY_DOCK);
}

static void qs_change(Ui *u)                                                 /* A: act on the selected row */
{
    if (u->qs_page == QS_PAGE_USB) {                                        /* the back row, or a mode: apply it and return to the list with the USB row selected */
        if (u->qs_sel >= 1 && u->qs_sel <= 3 && u->qs_sel - 1 != u->sys.usb) { u->sys.usb = u->qs_sel - 1; eff(u, EFF_SET_USB, u->sys.usb, 0, 0, 0, NULL); }
        qs_page_set(u, QS_PAGE_LIST, 5);
        return;
    }
    switch (u->qs_sel) {
    case 0: eff(u, EFF_WIFI, 0, 0, 0, 0, NULL); break;
    case 1: if (u->sys.bt_available) { u->sys.bt_on = !u->sys.bt_on; eff(u, EFF_SET_BLUETOOTH, u->sys.bt_on, 0, 0, 0, NULL); } break;
    case 4: u->sys.ssh_on = !u->sys.ssh_on; eff(u, EFF_SET_SSH, u->sys.ssh_on, 0, 0, 0, NULL); break;
    case 5: qs_page_set(u, QS_PAGE_USB, 1 + clampi(u->sys.usb, 0, 2)); return;     /* the submenu opens with the current mode selected */
    default: break;
    }
    dirty(u, DIRTY_DOCK);
}

static void qs_key(Ui *u, int code)
{
    switch (code) {
    case KEY_UP: case KEY_DOWN: {
        int d = code == KEY_UP ? -1 : 1, r = u->qs_sel + d;
        if (qs_selectable(u, r)) u->qs_sel = r;
        dirty(u, DIRTY_DOCK);
        break;
    }
    case KEY_LEFT: if (u->qs_page == QS_PAGE_USB) qs_page_set(u, QS_PAGE_LIST, 5); else qs_adjust(u, -1); break;
    case KEY_RIGHT: qs_adjust(u, 1); break;
    case KEY_ENTER: case KEY_KPENTER: qs_change(u); break;
    case KEY_ESC: if (u->qs_page == QS_PAGE_USB) qs_page_set(u, QS_PAGE_LIST, 5); else panel_back(u); break;
    default: break;
    }
}

/* ------------------------------------------------------------------ the settings window */
static void fmt_into(char *dst, size_t cap, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
/* The pages that exist (each later page adds its rows here). Display: Screen timeout, the panel's blank timeout in `wisp-screen timeout` (the file the idle watcher reads). */
enum { SET_DISPLAY, SET_PAGES };
static const char *const TIMEOUT_NAMES[6] = { "Never", "1 min", "2 min", "5 min", "10 min", "30 min" };
static const int TIMEOUT_SECS[6] = { 0, 60, 120, 300, 600, 1800 };               /* 120 s is what the idle watcher uses until someone sets another */

static int set_nrows(int page) { return page == SET_DISPLAY ? 1 : 0; }
static int set_nopt(int page, int row) { (void)row; return page == SET_DISPLAY ? 6 : 0; }
static int set_on(const Ui *u, int page, int row)                                 /* the option that applies, -1 when the value is none of them */
{
    (void)row;
    if (page == SET_DISPLAY) for (int i = 0; i < 6; i++) if (u->sys.screen_timeout == TIMEOUT_SECS[i]) return i;
    return -1;
}

static void settings_open(Ui *u)
{
    menu_close(u);
    if (u->osk) osk_set(u, false);
    if (u->dock != UI_IDLE) dock_close(u);
    u->settings = true;
    u->win = 0;
    u->set_page = 0;
    u->set_in = false;
    u->set_row = u->set_opt = 0;
    eff(u, EFF_REFRESH, 0, 0, 0, 0, NULL);
    dirty(u, DIRTY_SETTINGS | DIRTY_DOCK);
}

static void settings_close(Ui *u)
{
    if (!u->settings) return;
    u->settings = false;
    u->win = 0;
    u->ag_phase = 0;
    u->set_in = false;
    dirty(u, DIRTY_SETTINGS | DIRTY_DOCK);
}

static void set_enter(Ui *u)                                                      /* into the page's rows, on the option that applies */
{
    if (set_nrows(u->set_page) == 0) return;
    int on = set_on(u, u->set_page, 0);
    u->set_in = true;
    u->set_row = 0;
    u->set_opt = on >= 0 ? on : 0;
    dirty(u, DIRTY_SETTINGS);
}

/* Which rows apply something when picked. Every row counted by set_nrows needs a case here and in set_apply: a click on a row that is not listed is ignored, so a row added
 * without its case cannot be selected and then do nothing. */
static bool set_applies(int page, int row) { return page == SET_DISPLAY && row == 0; }

static void set_apply(Ui *u)
{
    if (set_applies(u->set_page, u->set_row)) {
        int secs = TIMEOUT_SECS[clampi(u->set_opt, 0, 5)];
        if (secs != u->sys.screen_timeout) { u->sys.screen_timeout = secs; eff(u, EFF_SET_SCREEN_TIMEOUT, secs, 0, 0, 0, NULL); }
    }
    dirty(u, DIRTY_SETTINGS);
}

static void settings_key(Ui *u, int code, unsigned mods)
{
    switch (code) {
    case KEY_ESC: if (u->set_in) { u->set_in = false; dirty(u, DIRTY_SETTINGS); } else settings_close(u); break;
    case KEY_TAB: u->set_page = clampi(u->set_page + ((mods & UI_SHIFT) ? -1 : 1), 0, SET_PAGES - 1); u->set_in = false; dirty(u, DIRTY_SETTINGS); break;     /* R2 / L2 */
    case KEY_UP: case KEY_DOWN: {
        int d = code == KEY_UP ? -1 : 1;
        if (u->set_in) u->set_row = clampi(u->set_row + d, 0, set_nrows(u->set_page) - 1);
        else u->set_page = clampi(u->set_page + d, 0, SET_PAGES - 1);
        dirty(u, DIRTY_SETTINGS);
        break;
    }
    case KEY_LEFT:
        if (u->set_in) { if (u->set_opt == 0) u->set_in = false; else u->set_opt--; dirty(u, DIRTY_SETTINGS); }
        break;
    case KEY_RIGHT:
        if (!u->set_in) set_enter(u);
        else { u->set_opt = clampi(u->set_opt + 1, 0, set_nopt(u->set_page, u->set_row) - 1); dirty(u, DIRTY_SETTINGS); }
        break;
    case KEY_ENTER: case KEY_KPENTER: if (u->set_in) set_apply(u); else set_enter(u); break;
    default: break;
    }
}

static void set_build(const Ui *u, SetPage pages[UI_SET_PAGES], SetRow rows[UI_SET_ROWS], char note[UI_SET_ROWS][64], Hint hints[3], SettingsView *out)
{
    pages[SET_DISPLAY] = (SetPage){ "Display", "brightness", 0 };
    int nrows = set_nrows(u->set_page);
    if (u->set_page == SET_DISPLAY) {
        int on = set_on(u, SET_DISPLAY, 0);
        if (on >= 0 || u->sys.screen_timeout < 0) fmt_into(note[0], 64, "Turn the screen off after no input");
        else fmt_into(note[0], 64, "Set to %d s from a terminal; pick one to change it", u->sys.screen_timeout);
        rows[0] = (SetRow){ "Screen timeout", note[0], { TIMEOUT_NAMES[0], TIMEOUT_NAMES[1], TIMEOUT_NAMES[2], TIMEOUT_NAMES[3], TIMEOUT_NAMES[4], TIMEOUT_NAMES[5] }, 6, on,
                            u->set_in && u->set_row == 0 ? u->set_opt : -1 };
    }
    hints[0] = (Hint){ "A", "Change" };
    hints[1] = (Hint){ "B", u->set_in ? "Back" : "Close" };
    hints[2] = (Hint){ "L2/R2", "Page" };
    *out = (SettingsView){ pages, SET_PAGES, u->set_page, rows, nrows, u->set_row, u->set_in, hints, SET_PAGES > 1 ? 3 : 2 };
}

static void pointer_settings(Ui *u, int x, int y, UiPtr ev, int button)
{
    if (ev != PTR_DOWN || button != BTN_LEFT) return;
    SetPage pages[UI_SET_PAGES]; SetRow rows[UI_SET_ROWS]; char note[UI_SET_ROWS][64]; Hint hints[3]; SettingsView sv;
    set_build(u, pages, rows, note, hints, &sv);
    SetHit h = view_settings_hit(&sv, x, y);
    if (h.page >= 0) { u->set_page = h.page; u->set_in = false; dirty(u, DIRTY_SETTINGS); }
    else if (h.row >= 0 && set_applies(u->set_page, h.row)) { u->set_in = true; u->set_row = h.row; u->set_opt = h.opt; set_apply(u); }
}

/* ------------------------------------------------------------------ the Add game window (issue #46)
 * Places at the left (the cards), a folder listing at the right: A opens a folder or picks a program (.exe), B goes up and then out. A pick asks once (Cancel / Add to Steam); adding hands the
 * name and program to the shell (EFF_AG_ADD: wisp-shortcut, which needs Steam stopped, as it is in desktop mode) and the window then says where the game is and offers Game mode. */
void ui_ag_clear_places(Ui *u) { u->ag_nplaces = 0; }

void ui_ag_add_place(Ui *u, const char *name, const char *sub, const char *path)
{
    if (u->ag_nplaces >= UI_AG_PLACES) return;
    UiAgPlace *p = &u->ag_places[u->ag_nplaces++];
    snprintf(p->name, sizeof p->name, "%s", name);
    snprintf(p->sub, sizeof p->sub, "%s", sub);
    snprintf(p->path, sizeof p->path, "%s", path);
    size_t n = strlen(p->path);
    while (n > 1 && p->path[n - 1] == '/') p->path[--n] = 0;
}

static bool ag_at_root(const Ui *u) { return u->ag_nplaces == 0 || !strcmp(u->ag_dir, u->ag_places[u->ag_place].path); }

static void ag_list(Ui *u, const char *dir)
{
    snprintf(u->ag_dir, sizeof u->ag_dir, "%s", dir);
    u->ag_n = 0;
    u->ag_sel = u->ag_top = 0;
    eff(u, EFF_AG_LIST, 0, 0, 0, 0, NULL);
    dirty(u, DIRTY_SETTINGS);
}

static int ag_cmp(const void *pa, const void *pb)
{
    const UiAgEntry *a = pa, *b = pb;
    if (a->dir != b->dir) return a->dir ? -1 : 1;
    return strcasecmp(a->name, b->name);
}

static bool ag_is_exe(const char *name)
{
    size_t n = strlen(name);
    return n > 4 && !strcasecmp(name + n - 4, ".exe");
}

void ui_ag_entries(Ui *u, const char *dir, const UiAgEntry *e, int n)
{
    if (!u->settings || u->win != 1 || strcmp(dir, u->ag_dir)) return;       /* an answer for a folder that is no longer the one shown */
    UiAgEntry tmp[UI_AG_ENTRIES];
    int m = 0;
    for (int i = 0; i < n && m < UI_AG_ENTRIES - 1; i++) {
        if (e[i].name[0] == '.' || e[i].name[0] == 0) continue;               /* hidden files and the dot entries are not games */
        if (!e[i].dir && !ag_is_exe(e[i].name)) continue;
        tmp[m++] = e[i];
    }
    qsort(tmp, (size_t)m, sizeof tmp[0], ag_cmp);
    u->ag_n = 0;
    if (!ag_at_root(u)) { memset(&u->ag_e[0], 0, sizeof u->ag_e[0]); snprintf(u->ag_e[0].name, sizeof u->ag_e[0].name, ".."); u->ag_e[0].dir = true; u->ag_n = 1; }
    for (int i = 0; i < m && u->ag_n < UI_AG_ENTRIES; i++) u->ag_e[u->ag_n++] = tmp[i];
    u->ag_sel = u->ag_n > 1 && u->ag_e[0].name[0] == '.' ? 1 : 0;            /* on the first real entry, not on "up one folder" */
    u->ag_top = 0;
    dirty(u, DIRTY_SETTINGS);
}

static void ag_open(Ui *u)
{
    if (u->ag_nplaces == 0) { ui_toast(u, "No storage found", "Insert a card with games on it.", 3000); return; }
    menu_close(u);
    if (u->osk) osk_set(u, false);
    if (u->dock != UI_IDLE) dock_close(u);
    u->settings = true;
    u->win = 1;
    u->ag_place = 0;
    u->ag_in = false;
    u->ag_phase = 0;
    ag_list(u, u->ag_places[0].path);
    dirty(u, DIRTY_SETTINGS | DIRTY_DOCK);
}

static void ag_up(Ui *u)
{
    char d[WD_PATH];
    snprintf(d, sizeof d, "%s", u->ag_dir);
    char *s = strrchr(d, '/');
    if (s && s != d) *s = 0;
    ag_list(u, d);
}

static void ag_scroll(Ui *u)
{
    if (u->ag_sel < u->ag_top) u->ag_top = u->ag_sel;
    if (u->ag_sel >= u->ag_top + AG_ROWS_VISIBLE) u->ag_top = u->ag_sel - AG_ROWS_VISIBLE + 1;
}

static bool ag_join(char *dst, size_t cap, const char *dir, const char *name)       /* false when it would not fit: a cut path would name another file */
{
    int n = snprintf(dst, cap, "%s/%s", dir, name);
    return n > 0 && (size_t)n < cap;
}

static void ag_pick(Ui *u, int i)                                                 /* A on a row: open a folder, go up, or choose a program */
{
    if (i < 0 || i >= u->ag_n) return;
    const UiAgEntry *e = &u->ag_e[i];
    if (!strcmp(e->name, "..")) { ag_up(u); return; }
    char path[WD_PATH];
    if (!ag_join(path, sizeof path, u->ag_dir, e->name)) { ui_toast(u, "That path is too long", "Move the game closer to the top of the card.", 4000); return; }
    if (e->dir) { ag_list(u, path); return; }
    snprintf(u->ag_exe, sizeof u->ag_exe, "%s", path);
    snprintf(u->ag_name, sizeof u->ag_name, "%s", e->name);
    char *dot = strrchr(u->ag_name, '.');                                         /* the name defaults to the file's name without .exe: the library shows it */
    if (dot) *dot = 0;
    u->ag_phase = 1;
    u->ag_opt = 1;
    dirty(u, DIRTY_SETTINGS);
}

static void ag_place_set(Ui *u, int p)
{
    p = clampi(p, 0, u->ag_nplaces - 1);
    if (p == u->ag_place && !u->ag_in) return;
    u->ag_place = p;
    u->ag_in = false;
    ag_list(u, u->ag_places[p].path);
}

static void ag_confirm(Ui *u, int opt)                                            /* the confirmation's two buttons */
{
    if (opt == 1) { eff(u, EFF_AG_ADD, 0, 0, 0, 0, NULL); return; }
    u->ag_phase = 0;
    dirty(u, DIRTY_SETTINGS);
}

void ui_ag_added(Ui *u, bool ok, const char *why)
{
    if (!u->settings || u->win != 1) return;
    if (ok) { u->ag_phase = 2; dirty(u, DIRTY_SETTINGS); return; }
    u->ag_phase = 0;
    dirty(u, DIRTY_SETTINGS);
    ui_toast(u, "Could not add the game", why && why[0] ? why : "Steam did not accept it.", 4000);
}

static void ag_done(Ui *u, int opt)                                               /* the "added" dialog: Game mode, or stay here */
{
    if (opt == 1) { eff(u, EFF_GAME_MODE, 0, 0, 0, 0, NULL); settings_close(u); return; }
    u->ag_phase = 0;
    dirty(u, DIRTY_SETTINGS);
}

static void ag_key(Ui *u, int code, unsigned mods)
{
    if (u->ag_phase == 2) {
        if (code == KEY_ENTER || code == KEY_KPENTER) ag_done(u, 1);
        else if (code == KEY_ESC) ag_done(u, 0);
        return;
    }
    if (u->ag_phase == 1) {
        switch (code) {
        case KEY_LEFT: u->ag_opt = 0; dirty(u, DIRTY_SETTINGS); break;
        case KEY_RIGHT: u->ag_opt = 1; dirty(u, DIRTY_SETTINGS); break;
        case KEY_TAB: u->ag_opt = (mods & UI_SHIFT) ? 0 : 1; dirty(u, DIRTY_SETTINGS); break;
        case KEY_ENTER: case KEY_KPENTER: ag_confirm(u, u->ag_opt); break;
        case KEY_ESC: ag_confirm(u, 0); break;
        default: break;
        }
        return;
    }
    switch (code) {
    case KEY_ESC:
        if (u->ag_in && !ag_at_root(u)) ag_up(u);
        else if (u->ag_in) { u->ag_in = false; dirty(u, DIRTY_SETTINGS); }
        else settings_close(u);
        break;
    case KEY_UP: case KEY_DOWN: {
        int d = code == KEY_UP ? -1 : 1;
        if (u->ag_in) { u->ag_sel = clampi(u->ag_sel + d, 0, u->ag_n > 0 ? u->ag_n - 1 : 0); ag_scroll(u); dirty(u, DIRTY_SETTINGS); }
        else ag_place_set(u, u->ag_place + d);
        break;
    }
    case KEY_LEFT:
        if (u->ag_in && !ag_at_root(u)) ag_up(u);
        else if (u->ag_in) { u->ag_in = false; dirty(u, DIRTY_SETTINGS); }
        break;
    case KEY_RIGHT:
        if (!u->ag_in && u->ag_n > 0) { u->ag_in = true; dirty(u, DIRTY_SETTINGS); }
        break;
    case KEY_ENTER: case KEY_KPENTER:
        if (u->ag_in) ag_pick(u, u->ag_sel);
        else if (u->ag_n > 0) { u->ag_in = true; dirty(u, DIRTY_SETTINGS); }
        break;
    default: break;
    }
}

static void ag_build(const Ui *u, AgPlace places[UI_AG_PLACES], AgRow rows[UI_AG_ENTRIES], char note[UI_AG_ENTRIES][16], char *path, size_t pathcap, Hint hints[3], AgView *out)
{
    for (int i = 0; i < u->ag_nplaces; i++) {
        const UiAgPlace *p = &u->ag_places[i];
        places[i] = (AgPlace){ p->name, p->sub, i == 0 ? "sdcard" : "disk" };
    }
    for (int i = 0; i < u->ag_n; i++) {
        const UiAgEntry *e = &u->ag_e[i];
        bool up = !strcmp(e->name, "..");
        note[i][0] = 0;
        if (up) snprintf(note[i], 16, "up one folder");
        else if (e->dir) snprintf(note[i], 16, "folder");
        else if (e->kb >= 1024) snprintf(note[i], 16, "%.1f MB", (double)e->kb / 1024.0);
        else snprintf(note[i], 16, "%u KB", e->kb);
        rows[i] = (AgRow){ e->name, note[i], up ? "left" : e->dir ? "folder" : "exe" };
    }
    const char *root = u->ag_nplaces ? u->ag_places[u->ag_place].path : "";
    size_t rl = strlen(root);
    snprintf(path, pathcap, "%s", u->ag_nplaces ? u->ag_places[u->ag_place].name : "");
    if (!strncmp(u->ag_dir, root, rl) && u->ag_dir[rl] == '/') {
        for (const char *s = u->ag_dir + rl; *s; ) {                              /* "SD card 2  /  games  /  wow-1.12.1" */
            const char *e = strchr(s + 1, '/');
            size_t k = e ? (size_t)(e - s - 1) : strlen(s + 1), at = strlen(path);
            if (at + k + 6 >= pathcap) break;
            snprintf(path + at, pathcap - at, "  /  %.*s", (int)k, s + 1);
            s = e ? e : s + strlen(s);
        }
    }
    if (u->ag_phase == 0) { hints[0] = (Hint){ "A", "Open" }; hints[1] = (Hint){ "B", "Up" }; *out = (AgView){ 0, places, u->ag_nplaces, u->ag_place, u->ag_in, path, rows, u->ag_n, u->ag_sel, u->ag_top, "", "", 0, hints, 2 }; }
    else if (u->ag_phase == 1) { hints[0] = (Hint){ "A", "Choose" }; hints[1] = (Hint){ "B", "Back" }; *out = (AgView){ 1, places, u->ag_nplaces, u->ag_place, true, path, rows, u->ag_n, u->ag_sel, u->ag_top, u->ag_name, u->ag_exe, u->ag_opt, hints, 2 }; }
    else { hints[0] = (Hint){ "A", "Game mode" }; hints[1] = (Hint){ "B", "Stay here" }; *out = (AgView){ 2, places, u->ag_nplaces, u->ag_place, true, path, rows, u->ag_n, u->ag_sel, u->ag_top, u->ag_name, u->ag_exe, u->ag_opt, hints, 0 }; }
}

static void pointer_addgame(Ui *u, int x, int y, UiPtr ev, int button)
{
    if (ev != PTR_DOWN || button != BTN_LEFT) return;
    AgPlace places[UI_AG_PLACES]; AgRow rows[UI_AG_ENTRIES]; char note[UI_AG_ENTRIES][16]; char path[WD_PATH + 64]; Hint hints[3]; AgView av;
    ag_build(u, places, rows, note, path, sizeof path, hints, &av);
    AgHit h = view_addgame_hit(&av, x, y);
    if (u->ag_phase == 2) { if (h.button >= 0) ag_done(u, h.button); return; }
    if (h.place >= 0) { if (u->ag_phase == 1) u->ag_phase = 0; ag_place_set(u, h.place); return; }
    if (u->ag_phase == 1) { if (h.button >= 0) { u->ag_opt = h.button; ag_confirm(u, h.button); } return; }
    if (h.row >= 0) { u->ag_in = true; u->ag_sel = h.row; ag_scroll(u); ag_pick(u, h.row); }
}

/* ------------------------------------------------------------------ the on-screen keyboard */
static void osk_clamp(Ui *u)
{
    u->orow = clampi(u->orow, 0, 4);
    u->ocol = clampi(u->ocol, 0, view_osk_row_len(u->page, u->orow) - 1);
}

static void osk_move(Ui *u, int dir)                                         /* 0 up, 1 down, 2 left, 3 right */
{
    int n = view_osk_row_len(u->page, u->orow);
    if (dir == 2) u->ocol = u->ocol == 0 ? n - 1 : u->ocol - 1;
    else if (dir == 3) u->ocol = u->ocol == n - 1 ? 0 : u->ocol + 1;
    else {
        VRect k;
        int nr = clampi(u->orow + (dir == 0 ? -1 : 1), 0, 4);
        if (nr != u->orow && view_osk_rect(u->page, u->orow, u->ocol, &k)) {
            u->ocol = view_osk_col_near(u->page, nr, (k.x0 + k.x1) / 2);
            u->orow = nr;
        }
    }
    dirty(u, DIRTY_OSK);
}

static void osk_type_code(Ui *u, int code, bool shift)
{
    eff(u, EFF_TYPE, code, shift, u->ctrl, 0, NULL);
    u->ctrl = false;
    if (u->shift) { u->shift = false; if (u->page == OSK_UPPER) u->page = OSK_LOWER; }
    dirty(u, DIRTY_OSK);
}

static void osk_shift_toggle(Ui *u)
{
    if (u->page == OSK_SYMBOLS) return;
    u->shift = !u->shift;
    u->page = u->shift ? OSK_UPPER : OSK_LOWER;
    osk_clamp(u);
    dirty(u, DIRTY_OSK);
}

static void osk_press(Ui *u, int row, int col)
{
    OskKey k;
    if (!view_osk_key(u->page, u->shift, u->ctrl, row, col, &k)) return;
    const char *kind = k.kind ? k.kind : "";
    int code;
    bool sh;
    if (!strcmp(kind, "char")) { if (ui_char_key(k.label[0], &code, &sh)) osk_type_code(u, code, sh); }
    else if (!strcmp(kind, "del")) osk_type_code(u, KEY_BACKSPACE, false);
    else if (!strcmp(kind, "space")) osk_type_code(u, KEY_SPACE, false);
    else if (!strcmp(kind, "tab")) osk_type_code(u, KEY_TAB, false);
    else if (!strcmp(kind, "esc")) osk_type_code(u, KEY_ESC, false);
    else if (!strcmp(kind, "enter")) osk_type_code(u, KEY_ENTER, false);
    else if (!strcmp(kind, "left")) osk_type_code(u, KEY_LEFT, false);
    else if (!strcmp(kind, "right")) osk_type_code(u, KEY_RIGHT, false);
    else if (!strcmp(kind, "up")) osk_type_code(u, KEY_UP, false);
    else if (!strcmp(kind, "down")) osk_type_code(u, KEY_DOWN, false);
    else if (!strcmp(kind, "123")) { u->page = OSK_SYMBOLS; u->shift = false; osk_clamp(u); dirty(u, DIRTY_OSK); }
    else if (!strcmp(kind, "abc")) { u->page = OSK_LOWER; osk_clamp(u); dirty(u, DIRTY_OSK); }
    else if (!strcmp(kind, "shift")) osk_shift_toggle(u);
    else if (!strcmp(kind, "ctrl")) { u->ctrl = !u->ctrl; dirty(u, DIRTY_OSK); }
}

/* ------------------------------------------------------------------ pad commands */
static void dock_key(Ui *u, int code, unsigned mods);
static void toast_end(Ui *u, bool accept);

void ui_pad(Ui *u, const char *cmd)
{
    if (u->toast.on && u->toast.modal && (!strcmp(cmd, "summon") || !strcmp(cmd, "menu") || !strcmp(cmd, "qs") || !strcmp(cmd, "osk"))) toast_end(u, false);       /* asking for something else puts an offer away */
    if (u->settings && (!strcmp(cmd, "summon") || !strcmp(cmd, "menu") || !strcmp(cmd, "qs") || !strcmp(cmd, "osk"))) settings_close(u);       /* asking for the strip, the menu, the quick settings or the keyboard leaves the window */
    if (!strcmp(cmd, "addgame")) {
        if (u->settings && u->win == 1) settings_close(u); else ag_open(u);                  /* the same command closes its own window and replaces the other */
    } else if (!strcmp(cmd, "settings")) {
        if (u->settings && u->win == 0) settings_close(u); else settings_open(u);
    } else if (!strcmp(cmd, "summon")) {
        if (u->dock == UI_SUMMONED || u->dock == UI_ACTIONS_OPEN || u->dock == UI_QS) dock_close(u);
        else dock_summon(u, true);
    } else if (!strcmp(cmd, "menu")) {
        if (u->menu) menu_close(u); else menu_open(u);
    } else if (!strcmp(cmd, "qs")) {
        if (u->dock == UI_QS) dock_close(u); else qs_open(u, false);
    } else if (!strcmp(cmd, "osk")) {
        if (u->menu || u->dock == UI_SUMMONED || u->dock == UI_ACTIONS_OPEN) {                       /* X in a list: pin or unpin the selected one */
            if (u->menu) { if (u->msel >= 0 && u->msel < u->mn) { pin_toggle_app(u, u->mlist[u->msel]); dirty(u, DIRTY_MENU); } }
            else if (u->dock == UI_SUMMONED) item_pin_toggle(u, u->focus);
        } else if (u->dock == UI_QS) {
            /* nothing: the keyboard is not for a settings panel */
        } else osk_set(u, !u->osk);
    } else if (!strcmp(cmd, "scroll-on")) toast_text(u, "Right stick: scroll", NULL, 1500);
    else if (!strcmp(cmd, "scroll-off")) toast_text(u, "Right stick: pointer", NULL, 1500);
    else if (!strcmp(cmd, "actions")) eff(u, EFF_BROWSER_ACTIONS, 0, 0, 0, 0, NULL);
    else if (!strncmp(cmd, "nav-", 4)) {
        int d = !strcmp(cmd + 4, "up") ? 0 : !strcmp(cmd + 4, "down") ? 1 : !strcmp(cmd + 4, "left") ? 2 : 3;
        if (u->osk) osk_move(u, d);
        else ui_key(u, d == 0 ? KEY_UP : d == 1 ? KEY_DOWN : d == 2 ? KEY_LEFT : KEY_RIGHT, "", 0);
    } else if (!strcmp(cmd, "osk-type")) { if (u->osk) osk_press(u, u->orow, u->ocol); }
    else if (!strcmp(cmd, "osk-del")) { if (u->osk) osk_type_code(u, KEY_BACKSPACE, false); }
    else if (!strcmp(cmd, "osk-space")) { if (u->osk) osk_type_code(u, KEY_SPACE, false); }
    else if (!strcmp(cmd, "osk-enter")) { if (u->osk) osk_type_code(u, KEY_ENTER, false); }
    else if (!strcmp(cmd, "osk-shift")) { if (u->osk) osk_shift_toggle(u); }
    else if (!strcmp(cmd, "osk-hide")) osk_set(u, false);
}

/* ------------------------------------------------------------------ keys */
static void dock_key(Ui *u, int code, unsigned mods)
{
    if (u->dock == UI_QS) { qs_key(u, code); return; }
    if (u->dock == UI_ACTIONS_OPEN) {
        switch (code) {
        case KEY_UP: u->act_sel = clampi(u->act_sel - 1, 0, u->nacts - 1); dirty(u, DIRTY_DOCK); break;
        case KEY_DOWN: u->act_sel = clampi(u->act_sel + 1, 0, u->nacts - 1); dirty(u, DIRTY_DOCK); break;
        case KEY_ENTER: case KEY_KPENTER: action_perform(u, u->acts[u->act_sel]); break;
        case KEY_ESC: u->dock = UI_SUMMONED; u->nacts = 0; dirty(u, DIRTY_DOCK); break;
        default: break;
        }
        return;
    }
    switch (code) {                                                          /* summoned */
    case KEY_LEFT: set_focus(u, wd_dock_move(u->items, u->n, u->focus, -1)); break;
    case KEY_RIGHT: set_focus(u, wd_dock_move(u->items, u->n, u->focus, 1)); break;
    case KEY_TAB: set_focus(u, wd_dock_move(u->items, u->n, u->focus, (mods & UI_SHIFT) ? -1 : 1)); break;
    case KEY_ENTER: case KEY_KPENTER: item_activate(u, u->focus); break;
    case KEY_ESC: dock_close(u); break;
    case KEY_SPACE: actions_open(u, u->focus); break;                        /* Y: More */
    case KEY_MENU: actions_open(u, u->focus); break;
    default: break;
    }
}

void ui_key(Ui *u, int code, const char *utf8, unsigned mods)
{
    if (u->toast.on && u->toast.modal) {                                    /* an offer holds the keyboard: A takes it, B puts it away, nothing else gets through */
        if (code == KEY_ENTER || code == KEY_KPENTER) toast_end(u, true);
        else if (code == KEY_ESC) toast_end(u, false);
        return;
    }
    if (u->settings) { if (u->win == 1) ag_key(u, code, mods); else settings_key(u, code, mods); return; }
    if (u->menu) { menu_key(u, code, utf8, mods); return; }
    if (u->dock != UI_IDLE) { dock_key(u, code, mods); return; }
    /* no panel is open: the shell only has the keyboard for shortcuts */
    if ((mods & UI_SUPER) && code == KEY_D) dock_summon(u, false);
    else if ((mods & UI_SUPER) && code == KEY_S) qs_open(u, false);
    else if ((mods & UI_SUPER) && code == KEY_K) osk_set(u, !u->osk);
    else if ((mods & UI_SUPER) && code == KEY_A) eff(u, EFF_BROWSER_ACTIONS, 0, 0, 0, 0, NULL);
    else if (code == KEY_LEFTMETA || code == KEY_RIGHTMETA) { /* a lone Super opens the menu on release: handled by the compositor's binding */ }
}

/* ------------------------------------------------------------------ the pointer */
static void pointer_dock(Ui *u, int x, int y, UiPtr ev, int button)
{
    const int top = WISP_UI_H - WISP_UI_DOCK_H;
    bool in_strip = y >= top;
    int hit = in_strip ? wd_dock_hit(u->items, u->n, x, y - top) : -1;
    if (ev == PTR_LEAVE) { u->hover = u->press = -1; u->dragging = false; dirty(u, DIRTY_DOCK); return; }

    if (u->dock == UI_QS) {
        QsRow rows[UI_QS_ROWS];
        char text[UI_QS_ROWS][64];
        Hint hints[2];
        QsView q;
        qs_build(u, rows, text, hints, &q);
        QsHit h = view_quick_hit(&q, x, y);
        if (ev == PTR_MOVE) {
            if (u->dragging) {
                VRect pr = view_quick_rect(&q);
                float t = clampf((float)(x - (pr.x0 + 62)) / (float)(WISP_UI_QS_W - 28 - 62), 0.f, 1.f);
                if (u->qs_sel == 2) { u->sys.volume = t; eff(u, EFF_SET_VOLUME, 0, 0, 0, t, NULL); }
                else if (u->qs_sel == 3) { u->sys.brightness = clampf(t, BRIGHTNESS_MIN, 1.f); eff(u, EFF_SET_BRIGHTNESS, 0, 0, 0, u->sys.brightness, NULL); }
                dirty(u, DIRTY_DOCK);
            } else if (h.row >= 0 && qs_selectable(u, h.row) && u->qs_sel != h.row) {
                u->qs_sel = h.row;
                dirty(u, DIRTY_DOCK);
            }
            if (in_strip && hit != u->hover) { u->hover = hit; dirty(u, DIRTY_DOCK); }
        } else if (ev == PTR_DOWN && button == BTN_LEFT) {
            if (h.row >= 0 && qs_selectable(u, h.row)) {
                u->qs_sel = h.row;
                if (u->qs_page == QS_PAGE_USB) qs_change(u);
                else if (h.row == 2 || h.row == 3) {
                    u->dragging = true;
                    if (h.row == 2) { u->sys.volume = h.frac; eff(u, EFF_SET_VOLUME, 0, 0, 0, h.frac, NULL); }
                    else { u->sys.brightness = clampf(h.frac, BRIGHTNESS_MIN, 1.f); eff(u, EFF_SET_BRIGHTNESS, 0, 0, 0, u->sys.brightness, NULL); }
                } else qs_change(u);
                dirty(u, DIRTY_DOCK);
            } else if (h.row == -1) {                                                                  /* outside the panel */
                if (in_strip && hit >= 0 && u->items[hit].kind != WD_STATUS) { dock_close(u); item_activate(u, hit); }
                else dock_close(u);                                                                    /* the status group again closes it, as does a click anywhere else */
            }
        } else if (ev == PTR_UP) u->dragging = false;
        return;
    }

    if (u->dock == UI_ACTIONS_OPEN) {
        if (u->focus >= 0) {
            int row = view_popup_hit(u->items[u->focus].x - 10, u->nacts, 214, x, y);
            if (ev == PTR_MOVE && row >= 0 && row != u->act_sel) { u->act_sel = row; dirty(u, DIRTY_DOCK); }
            if (ev == PTR_DOWN && button == BTN_LEFT) {
                if (row >= 0) action_perform(u, u->acts[row]);
                else if (row == -1) dock_close(u);
            } else if (ev == PTR_DOWN && button == BTN_RIGHT && row == -1) dock_close(u);
        }
        return;
    }

    if (ev == PTR_MOVE) {
        if (hit != u->hover) { u->hover = hit; dirty(u, DIRTY_DOCK); }
        if (u->dock == UI_SUMMONED && hit >= 0 && hit != u->focus) set_focus(u, hit);
        return;
    }
    if (ev == PTR_DOWN) {
        if (u->dock == UI_SUMMONED && !in_strip) { dock_close(u); return; }                         /* a click outside the summoned dock */
        if (hit >= 0 && button == BTN_LEFT) { u->press = hit; dirty(u, DIRTY_DOCK); }
        else if (hit >= 0 && button == BTN_RIGHT) { if (u->dock == UI_IDLE) u->from_pad = false; actions_open(u, hit); }
        return;
    }
    if (ev == PTR_UP && button == BTN_LEFT) {
        int pressed = u->press;
        u->press = -1;
        dirty(u, DIRTY_DOCK);
        if (pressed >= 0 && pressed == hit) item_activate(u, hit);
    }
}

static void pointer_menu(Ui *u, int x, int y, UiPtr ev, int button)
{
    if (ev == PTR_LEAVE) return;
    MenuView m = { NULL, u->mn, u->msel, u->mtop, NULL, NULL, NULL, 0, NULL };
    int hit = view_menu_hit(&m, x, y);
    if (ev == PTR_MOVE && hit >= 0 && hit != u->msel) { u->msel = hit; dirty(u, DIRTY_MENU); }
    else if (ev == PTR_DOWN) {
        if (hit < 0) { if (button == BTN_LEFT) menu_close(u); }                                        /* a click between the tiles closes the menu */
        else if (button == BTN_LEFT) { u->msel = hit; menu_launch(u); }
        else if (button == BTN_RIGHT) { pin_toggle_app(u, u->mlist[hit]); dirty(u, DIRTY_MENU); }
    }
}

static void pointer_osk(Ui *u, int x, int y, UiPtr ev, int button)
{
    int row, col;
    if (ev == PTR_LEAVE) return;
    bool hit = view_osk_hit(u->page, x, y, &row, &col);
    if (ev == PTR_MOVE && hit && (row != u->orow || col != u->ocol)) { u->orow = row; u->ocol = col; dirty(u, DIRTY_OSK); }
    else if (ev == PTR_DOWN && button == BTN_LEFT && hit) { u->orow = row; u->ocol = col; osk_press(u, row, col); dirty(u, DIRTY_OSK); }
}

void ui_pointer(Ui *u, UiSurf s, int x, int y, UiPtr ev, int button)
{
    if (s == SURF_TOAST) {
        (void)x; (void)y;
        if (ev == PTR_DOWN && u->toast.on && u->toast.modal) toast_end(u, button == BTN_LEFT);
        return;
    }
    if (s == SURF_SETTINGS) { if (u->settings) { if (u->win == 1) pointer_addgame(u, x, y, ev, button); else pointer_settings(u, x, y, ev, button); } return; }
    if (s == SURF_DOCK && u->settings && ev == PTR_DOWN) settings_close(u);                    /* a press on the strip leaves the window and then acts */
    if (s == SURF_DOCK) { if (!u->menu && !u->osk) pointer_dock(u, x, y, ev, button); }      /* the dock is covered by the menu and hidden by the keyboard */
    else if (s == SURF_MENU) { if (u->menu) pointer_menu(u, x, y, ev, button); }
    else if (u->osk) pointer_osk(u, x, y, ev, button);
}

void ui_wheel(Ui *u, UiSurf s, int steps)
{
    if (s == SURF_SETTINGS && u->settings && u->win == 1 && u->ag_phase == 0) {                  /* the listing scrolls under the wheel */
        int max_top = u->ag_n > AG_ROWS_VISIBLE ? u->ag_n - AG_ROWS_VISIBLE : 0;
        u->ag_top = clampi(u->ag_top - steps, 0, max_top);
        dirty(u, DIRTY_SETTINGS);
        return;
    }
    if (s == SURF_DOCK && (u->menu || u->osk)) return;
    if (s == SURF_MENU && u->menu) {
        int rows = (u->mn + WISP_UI_MENU_COLS - 1) / WISP_UI_MENU_COLS, vis = view_menu_rows_visible(), max_top = rows > vis ? rows - vis : 0;
        u->mtop = clampi(u->mtop - steps, 0, max_top);
        dirty(u, DIRTY_MENU);
    } else if (s == SURF_DOCK && u->dock == UI_QS && (u->qs_sel == 2 || u->qs_sel == 3)) {
        qs_adjust(u, steps > 0 ? 1 : -1);
    } else if (s == SURF_DOCK && u->dock == UI_ACTIONS_OPEN) {
        u->act_sel = clampi(u->act_sel - steps, 0, u->nacts - 1);
        dirty(u, DIRTY_DOCK);
    }
}

/* ------------------------------------------------------------------ time, toasts, geometry */
void ui_toast(Ui *u, const char *title, const char *text, int ttl_ms)
{
    u->toast.on = true;
    u->toast.modal = false;
    u->toast.action = 0;
    u->toast.arg[0] = 0;
    u->toast.label[0] = 0;
    snprintf(u->toast.title, sizeof u->toast.title, "%s", title);
    snprintf(u->toast.text, sizeof u->toast.text, "%s", text ? text : "");
    u->toast.until = u->now + ttl_ms;
    dirty(u, DIRTY_TOAST);
}

void ui_toast_action(Ui *u, const char *title, const char *text, const char *label, int action, const char *arg, int ttl_ms)
{
    ui_toast(u, title, text, ttl_ms);
    u->toast.modal = true;
    u->toast.action = action;
    snprintf(u->toast.label, sizeof u->toast.label, "%s", label ? label : "");
    snprintf(u->toast.arg, sizeof u->toast.arg, "%s", arg ? arg : "");
    dirty(u, DIRTY_TOAST);
}

static void toast_end(Ui *u, bool accept)
{
    if (!u->toast.on) return;
    if (accept && u->toast.modal && u->toast.label[0]) eff(u, EFF_DOWNLOAD, u->toast.action, 0, 0, 0, u->toast.arg);
    u->toast.on = false;
    u->toast.modal = false;
    dirty(u, DIRTY_TOAST);
}

void ui_tick(Ui *u, int64_t now_ms)
{
    u->now = now_ms;
    if (u->toast.on && now_ms >= u->toast.until) toast_end(u, false);
}

int ui_next_wake(const Ui *u)
{
    if (!u->toast.on) return -1;
    int64_t d = u->toast.until - u->now;
    return d < 1 ? 1 : d > 100000 ? 100000 : (int)d;
}

UiGeom ui_geometry(const Ui *u)
{
    UiGeom g = { 0 };
    g.dock_visible = !u->osk;
    g.dock_modal = u->dock != UI_IDLE;
    g.dock_h = g.dock_modal ? WISP_UI_H : WISP_UI_DOCK_H;
    g.menu_visible = u->menu;
    g.settings_visible = u->settings;
    g.settings_h = view_settings_height();
    g.osk_visible = u->osk;
    g.toast_visible = u->toast.on;
    g.toast_modal = u->toast.on && u->toast.modal;
    if (u->toast.on && u->fonts) {
        Hint th[2] = { { "A", u->toast.label }, { "B", "Dismiss" } };
        ToastView t = { u->toast.title, u->toast.text[0] ? u->toast.text : NULL, u->toast.modal ? th : NULL, u->toast.modal ? 2 : 0 };
        view_toast_size(u->fonts, &t, &g.toast_w, &g.toast_h);
    }
    return g;
}

unsigned ui_take_dirty(Ui *u) { unsigned d = u->dirty; u->dirty = 0; return d; }

int ui_take_effects(Ui *u, UiEffect *out, int cap)
{
    int n = u->neff < cap ? u->neff : cap;
    memcpy(out, u->eff, (size_t)n * sizeof *out);
    if (n < u->neff) memmove(u->eff, u->eff + n, (size_t)(u->neff - n) * sizeof *u->eff);
    u->neff -= n;
    return n;
}

/* ------------------------------------------------------------------ views */
static IconSpec icon_for(const Ui *u, const WdApp *app, const char *key, const char *label)
{
    if (u->icon_fn) return u->icon_fn(u->icon_ctx, app, key);
    IconSpec s = { 0 };
    s.letter = wd_tile_letter(label && label[0] ? label : key);
    s.letter_ink = RD_RGB(WISP_UI_GLYPH);
    return s;
}

static int volume_level(const UiSys *s) { return s->volume <= 0.001f ? 0 : s->volume < 0.5f ? 1 : 2; }

static void fmt_into(char *dst, size_t cap, const char *fmt, ...) __attribute__((format(printf, 3, 4)));
static void fmt_into(char *dst, size_t cap, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(dst, cap, fmt, ap);
    va_end(ap);
}

static const char *bar_label(const Ui *u, const WdItem *it, char which)
{
    bool pinned = it->kind == WD_PIN;
    switch (which) {
    case 'A': return it->kind == WD_GAME ? "Game mode" : it->kind == WD_STATUS ? "Settings" : (it->running ? "Switch" : "Open");
    case 'X': return pinned ? "Unpin" : "Pin";
    default: (void)u; return "";
    }
}

static const char *const USB_NAMES[3] = { "Off", "Computer", "Host" };
static const char *const USB_NOTES[3] = { "port unpowered, no data", "the handheld is a device", "powers a connected device" };

static void qs_build(const Ui *u, QsRow rows[UI_QS_ROWS], char text[UI_QS_ROWS][64], Hint hints[2], QsView *out)
{
    const UiSys *s = &u->sys;
    int vol = volume_level(s);
    fmt_into(text[0], 64, "%s", s->ssid[0] ? s->ssid : "Not connected");
    rows[0] = (QsRow){ QS_LINK, "wifi", s->wifi, "Wi-Fi", text[0], false, 0 };
    rows[1] = (QsRow){ QS_SWITCH, "bluetooth", 0, "Bluetooth", s->bt_available ? NULL : "Not available", s->bt_on, 0 };
    rows[2] = (QsRow){ QS_SLIDER, "volume", vol, "Volume", NULL, false, s->volume };
    rows[3] = (QsRow){ QS_SLIDER, "brightness", 0, "Brightness", NULL, false, s->brightness };
    rows[4] = (QsRow){ QS_SWITCH, "ssh", 0, "SSH", s->ssh_addr[0] ? s->ssh_addr : NULL, s->ssh_on, 0 };
    rows[5] = (QsRow){ QS_LINK, "usb", 0, "USB port", USB_NAMES[clampi(s->usb, 0, 2)], false, 0 };
    if (s->battery >= 0.f) fmt_into(text[6], 64, "%d %%  %s", (int)(s->battery * 100.f + 0.5f), s->charging ? "charging" : "discharging");
    else fmt_into(text[6], 64, "No battery");
    rows[6] = (QsRow){ QS_TEXT, "battery", 0, "Battery", text[6], false, s->battery < 0.f ? 0.f : s->battery };
    QsView list = { rows, UI_QS_ROWS, u->qs_sel, hints, 2, 0 };
    if (u->qs_page == QS_PAGE_LIST) {
        hints[0] = (Hint){ "A", "Change" };
        hints[1] = (Hint){ "B", "Close" };
        *out = list;
        return;
    }
    int height = view_quick_height(&list);                                   /* the submenu keeps the list's size and place: nothing moves when it opens */
    rows[0] = (QsRow){ QS_BACK, NULL, 0, "USB port", NULL, false, 0 };
    for (int k = 0; k < 3; k++) rows[1 + k] = (QsRow){ QS_CHOICE, NULL, 0, USB_NAMES[k], USB_NOTES[k], s->usb == k, 0 };
    rows[4] = (QsRow){ QS_NOTE, NULL, 0, "Off again at every boot.", NULL, false, 0 };
    hints[0] = (Hint){ "A", "Choose" };
    hints[1] = (Hint){ "B", "Back" };
    *out = (QsView){ rows, 5, u->qs_sel, hints, 2, height };
}

void ui_make_views(const Ui *u, UiViews *vw)
{
    memset(vw, 0, sizeof *vw);
    /* the strip */
    for (int i = 0; i < u->n; i++) {
        const WdItem *it = &u->items[i];
        if (it->kind == WD_APPS) vw->icons[i] = icon_for(u, NULL, "apps", "Applications");
        else if (it->kind == WD_GAME) vw->icons[i] = icon_for(u, NULL, "game", "Game mode");
        else if (it->kind == WD_PIN || it->kind == WD_WINDOW) vw->icons[i] = icon_for(u, item_app(u, it), it->app >= 0 ? u->apps->v[it->app].id : it->key, it->label);
    }
    int vol = volume_level(&u->sys);
    vw->dock = (DockView){ u->items, vw->icons, u->n, u->dock != UI_IDLE ? u->focus : -1, u->hover, u->press, u->dock == UI_SUMMONED || u->dock == UI_ACTIONS_OPEN, u->clock,
                           u->sys.wifi, vol, u->sys.battery < 0.f ? 1.f : u->sys.battery };
    /* the bar above it while it is summoned */
    if ((u->dock == UI_SUMMONED || u->dock == UI_ACTIONS_OPEN) && u->focus >= 0) {
        const WdItem *it = &u->items[u->focus];
        vw->bar_visible = true;
        snprintf(vw->bar_name, sizeof vw->bar_name, "%s", it->label);
        if (u->dock == UI_ACTIONS_OPEN) {
            vw->bar[vw->nbar++] = (Hint){ "A", "Select" };
        } else if (it->kind == WD_PIN || it->kind == WD_WINDOW) {
            vw->bar[vw->nbar++] = (Hint){ "A", bar_label(u, it, 'A') };
            if (it->app >= 0) vw->bar[vw->nbar++] = (Hint){ "X", bar_label(u, it, 'X') };
            vw->bar[vw->nbar++] = (Hint){ "Y", "More" };
        } else {
            vw->bar[vw->nbar++] = (Hint){ "A", bar_label(u, it, 'A') };
        }
        vw->bar[vw->nbar++] = (Hint){ "B", "Back" };
    }
    /* the actions list */
    if (u->dock == UI_ACTIONS_OPEN && u->focus >= 0) {
        const WdItem *it = &u->items[u->focus];
        vw->npopup = u->nacts;
        vw->popup_x = it->x - 10;
        for (int i = 0; i < u->nacts; i++) {
            WdAction a = u->acts[i];
            IconSpec ic = { 0 };
            if (a == WD_ACT_OPEN) ic = vw->icons[u->focus];
            else { ic.glyph = a == WD_ACT_PIN || a == WD_ACT_UNPIN ? "pin" : a == WD_ACT_LEFT ? "left" : a == WD_ACT_RIGHT ? "right" : "close"; ic.ink = RD_RGB(WISP_UI_GLYPH); }
            vw->popup[i] = (PopupRow){ ic, wd_action_label(a) };
        }
    }
    /* the menu */
    vw->menu_hints[0] = (Hint){ "A", "Open" };
    {
        bool pinned = u->menu && u->msel >= 0 && u->msel < u->mn && wd_pins_find(&u->pins, u->apps->v[u->mlist[u->msel]].id) >= 0;
        vw->menu_hints[1] = (Hint){ "X", pinned ? "Unpin" : "Pin" };
    }
    vw->menu_hints[2] = (Hint){ "Y", "Search" };
    vw->menu_hints[3] = (Hint){ "B", "Back" };
    for (int i = 0; i < u->mn; i++) {
        const WdApp *a = &u->apps->v[u->mlist[i]];
        vw->menu_e[i] = (MenuEntry){ icon_for(u, a, a->id, a->name), a->name, wd_pins_find(&u->pins, a->id) >= 0 };
    }
    vw->menu = (MenuView){ vw->menu_e, u->mn, u->msel, u->mtop, "Applications", "Pick an app. X pins it to the dock.", vw->menu_hints, 4, u->query[0] ? u->query : NULL };
    qs_build(u, vw->qs_rows, vw->qs_text, vw->qs_hints, &vw->qs);
    set_build(u, vw->set_pages, vw->set_rows, vw->set_note, vw->set_hints, &vw->settings);
    ag_build(u, vw->ag_places, vw->ag_rows, vw->ag_note, vw->ag_path, sizeof vw->ag_path, vw->ag_hints, &vw->ag);
    vw->osk = (OskView){ u->page, u->shift, u->ctrl, u->orow, u->ocol };
    vw->toast_hints[0] = (Hint){ "A", u->toast.label };
    vw->toast_hints[1] = (Hint){ "B", "Dismiss" };
    vw->toast = (ToastView){ u->toast.title, u->toast.text[0] ? u->toast.text : NULL, u->toast.modal ? vw->toast_hints : NULL, u->toast.modal ? 2 : 0 };
}

void ui_draw_dock(const Ui *u, const Vctx *v, const UiViews *vw)
{
    view_dock(v, &vw->dock);
    if (vw->bar_visible) view_focus_bar(v, vw->bar_name, vw->bar, vw->nbar);
    if (u->dock == UI_ACTIONS_OPEN) view_popup(v, vw->popup_x, vw->popup, vw->npopup, u->act_sel, 214);
    if (u->dock == UI_QS) view_quick(v, &vw->qs);
}

void ui_draw_settings(const Ui *u, const Vctx *v, const UiViews *vw)
{
    if (u->win == 1) view_addgame(v, &vw->ag);
    else view_settings(v, &vw->settings);
}
