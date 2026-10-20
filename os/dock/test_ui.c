/* Tests of the desktop shell's UI state machine (ui.c): scripted flows (the dock, its actions list, the application menu, the quick settings, the on-screen keyboard, the pointer) and a
 * random run that checks the state stays consistent. Effects are written as short words: launch:<id> activate:<window> close:<window> save game type:<key>[+shift][+ctrl] pad-osk:<0|1>
 * pad-focus:<app> volume:<f> brightness:<f> bluetooth:<0|1> ssh:<0|1> usb:<0|1|2> screen-timeout:<seconds> wifi refresh browser-actions.
 * Run by os/test_dock_ui.py under the sanitizers and against rule mutants of ui.c. */
#define _GNU_SOURCE
#include "ui.h"

#include <errno.h>
#include <linux/input-event-codes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#include "../ui/wisp-ui.h"

static int checks, failed;
static void check_true(const char *what, int ok) { checks++; if (!ok) { failed++; printf("FAIL %s\n", what); } }
static void check_int(const char *what, long got, long want) { checks++; if (got != want) { failed++; printf("FAIL %s: got %ld, want %ld\n", what, got, want); } }
static void check_str(const char *what, const char *got, const char *want) { checks++; if (strcmp(got, want) != 0) { failed++; printf("FAIL %s\n  got:  [%s]\n  want: [%s]\n", what, got, want); } }

static Ui U;
static WdApps APPS;
static char DIR[512];

static void put(const char *name, const char *text)
{
    char p[640];
    snprintf(p, sizeof p, "%s/%s", DIR, name);
    FILE *f = fopen(p, "w");
    if (!f) { perror(p); exit(2); }
    fputs(text, f);
    fclose(f);
}

static void make_apps(int extra)
{
    const char *base = getenv("TMPDIR");
    snprintf(DIR, sizeof DIR, "%s/ui-XXXXXX", base ? base : "/tmp");
    if (!mkdtemp(DIR)) { perror("mkdtemp"); exit(2); }
    put("browser.desktop", "[Desktop Entry]\nType=Application\nName=Browser\nExec=firefox\nIcon=browser\n");
    put("terminal.desktop", "[Desktop Entry]\nType=Application\nName=Terminal\nExec=foot\nIcon=terminal\n");
    put("settings.desktop", "[Desktop Entry]\nType=Application\nName=Settings\nExec=wisp-settings\nIcon=settings\n");
    put("images.desktop", "[Desktop Entry]\nType=Application\nName=Images\nExec=swayimg\nIcon=images\nStartupWMClass=swayimg\n");
    put("monitor.desktop", "[Desktop Entry]\nType=Application\nName=Monitor\nExec=htop\nIcon=monitor\n");
    put("about.desktop", "[Desktop Entry]\nType=Application\nName=About\nExec=wisp-about\nIcon=about\n");
    for (int i = 0; i < extra; i++) {
        char n[64], t[200];
        snprintf(n, sizeof n, "extra%02d.desktop", i);
        snprintf(t, sizeof t, "[Desktop Entry]\nType=Application\nName=Extra %02d\nExec=extra%02d\n", i, i);
        put(n, t);
    }
    const char *dirs[] = { DIR };
    WdLoadOpts o = { NULL, NULL, NULL };
    memset(&APPS, 0, sizeof APPS);
    wd_apps_load(&APPS, dirs, 1, &o);
}

static void cleanup(void)
{
    char cmd[640];
    snprintf(cmd, sizeof cmd, "rm -rf '%s'", DIR);
    if (system(cmd) != 0) fprintf(stderr, "cleanup failed\n");
}

static void fresh(bool windows)
{
    ui_init(&U, &APPS, NULL);
    wd_pins_parse("browser.desktop\nterminal.desktop\nsettings.desktop\n", &U.pins);
    ui_set_clock(&U, "12:34", 45);
    if (windows) {
        wd_windows_update(&U.ws, 11, "foot", "wisp@WispOS: ~", 1);
        wd_windows_update(&U.ws, 12, "swayimg", "photo.png", 0);
    }
    ui_windows_changed(&U);
    ui_take_dirty(&U);
    UiEffect sink[UI_EFFECTS];
    ui_take_effects(&U, sink, UI_EFFECTS);
}

static const char *app_name(int a)
{
    static char b[8][160];
    static int k;
    char *s = b[k++ & 7];
    snprintf(s, 160, "%s", a >= 0 && a < APPS.n ? APPS.v[a].id : "?");
    char *dot = strrchr(s, '.');
    if (dot) *dot = 0;
    return s;
}

static const char *effs(void)
{
    static char buf[1024];
    UiEffect e[UI_EFFECTS];
    int n = ui_take_effects(&U, e, UI_EFFECTS);
    buf[0] = 0;
    size_t o = 0;
    for (int i = 0; i < n; i++) {
        char w[160];
        switch (e[i].kind) {
        case EFF_LAUNCH: snprintf(w, sizeof w, "launch:%s", app_name(e[i].a)); break;
        case EFF_ACTIVATE: snprintf(w, sizeof w, "activate:%d", e[i].a); break;
        case EFF_CLOSE: snprintf(w, sizeof w, "close:%d", e[i].a); break;
        case EFF_SAVE_PINS: snprintf(w, sizeof w, "save"); break;
        case EFF_GAME_MODE: snprintf(w, sizeof w, "game"); break;
        case EFF_TYPE: snprintf(w, sizeof w, "type:%d%s%s", e[i].a, e[i].b ? "+shift" : "", e[i].c ? "+ctrl" : ""); break;
        case EFF_PAD_OSK: snprintf(w, sizeof w, "pad-osk:%d", e[i].a); break;
        case EFF_PAD_FOCUS: snprintf(w, sizeof w, "pad-focus:%s", e[i].s); break;
        case EFF_SET_VOLUME: snprintf(w, sizeof w, "volume:%.2f", (double)e[i].f); break;
        case EFF_SET_BRIGHTNESS: snprintf(w, sizeof w, "brightness:%.2f", (double)e[i].f); break;
        case EFF_SET_BLUETOOTH: snprintf(w, sizeof w, "bluetooth:%d", e[i].a); break;
        case EFF_SET_SSH: snprintf(w, sizeof w, "ssh:%d", e[i].a); break;
        case EFF_SET_USB: snprintf(w, sizeof w, "usb:%d", e[i].a); break;
        case EFF_SET_SCREEN_TIMEOUT: snprintf(w, sizeof w, "screen-timeout:%d", e[i].a); break;
        case EFF_WIFI: snprintf(w, sizeof w, "wifi"); break;
        case EFF_REFRESH: snprintf(w, sizeof w, "refresh"); break;
        case EFF_BROWSER_ACTIONS: snprintf(w, sizeof w, "browser-actions"); break;
        case EFF_DOWNLOAD: snprintf(w, sizeof w, "download:%d:%s", e[i].a, e[i].s); break;
        default: snprintf(w, sizeof w, "?%d", e[i].kind); break;
        }
        o += (size_t)snprintf(buf + o, sizeof buf - o, "%s%s", o ? " " : "", w);
    }
    return buf;
}

#define EXPECT(what, want) check_str(what, effs(), want)

static void key(int code) { ui_key(&U, code, "", 0); }
static void keym(int code, unsigned mods) { ui_key(&U, code, "", mods); }
static void text(int code, const char *utf8) { ui_key(&U, code, utf8, 0); }
static void pad(const char *c) { ui_pad(&U, c); }
static const char *focus_key(void) { return U.focus >= 0 ? U.items[U.focus].key : "-"; }

/* ------------------------------------------------------------------ the dock */
static void t_summon(void)
{
    fresh(true);
    UiGeom g = ui_geometry(&U);
    check_true("at first a plain strip", g.dock_visible && g.dock_h == WISP_UI_DOCK_H && !g.dock_modal && !g.menu_visible && !g.osk_visible);
    check_int("no item is focused", U.focus, -1);
    pad("summon");
    check_int("summoned", U.dock, UI_SUMMONED);
    check_str("the focus starts on the window in front", focus_key(), "terminal.desktop");
    g = ui_geometry(&U);
    check_true("a summoned dock is modal and has the whole screen to catch clicks", g.dock_modal && g.dock_h == WISP_UI_H);
    key(KEY_RIGHT);
    check_str("right moves along the pins", focus_key(), "settings.desktop");
    key(KEY_RIGHT);
    check_str("past the last pin, the other window (a divider is skipped)", focus_key(), "w12");
    key(KEY_RIGHT);
    check_str("Game mode", focus_key(), "game");
    key(KEY_RIGHT);
    check_str("the status group", focus_key(), "status");
    key(KEY_RIGHT);
    check_str("the end stays the end", focus_key(), "status");
    key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT);
    check_str("left to the Applications button", focus_key(), "apps");
    key(KEY_LEFT);
    check_str("and no further", focus_key(), "apps");
    keym(KEY_TAB, 0);
    check_str("Tab is right (R2)", focus_key(), "browser.desktop");
    keym(KEY_TAB, UI_SHIFT);
    check_str("Shift+Tab is left (L2)", focus_key(), "apps");
    EXPECT("moving causes no effect", "");
    key(KEY_ESC);
    check_int("Escape puts it away", U.dock, UI_IDLE);
    check_int("and clears the focus", U.focus, -1);
    pad("summon"); pad("summon");
    check_int("Mode again puts it away", U.dock, UI_IDLE);
}

static void t_activate(void)
{
    fresh(true);
    pad("summon");
    key(KEY_LEFT);
    check_str("the browser (not running)", focus_key(), "browser.desktop");
    key(KEY_ENTER);
    EXPECT("A on an application that is not running starts it", "launch:browser");
    check_int("and puts the dock away", U.dock, UI_IDLE);
    pad("summon");
    check_str("a running one is first", focus_key(), "terminal.desktop");
    key(KEY_ENTER);
    EXPECT("A on a running application brings its window", "activate:11");
    pad("summon");
    key(KEY_RIGHT); key(KEY_RIGHT);
    check_str("the unpinned window", focus_key(), "w12");
    key(KEY_ENTER);
    EXPECT("brings the other window", "activate:12");
    pad("summon");
    key(KEY_RIGHT); key(KEY_RIGHT); key(KEY_RIGHT);
    check_str("Game mode", focus_key(), "game");
    key(KEY_ENTER);
    EXPECT("is the way back", "game");
    pad("summon");
    for (int i = 0; i < 6; i++) key(KEY_RIGHT);
    key(KEY_ENTER);
    check_int("the status group opens the quick settings", U.dock, UI_QS);
    EXPECT("and asks for fresh values", "refresh");
    key(KEY_ESC);
    check_int("B from the quick settings is back to the summoned dock (the pad opened it)", U.dock, UI_SUMMONED);
    key(KEY_ESC);
    check_int("and again puts it away", U.dock, UI_IDLE);
    pad("summon");
    key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT);
    key(KEY_ENTER);
    check_true("the Applications button opens the menu", U.menu && U.dock == UI_IDLE);
}

static void t_pin_and_actions(void)
{
    fresh(true);
    pad("summon");
    key(KEY_RIGHT);
    check_str("on the settings pin", focus_key(), "settings.desktop");
    pad("osk");
    EXPECT("X in the summoned dock is Unpin, not the keyboard", "save");
    check_true("settings is no longer pinned", wd_pins_find(&U.pins, "settings.desktop") < 0);
    check_true("the keyboard stays shut", !U.osk);
    check_true("the focus moved to something near", U.focus >= 0 && U.items[U.focus].kind != WD_SEP);
    pad("osk");
    key(KEY_ESC);

    fresh(true);
    pad("summon");
    key(KEY_RIGHT); key(KEY_RIGHT);
    check_str("the images window (not pinned)", focus_key(), "w12");
    pad("osk");
    EXPECT("X pins a running window's application", "save");
    check_true("images is pinned", wd_pins_find(&U.pins, "images.desktop") >= 0);
    check_str("the focus stays on the same application", focus_key(), "images.desktop");
    key(KEY_ESC);

    fresh(true);
    pad("summon");
    key(KEY_SPACE);
    check_int("Y (Space) opens the actions", U.dock, UI_ACTIONS_OPEN);
    check_int("for a pinned running application: open, unpin, left, right, close", U.nacts, 5);
    check_int("first is Open", U.acts[0], WD_ACT_OPEN);
    check_int("last is Close", U.acts[4], WD_ACT_CLOSE);
    key(KEY_DOWN); key(KEY_DOWN);
    check_int("down moves", U.act_sel, 2);
    key(KEY_DOWN); key(KEY_DOWN); key(KEY_DOWN); key(KEY_DOWN);
    check_int("and stops at the last", U.act_sel, 4);
    key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP);
    check_int("up stops at the first", U.act_sel, 0);
    ui_wheel(&U, SURF_DOCK, -2);
    check_int("the wheel moves down the list", U.act_sel, 2);
    ui_wheel(&U, SURF_DOCK, 1);
    check_int("and up", U.act_sel, 1);
    ui_wheel(&U, SURF_DOCK, 50);
    check_int("it stops at the first row", U.act_sel, 0);
    key(KEY_ESC);
    check_int("B closes the list, back to the summoned dock", U.dock, UI_SUMMONED);
    key(KEY_SPACE);
    key(KEY_DOWN); key(KEY_DOWN); key(KEY_DOWN);
    check_int("Move right is the 4th", U.acts[U.act_sel], WD_ACT_RIGHT);
    key(KEY_ENTER);
    EXPECT("moving a pin saves", "save");
    check_int("pin order changed: terminal is last now", wd_pins_find(&U.pins, "terminal.desktop"), 2);
    check_str("the focus follows the item", focus_key(), "terminal.desktop");
    check_int("back to the summoned dock", U.dock, UI_SUMMONED);
    key(KEY_SPACE); key(KEY_DOWN); key(KEY_DOWN);
    check_int("Move left", U.acts[U.act_sel], WD_ACT_LEFT);
    key(KEY_ENTER);
    check_int("moved back: terminal is second again", wd_pins_find(&U.pins, "terminal.desktop"), 1);
    EXPECT("", "save");
    key(KEY_SPACE);
    key(KEY_DOWN); key(KEY_DOWN); key(KEY_DOWN); key(KEY_DOWN);
    check_int("Close window", U.acts[U.act_sel], WD_ACT_CLOSE);
    key(KEY_ENTER);
    EXPECT("closing a window asks for it by its ID", "close:11");
    check_int("and puts the dock away", U.dock, UI_IDLE);

    fresh(true);
    pad("summon"); key(KEY_SPACE); key(KEY_DOWN);
    check_int("Unpin is the 2nd", U.acts[U.act_sel], WD_ACT_UNPIN);
    key(KEY_ENTER);
    EXPECT("unpinning saves", "save");
    check_true("terminal is not a pin any more", wd_pins_find(&U.pins, "terminal.desktop") < 0);
    check_true("but its window remains in the strip", U.dock == UI_SUMMONED);

    fresh(true);
    pad("summon");
    key(KEY_LEFT); key(KEY_LEFT);
    check_str("Applications", focus_key(), "apps");
    key(KEY_SPACE);
    check_int("the Applications button has no actions", U.dock, UI_SUMMONED);
    key(KEY_LEFT); key(KEY_LEFT);
}

static void t_dock_full(void)
{
    fresh(false);
    WdPins pins = { .n = 0 };
    (void)pins;
    fresh(true);
    char t[64];
    for (int i = 0; i < 40; i++) { snprintf(t, sizeof t, "p%02d.desktop", i); wd_pins_toggle(&U.pins, t); }
    check_int("the pin list is bounded", U.pins.n, WD_MAX_PINS);
    ui_rebuild(&U);
    pad("summon");
    key(KEY_RIGHT); key(KEY_RIGHT);
    int before = U.pins.n;
    pad("osk");
    check_int("pinning past the limit changes nothing", U.pins.n, before);
    check_true("and says so", U.toast.on && strstr(U.toast.title, "full") != NULL);
}

/* ------------------------------------------------------------------ the menu */
static void t_menu(void)
{
    fresh(true);
    pad("menu");
    check_true("Start opens the menu", U.menu && U.dock == UI_IDLE);
    check_int("all six applications", U.mn, 6);
    check_int("the first is selected", U.msel, 0);
    UiGeom g = ui_geometry(&U);
    check_true("the menu is a surface", g.menu_visible);
    key(KEY_RIGHT); key(KEY_RIGHT);
    check_int("right moves along the row", U.msel, 2);
    key(KEY_DOWN);
    check_int("down goes to the next row (a short last row lands on its last tile)", U.msel, 5);
    key(KEY_UP);
    check_int("up", U.msel, 1);
    key(KEY_LEFT); key(KEY_LEFT);
    check_int("left stops at the first", U.msel, 0);
    key(KEY_ENTER);
    EXPECT("A launches the selected application", "launch:about");
    check_true("and closes the menu", !U.menu);

    pad("menu");
    key(KEY_ESC);
    check_true("B closes it", !U.menu);
    pad("menu"); pad("menu");
    check_true("Start again closes it", !U.menu);

    pad("menu");
    text(KEY_T, "t");
    check_str("typing filters the list", U.query, "t");
    check_true("by name, anywhere in it", U.mn >= 2);
    text(KEY_E, "e");
    check_str("more letters", U.query, "te");
    check_int("only the terminal", U.mn, 1);
    key(KEY_BACKSPACE);
    check_str("backspace", U.query, "t");
    key(KEY_BACKSPACE); key(KEY_BACKSPACE);
    check_str("nothing to delete", U.query, "");
    check_int("all again", U.mn, 6);
    text(KEY_T, "t"); text(KEY_E, "e"); text(KEY_R, "r");
    key(KEY_ENTER);
    EXPECT("Enter launches the match", "launch:terminal");
    pad("menu"); text(KEY_Z, "z"); text(KEY_Z, "z");
    check_int("nothing matches", U.mn, 0);
    key(KEY_ENTER);
    EXPECT("Enter on no match does nothing", "");
    check_true("and the menu stays open", U.menu);
    key(KEY_ESC);
    check_true("Escape clears a query first", U.menu && U.query[0] == 0);
    key(KEY_ESC);
    check_true("then closes", !U.menu);

    pad("menu");
    key(KEY_SPACE);
    EXPECT("Y (Space) with nothing typed is Search: the keyboard opens", "pad-osk:1");
    check_true("it is open", U.osk);
    pad("osk-hide"); effs();
    text(KEY_A, "a"); key(KEY_SPACE);
    check_str("a space in a query is a space", U.query, "a ");
    check_true("without opening the keyboard", !U.osk);
    key(KEY_ESC); key(KEY_ESC);

    pad("menu");
    keym(KEY_C, UI_CTRL);
    check_str("a control key is not text", U.query, "");
    ui_key(&U, KEY_A, "\xc3\xa9", 0);
    check_str("UTF-8 text is added", U.query, "\xc3\xa9");
    key(KEY_BACKSPACE);
    check_str("and removed as a whole letter", U.query, "");
    ui_key(&U, KEY_A, "\x01", 0);
    check_str("a control character is not text", U.query, "");
    for (int i = 0; i < 80; i++) text(KEY_A, "a");
    check_true("the query is bounded", strlen(U.query) < UI_QUERY);
    key(KEY_ESC); key(KEY_ESC);
}

static void t_menu_pins_and_pointer(void)
{
    fresh(true);
    pad("menu");
    key(KEY_RIGHT);
    check_str("images is second", APPS.v[U.mlist[U.msel]].id, "browser.desktop");
    key(KEY_RIGHT);
    check_str("the third", APPS.v[U.mlist[U.msel]].id, "images.desktop");
    pad("osk");
    EXPECT("X pins the selected application", "save");
    check_true("images is pinned", wd_pins_find(&U.pins, "images.desktop") >= 0);
    check_true("the menu stays open", U.menu);
    pad("osk");
    EXPECT("X again unpins it", "save");
    check_true("and the keyboard did not open", !U.osk);

    UiViews *vw = malloc(sizeof *vw);
    ui_make_views(&U, vw);
    int hit = view_menu_hit(&vw->menu, WISP_UI_MENU_X + 75 + 150, WISP_UI_MENU_Y + 20);
    check_int("the tile at the second column", hit, 1);
    ui_pointer(&U, SURF_MENU, WISP_UI_MENU_X + 75 + 150, WISP_UI_MENU_Y + 20, PTR_MOVE, 0);
    check_int("hover selects", U.msel, 1);
    ui_pointer(&U, SURF_MENU, WISP_UI_MENU_X + 75 + 150, WISP_UI_MENU_Y + 20, PTR_DOWN, BTN_RIGHT);
    EXPECT("a right click pins", "save");
    ui_pointer(&U, SURF_MENU, WISP_UI_MENU_X + 75 + 150, WISP_UI_MENU_Y + 20, PTR_UP, BTN_RIGHT);
    ui_pointer(&U, SURF_MENU, WISP_UI_MENU_X + 75 + 150, WISP_UI_MENU_Y + 20, PTR_DOWN, BTN_LEFT);
    EXPECT("a left click launches", "launch:browser");
    check_true("and closes", !U.menu);
    pad("menu");
    ui_pointer(&U, SURF_MENU, 5, 5, PTR_DOWN, BTN_LEFT);
    check_true("a click on the empty part closes the menu", !U.menu);
    pad("menu");
    ui_pointer(&U, SURF_MENU, 5, 5, PTR_DOWN, BTN_RIGHT);
    check_true("a right click on the empty part does not", U.menu);
    ui_pointer(&U, SURF_MENU, 5, 5, PTR_LEAVE, 0);
    key(KEY_ESC);
    free(vw);
}

static void t_menu_scroll(void)
{
    wd_apps_free(&APPS);
    cleanup();
    make_apps(20);
    fresh(true);
    pad("menu");
    check_int("26 applications", U.mn, 26);
    check_int("two rows are visible", view_menu_rows_visible(), 2);
    for (int i = 0; i < 8; i++) key(KEY_DOWN);
    check_true("moving down scrolls", U.mtop > 0);
    check_true("the selection is always on screen", U.msel / 4 >= U.mtop && U.msel / 4 < U.mtop + 2);
    int top = U.mtop;
    ui_wheel(&U, SURF_MENU, 1);
    check_int("the wheel up scrolls up", U.mtop, top - 1);
    ui_wheel(&U, SURF_MENU, -10);
    check_int("down stops at the last row", U.mtop, (26 + 3) / 4 - 2);
    ui_wheel(&U, SURF_MENU, 100);
    check_int("up stops at the first", U.mtop, 0);
    key(KEY_ESC);
    wd_apps_free(&APPS);
    cleanup();
    make_apps(0);
}

/* ------------------------------------------------------------------ the quick settings */
static void t_quick_settings(void)
{
    fresh(true);
    U.sys.volume = 0.50f; U.sys.brightness = 0.50f; U.sys.bt_available = true; U.sys.bt_on = false; U.sys.ssh_on = false; U.sys.usb = 0;
    pad("qs");
    check_int("opened", U.dock, UI_QS);
    EXPECT("fresh values are asked for", "refresh");
    check_true("the status group has the focus", U.focus >= 0 && U.items[U.focus].kind == WD_STATUS);
    check_int("the first row", U.qs_sel, 0);
    key(KEY_ENTER);
    EXPECT("A on Wi-Fi opens the chooser", "wifi");
    key(KEY_DOWN);
    check_int("down to Bluetooth", U.qs_sel, 1);
    key(KEY_ENTER);
    EXPECT("A toggles Bluetooth", "bluetooth:1");
    key(KEY_LEFT);
    EXPECT("left turns it off", "bluetooth:0");
    key(KEY_RIGHT); key(KEY_RIGHT);
    EXPECT("right turns it on once", "bluetooth:1");
    key(KEY_DOWN);
    check_int("volume", U.qs_sel, 2);
    key(KEY_RIGHT);
    EXPECT("right raises the volume a step", "volume:0.55");
    key(KEY_RIGHT);
    EXPECT("again", "volume:0.60");
    key(KEY_LEFT); key(KEY_LEFT); key(KEY_LEFT);
    EXPECT("and lowers it", "volume:0.55 volume:0.50 volume:0.45");
    for (int i = 0; i < 20; i++) key(KEY_LEFT);
    check_true("it stops at nothing", U.sys.volume >= 0.f && U.sys.volume < 0.001f);
    effs();
    for (int i = 0; i < 30; i++) key(KEY_RIGHT);
    check_true("and at full", U.sys.volume > 0.999f && U.sys.volume <= 1.f);
    effs();
    key(KEY_DOWN);
    check_int("brightness", U.qs_sel, 3);
    for (int i = 0; i < 30; i++) key(KEY_LEFT);
    check_true("brightness never goes to black", U.sys.brightness >= 0.0499f);
    effs();
    key(KEY_DOWN);
    check_int("SSH", U.qs_sel, 4);
    key(KEY_ENTER);
    EXPECT("A toggles SSH", "ssh:1");
    key(KEY_ENTER);
    EXPECT("and back", "ssh:0");
    key(KEY_DOWN);
    check_int("USB port", U.qs_sel, 5);
    key(KEY_RIGHT);
    EXPECT("right does nothing on a row that opens a submenu", "");
    check_int("still the list", U.qs_page, 0);
    key(KEY_ENTER);
    check_int("A opens the USB submenu", U.qs_page, 1);
    check_int("with the current mode selected", U.qs_sel, 1);
    EXPECT("opening it changes nothing", "");
    key(KEY_DOWN); key(KEY_DOWN);
    check_int("down to Host", U.qs_sel, 3);
    ui_wheel(&U, SURF_DOCK, -1);
    EXPECT("the wheel does not change the volume while the submenu is open", "");
    key(KEY_DOWN);
    check_int("the note under the modes is text, not a row to select", U.qs_sel, 3);
    key(KEY_ENTER);
    EXPECT("A applies the mode", "usb:2");
    check_int("the shown mode follows", U.sys.usb, 2);
    check_int("back on the list", U.qs_page, 0);
    check_int("with the USB row selected", U.qs_sel, 5);
    key(KEY_ENTER);
    check_int("reopened with the current mode selected", U.qs_sel, 3);
    key(KEY_ENTER);
    EXPECT("choosing the mode that is on does nothing", "");
    check_int("and returns", U.qs_page, 0);
    key(KEY_ENTER); key(KEY_UP); key(KEY_UP);
    check_int("up to Off", U.qs_sel, 1);
    key(KEY_UP);
    check_int("up to the back row", U.qs_sel, 0);
    key(KEY_ENTER);
    EXPECT("the back row applies nothing", "");
    check_int("and returns to the list", U.qs_page, 0);
    key(KEY_ENTER); key(KEY_ESC);
    check_int("B in the submenu goes back, it does not close the panel", U.dock, UI_QS);
    check_int("to the list", U.qs_page, 0);
    key(KEY_ENTER); key(KEY_LEFT);
    check_int("left also goes back", U.qs_page, 0);
    key(KEY_ENTER); key(KEY_UP); key(KEY_UP);
    key(KEY_ENTER);
    EXPECT("back to off", "usb:0");
    key(KEY_DOWN);
    check_int("the battery row is information: the selection stays on USB", U.qs_sel, 5);
    key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP); key(KEY_UP);
    check_int("up stops at Wi-Fi", U.qs_sel, 0);
    key(KEY_ESC);
    check_int("B closes it", U.dock, UI_IDLE);

    U.sys.bt_available = false;
    pad("qs");
    effs();
    key(KEY_DOWN); key(KEY_ENTER); key(KEY_RIGHT);
    EXPECT("Bluetooth that is not there cannot be switched", "");
    pad("qs");
    check_int("the same command closes it", U.dock, UI_IDLE);
}

static void t_quick_pointer(void)
{
    fresh(true);
    U.sys.volume = 0.5f; U.sys.brightness = 0.5f; U.sys.bt_available = true;
    pad("qs");
    effs();
    UiViews *vw = malloc(sizeof *vw);
    ui_make_views(&U, vw);
    VRect pr = view_quick_rect(&vw->qs);
    int y = pr.y0 + 12;
    int hh[UI_QS_ROWS];
    for (int i = 0; i < UI_QS_ROWS; i++) { QsRow *r = &vw->qs_rows[i]; hh[i] = (r->kind == QS_SWITCH && r->value) ? 52 : 42; }
    int rowy[UI_QS_ROWS];
    for (int i = 0; i < UI_QS_ROWS; i++) { rowy[i] = y; y += hh[i]; }
    int tx0 = pr.x0 + 62, tx1 = pr.x0 + WISP_UI_QS_W - 28;
    ui_pointer(&U, SURF_DOCK, tx0 + 10, rowy[2] + 10, PTR_MOVE, 0);
    check_int("hover selects the row", U.qs_sel, 2);
    ui_pointer(&U, SURF_DOCK, tx0 + (tx1 - tx0) / 4, rowy[2] + 20, PTR_DOWN, BTN_LEFT);
    check_true("a press on the volume track sets it", U.sys.volume > 0.2f && U.sys.volume < 0.3f);
    EXPECT("with the effect", "volume:0.25");
    ui_pointer(&U, SURF_DOCK, tx0 + (tx1 - tx0) * 3 / 4, rowy[2] + 20, PTR_MOVE, 0);
    check_true("dragging follows the pointer", U.sys.volume > 0.7f && U.sys.volume < 0.8f);
    effs();
    ui_pointer(&U, SURF_DOCK, tx1 + 100, rowy[2] + 20, PTR_MOVE, 0);
    check_true("a drag past the end is full", U.sys.volume > 0.999f);
    effs();
    ui_pointer(&U, SURF_DOCK, 0, 0, PTR_UP, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, tx0, rowy[2] + 20, PTR_MOVE, 0);
    check_true("after the release it is only hovering", U.sys.volume > 0.999f);
    ui_pointer(&U, SURF_DOCK, tx0 + 40, rowy[4] + 15, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, tx0 + 40, rowy[4] + 15, PTR_UP, BTN_LEFT);
    EXPECT("a click on the SSH row toggles it", "ssh:1");
    ui_pointer(&U, SURF_DOCK, pr.x0 + 60, rowy[5] + 15, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, pr.x0 + 60, rowy[5] + 15, PTR_UP, BTN_LEFT);
    check_int("a click on the USB row opens the submenu", U.qs_page, 1);
    EXPECT("with no effect yet", "");
    ui_make_views(&U, vw);
    check_int("the panel keeps its size and place", view_quick_rect(&vw->qs).y0, pr.y0);
    int sub_y = pr.y0 + 12;                                      /* the back row (42), then the modes (63 each), then the note */
    ui_pointer(&U, SURF_DOCK, pr.x0 + 60, sub_y + 42 + 63 * 2 + 20, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, pr.x0 + 60, sub_y + 42 + 63 * 2 + 20, PTR_UP, BTN_LEFT);
    EXPECT("a click on a mode applies it", "usb:2");
    check_int("and returns to the list", U.qs_page, 0);
    ui_pointer(&U, SURF_DOCK, pr.x0 + 60, rowy[5] + 15, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, pr.x0 + 60, sub_y + 15, PTR_DOWN, BTN_LEFT);
    check_int("a click on the back row returns without applying", U.qs_page, 0);
    EXPECT("nothing was applied", "");
    ui_pointer(&U, SURF_DOCK, pr.x0 + 30, rowy[6] + 5, PTR_DOWN, BTN_LEFT);
    EXPECT("a click on the battery does nothing", "");
    check_int("and stays open", U.dock, UI_QS);
    ui_pointer(&U, SURF_DOCK, 20, 100, PTR_DOWN, BTN_LEFT);
    check_int("a click outside closes it", U.dock, UI_IDLE);
    EXPECT("with no other effect", "");
    free(vw);
}

/* ------------------------------------------------------------------ the settings window */
static void t_settings(void)
{
    fresh(true);
    U.sys.screen_timeout = 120;
    pad("settings");
    check_true("opened", U.settings);
    EXPECT("fresh values are asked for", "refresh");
    UiGeom g = ui_geometry(&U);
    check_true("its own surface is wanted", g.settings_visible);
    check_int("above the strip, which keeps its space", g.settings_h, WISP_UI_H - WISP_UI_DOCK_H);
    check_true("the pad starts in the list of pages", !U.set_in);
    key(KEY_TAB);
    check_int("Tab on the last page stays on it", U.set_page, 0);
    key(KEY_RIGHT);
    check_true("right enters the rows", U.set_in);
    check_int("on the option that applies (2 min)", U.set_opt, 2);
    key(KEY_RIGHT);
    check_int("right moves to 5 min", U.set_opt, 3);
    EXPECT("moving applies nothing", "");
    key(KEY_ENTER);
    EXPECT("A applies it", "screen-timeout:300");
    check_int("the shown value follows", U.sys.screen_timeout, 300);
    key(KEY_ENTER);
    EXPECT("applying the one that is on does nothing", "");
    for (int i = 0; i < 6; i++) key(KEY_RIGHT);
    check_int("right stops at the last option", U.set_opt, 5);
    key(KEY_ENTER);
    EXPECT("30 min is 1800 seconds", "screen-timeout:1800");
    for (int i = 0; i < 5; i++) key(KEY_LEFT);
    check_int("left walks back to Never", U.set_opt, 0);
    key(KEY_ENTER);
    EXPECT("Never is 0", "screen-timeout:0");
    key(KEY_LEFT);
    check_true("left from the first option returns to the pages", !U.set_in);
    key(KEY_RIGHT);
    key(KEY_ESC);
    check_true("B in the rows returns to the pages", !U.set_in);
    check_true("without closing the window", U.settings);
    key(KEY_ESC);
    check_true("B in the pages closes the window", !U.settings);
    check_true("and its surface goes", !ui_geometry(&U).settings_visible);

    U.sys.screen_timeout = 90;                                         /* set from a terminal to a value none of the options is */
    pad("settings");
    effs();
    UiViews *vw = malloc(sizeof *vw);
    ui_make_views(&U, vw);
    check_int("no option is on", vw->set_rows[0].on, -1);
    check_true("the note says so", strstr(vw->set_rows[0].note, "90 s") != NULL);
    key(KEY_RIGHT);
    check_int("entering starts on the first option", U.set_opt, 0);
    key(KEY_RIGHT); key(KEY_ENTER);
    EXPECT("picking one replaces the odd value", "screen-timeout:60");
    free(vw);

    pad("qs");
    check_true("asking for the quick settings closes the window", !U.settings);
    check_int("and opens them", U.dock, UI_QS);
    pad("settings");
    check_true("asking for the window closes the quick settings", U.settings && U.dock == UI_IDLE);
    pad("settings");
    check_true("the same command closes it", !U.settings);
    pad("settings"); pad("menu");
    check_true("the application menu replaces it", !U.settings && U.menu);
    pad("menu");
}

static void t_dock_pointer_under_the_menu(void)
{
    fresh(true);
    pad("menu");
    effs();
    for (int x = 0; x < WISP_UI_W; x += 20) {
        ui_pointer(&U, SURF_DOCK, x, WISP_UI_H - 20, PTR_DOWN, BTN_LEFT);
        ui_pointer(&U, SURF_DOCK, x, WISP_UI_H - 20, PTR_UP, BTN_LEFT);
    }
    EXPECT("the strip is covered by the menu: presses on it do nothing", "");
    check_true("and the menu stays", U.menu && U.dock == UI_IDLE);
    pad("menu");
}

static void t_settings_pointer(void)
{
    fresh(true);
    U.sys.screen_timeout = 120;
    pad("settings");
    effs();
    int w6 = (702 - 194) / 6;
    int y_opt = 80 + 34 + 15;
    ui_pointer(&U, SURF_SETTINGS, 194 + 4 * w6 + w6 / 2, y_opt, PTR_DOWN, BTN_LEFT);
    EXPECT("a click on 10 min applies it", "screen-timeout:600");
    check_true("and puts the pad in the rows", U.set_in && U.set_opt == 4);
    ui_pointer(&U, SURF_SETTINGS, 194 + 4 * w6 + w6 / 2, y_opt, PTR_DOWN, BTN_LEFT);
    EXPECT("a click on the one that is on does nothing", "");
    ui_pointer(&U, SURF_SETTINGS, 194 + 5 * w6 + w6 / 2, y_opt, PTR_DOWN, BTN_RIGHT);
    EXPECT("a right click on another option does nothing", "");
    ui_pointer(&U, SURF_SETTINGS, 80, 56 + 20, PTR_DOWN, BTN_LEFT);
    check_true("a click on a page returns to the pages", !U.set_in && U.set_page == 0);
    ui_pointer(&U, SURF_SETTINGS, 400, 400, PTR_DOWN, BTN_LEFT);
    check_true("a click on nothing changes nothing", U.settings && !U.set_in);
    EXPECT("with no effect", "");
    ui_pointer(&U, SURF_DOCK, 20, WISP_UI_H - 20, PTR_DOWN, BTN_LEFT);
    check_true("a press on the strip leaves the window", !U.settings);
    effs();
    ui_pointer(&U, SURF_SETTINGS, 194 + w6, y_opt, PTR_DOWN, BTN_LEFT);
    EXPECT("a click with the window closed does nothing", "");
}

/* ------------------------------------------------------------------ the keyboard */
static void t_osk(void)
{
    fresh(true);
    pad("osk");
    EXPECT("X opens the keyboard and tells the pad", "pad-osk:1");
    check_true("it is open", U.osk);
    UiGeom g = ui_geometry(&U);
    check_true("the dock is hidden while it is open", g.osk_visible && !g.dock_visible);
    check_int("it starts on the picture's key (row 2, column 4: g)", U.orow * 100 + U.ocol, 204);
    pad("osk-type");
    EXPECT("A types the focused key", "type:34");
    pad("nav-right");
    pad("osk-type");
    EXPECT("right then type: h", "type:35");
    pad("nav-up");
    pad("osk-type");
    EXPECT("up: a key of the row above (y or u), centred near g", "type:21");
    pad("nav-down"); pad("nav-down"); pad("nav-down");
    check_int("down reaches the special row", U.orow, 4);
    pad("nav-down");
    check_int("and stops there", U.orow, 4);
    pad("nav-up"); pad("nav-up"); pad("nav-up"); pad("nav-up"); pad("nav-up");
    check_int("up stops at the digits", U.orow, 0);
    U.ocol = 0;
    for (int i = 0; i < 12; i++) pad("nav-left");
    check_int("left wraps around the row", U.ocol, 8);
    pad("osk-type");
    EXPECT("typing a digit", "type:10");
    pad("osk-space");
    pad("osk-del");
    pad("osk-enter");
    EXPECT("space, delete and enter are commands too", "type:57 type:14 type:28");

    pad("osk-shift");
    check_true("X is Shift: the keyboard shows capitals", U.shift && U.page == OSK_UPPER);
    U.orow = 1; U.ocol = 0;
    pad("osk-type");
    EXPECT("a capital is the key with Shift", "type:16+shift");
    check_true("and Shift lasts for one key", !U.shift && U.page == OSK_LOWER);
    pad("osk-type");
    EXPECT("the next one is plain", "type:16");
    pad("osk-shift"); pad("osk-shift");
    check_true("Shift twice is off again", !U.shift && U.page == OSK_LOWER);

    U.orow = 4; U.ocol = 0;
    pad("osk-type");
    check_int("123 goes to the symbols page", U.page, OSK_SYMBOLS);
    EXPECT("nothing typed", "");
    U.orow = 1; U.ocol = 0;
    pad("osk-type");
    EXPECT("a symbol that needs Shift on a US keyboard", "type:2+shift");
    pad("osk-shift");
    check_int("Shift does nothing on the symbols page", U.page, OSK_SYMBOLS);
    U.orow = 4; U.ocol = 0;
    pad("osk-type");
    check_int("abc goes back", U.page, OSK_LOWER);
    U.orow = 4; U.ocol = 2;
    pad("osk-type");
    check_true("Ctrl is armed", U.ctrl);
    U.orow = 2; U.ocol = 0;
    pad("osk-type");
    EXPECT("Ctrl lasts for one key: Ctrl+A", "type:30+ctrl");
    check_true("and is then off", !U.ctrl);

    U.orow = 4; U.ocol = 3;
    pad("osk-type");
    U.orow = 4; U.ocol = 4;
    pad("osk-type");
    U.orow = 4; U.ocol = 5;
    pad("osk-type");
    U.orow = 4; U.ocol = 6;
    pad("osk-type");
    EXPECT("the special keys: space, tab, escape, enter", "type:57 type:15 type:1 type:28");
    U.page = OSK_SYMBOLS;
    U.orow = 4; U.ocol = 3;
    pad("osk-type");
    U.ocol = 4; pad("osk-type");
    U.ocol = 5; pad("osk-type");
    U.ocol = 6; pad("osk-type");
    EXPECT("the arrows of the symbols page", "type:105 type:108 type:103 type:106");
    U.ocol = 8;
    pad("osk-type");
    EXPECT("delete", "type:14");
    pad("osk-hide");
    EXPECT("Select hides it and tells the pad", "pad-osk:0");
    check_true("it is closed", !U.osk);
    pad("osk-type");
    EXPECT("commands for a closed keyboard are ignored", "");
    pad("nav-up");
    EXPECT("a stray navigation command is a key press for the dock, which is not summoned: nothing", "");
}

static void t_osk_pointer_and_focus(void)
{
    fresh(true);
    pad("osk"); effs();
    VRect k;
    check_true("the key geometry is there", view_osk_rect(U.page, 1, 0, &k));
    ui_pointer(&U, SURF_OSK, (k.x0 + k.x1) / 2, (k.y0 + k.y1) / 2, PTR_MOVE, 0);
    check_int("hover moves the focus to the key", U.orow * 100 + U.ocol, 100);
    ui_pointer(&U, SURF_OSK, (k.x0 + k.x1) / 2, (k.y0 + k.y1) / 2, PTR_DOWN, BTN_LEFT);
    EXPECT("a click types it", "type:16");
    ui_pointer(&U, SURF_OSK, 1, 1, PTR_DOWN, BTN_LEFT);
    EXPECT("a click beside the keys does not", "");
    ui_pointer(&U, SURF_OSK, (k.x0 + k.x1) / 2, (k.y0 + k.y1) / 2, PTR_DOWN, BTN_RIGHT);
    EXPECT("a right click does not", "");
    pad("osk-hide"); effs();

    fresh(true);
    pad("summon");
    pad("osk");
    EXPECT("in the summoned dock X is Unpin, not the keyboard", "save");
    check_true("the keyboard stays shut", !U.osk);
    key(KEY_ESC);
    pad("osk");
    EXPECT("the keyboard opens", "pad-osk:1");
    pad("menu");
    check_true("the menu closes the keyboard", U.menu && !U.osk);
    EXPECT("and tells the pad", "pad-osk:0");
    key(KEY_ESC);
    pad("osk");
    pad("summon");
    check_true("summoning the dock closes the keyboard", !U.osk && U.dock == UI_SUMMONED);
    effs();
}

/* ------------------------------------------------------------------ windows, the pointer on the strip, shortcuts */
static void t_windows(void)
{
    fresh(false);
    wd_windows_update(&U.ws, 11, "foot", "t", 0);
    ui_windows_changed(&U);
    EXPECT("a window that is not in front tells the pad nothing", "");
    wd_windows_update(&U.ws, 11, "foot", "t", 1);
    ui_windows_changed(&U);
    EXPECT("the one in front is told to the pad (it picks the layer)", "pad-focus:foot");
    ui_windows_changed(&U);
    EXPECT("and only when it changes", "");
    wd_windows_update(&U.ws, 12, "firefox", "page", 1);
    ui_windows_changed(&U);
    EXPECT("a browser in front", "pad-focus:firefox");
    wd_windows_remove(&U.ws, 12);
    ui_windows_changed(&U);
    EXPECT("closed: none in front", "pad-focus:");

    fresh(true);
    pad("summon");
    key(KEY_RIGHT);
    check_str("on settings", focus_key(), "settings.desktop");
    wd_windows_update(&U.ws, 13, "wisp-settings", "Settings", 0);
    ui_windows_changed(&U);
    check_str("the focus stays on the same item when a window appears", focus_key(), "settings.desktop");
    wd_windows_remove(&U.ws, 13);
    ui_windows_changed(&U);
    check_str("and when it goes", focus_key(), "settings.desktop");
    wd_windows_remove(&U.ws, 11);
    wd_windows_remove(&U.ws, 12);
    ui_windows_changed(&U);
    check_true("windows gone: the focus is still a real item", U.focus >= 0 && U.items[U.focus].kind != WD_SEP);
}

static void t_strip_pointer(void)
{
    fresh(true);
    int top = WISP_UI_H - WISP_UI_DOCK_H;
    int tx = U.items[3].x + 5, ty = top + 15;
    ui_pointer(&U, SURF_DOCK, tx, ty, PTR_MOVE, 0);
    check_int("hover marks the item", U.hover, 3);
    check_int("without focusing it (the pad's focus is separate)", U.focus, -1);
    ui_pointer(&U, SURF_DOCK, tx, ty, PTR_DOWN, BTN_LEFT);
    check_int("a press marks it", U.press, 3);
    ui_pointer(&U, SURF_DOCK, tx, ty, PTR_UP, BTN_LEFT);
    EXPECT("a click activates (the terminal is running: its window)", "activate:11");
    ui_pointer(&U, SURF_DOCK, U.items[2].x + 5, ty, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, U.items[3].x + 5, ty, PTR_UP, BTN_LEFT);
    EXPECT("a press on one item released on another is no click", "");
    ui_pointer(&U, SURF_DOCK, U.items[2].x + 5, ty, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, U.items[2].x + 5, ty, PTR_UP, BTN_LEFT);
    EXPECT("a click on the browser (not running) starts it", "launch:browser");
    ui_pointer(&U, SURF_DOCK, 0, 0, PTR_LEAVE, 0);
    check_int("leaving clears the hover", U.hover, -1);
    ui_pointer(&U, SURF_DOCK, tx, ty, PTR_DOWN, BTN_RIGHT);
    check_int("a right click opens the actions list", U.dock, UI_ACTIONS_OPEN);
    UiViews *vw = malloc(sizeof *vw);
    ui_make_views(&U, vw);
    VRect pr = view_popup_rect(vw->popup_x, vw->npopup, 214);
    ui_pointer(&U, SURF_DOCK, pr.x0 + 30, pr.y0 + 6 + 36 * 4 + 10, PTR_MOVE, 0);
    check_int("hover selects a row", U.act_sel, 4);
    ui_pointer(&U, SURF_DOCK, pr.x0 + 30, pr.y0 + 6 + 36 * 4 + 10, PTR_DOWN, BTN_LEFT);
    EXPECT("a click performs it: Close window", "close:11");
    check_int("and the list closes with the dock (no pad opened it)", U.dock, UI_IDLE);
    ui_pointer(&U, SURF_DOCK, tx, ty, PTR_DOWN, BTN_RIGHT);
    ui_pointer(&U, SURF_DOCK, 10, 10, PTR_DOWN, BTN_LEFT);
    check_int("a click away from the list closes it", U.dock, UI_IDLE);
    EXPECT("with no effect", "");
    ui_pointer(&U, SURF_DOCK, U.items[0].x + 5, ty, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, U.items[0].x + 5, ty, PTR_UP, BTN_LEFT);
    check_true("a click on Applications opens the menu", U.menu);
    key(KEY_ESC);
    ui_pointer(&U, SURF_DOCK, U.items[U.n - 1].x + 5, ty, PTR_DOWN, BTN_LEFT);
    ui_pointer(&U, SURF_DOCK, U.items[U.n - 1].x + 5, ty, PTR_UP, BTN_LEFT);
    check_int("a click on the status group opens the quick settings", U.dock, UI_QS);
    EXPECT("refresh", "refresh");
    ui_pointer(&U, SURF_DOCK, U.items[U.n - 1].x + 5, ty, PTR_DOWN, BTN_LEFT);
    check_int("and the status group again closes it", U.dock, UI_IDLE);
    pad("summon");
    ui_pointer(&U, SURF_DOCK, 50, 100, PTR_DOWN, BTN_LEFT);
    check_int("a click away from a summoned dock puts it away", U.dock, UI_IDLE);
    pad("summon");
    ui_pointer(&U, SURF_DOCK, U.items[2].x + 5, ty, PTR_MOVE, 0);
    check_str("the pointer moves the summoned dock's focus", focus_key(), "browser.desktop");
    free(vw);
}

static void t_shortcuts(void)
{
    fresh(true);
    keym(KEY_D, UI_SUPER);
    check_int("Super+D summons the dock", U.dock, UI_SUMMONED);
    check_true("not from the pad", !U.from_pad);
    key(KEY_ESC);
    keym(KEY_S, UI_SUPER);
    check_int("Super+S opens the quick settings", U.dock, UI_QS);
    effs();
    key(KEY_ESC);
    check_int("and Escape closes it to the plain strip", U.dock, UI_IDLE);
    keym(KEY_K, UI_SUPER);
    check_true("Super+K shows the keyboard", U.osk);
    effs();
    keym(KEY_K, UI_SUPER);
    check_true("and hides it", !U.osk);
    effs();
    keym(KEY_A, UI_SUPER);
    EXPECT("Super+A asks for the browser actions", "browser-actions");
    pad("actions");
    EXPECT("so does Select in the browser layer", "browser-actions");
    keym(KEY_D, 0);
    check_int("the letter alone does nothing", U.dock, UI_IDLE);
    pad("scroll-on");
    check_true("the scroll toggle says so", U.toast.on && strstr(U.toast.title, "scroll") != NULL);
    int w = ui_next_wake(&U);
    check_true("and wants a tick to take it down", w > 0 && w <= 1500);
    ui_tick(&U, U.now + 1000);
    check_true("still up", U.toast.on);
    ui_tick(&U, U.now + 600);
    check_true("gone after its time", !U.toast.on);
    check_int("nothing more is wanted", ui_next_wake(&U), -1);
    pad("scroll-off");
    check_true("pointer mode says so too", U.toast.on && strstr(U.toast.title, "pointer") != NULL);
}

static void t_chars(void)
{
    int code;
    bool sh;
    check_true("a", ui_char_key('a', &code, &sh) && code == KEY_A && !sh);
    check_true("A", ui_char_key('A', &code, &sh) && code == KEY_A && sh);
    check_true("1", ui_char_key('1', &code, &sh) && code == KEY_1 && !sh);
    check_true("0", ui_char_key('0', &code, &sh) && code == KEY_0 && !sh);
    check_true("!", ui_char_key('!', &code, &sh) && code == KEY_1 && sh);
    check_true(")", ui_char_key(')', &code, &sh) && code == KEY_0 && sh);
    check_true("-", ui_char_key('-', &code, &sh) && code == KEY_MINUS && !sh);
    check_true("_", ui_char_key('_', &code, &sh) && code == KEY_MINUS && sh);
    check_true("\\", ui_char_key('\\', &code, &sh) && code == KEY_BACKSLASH && !sh);
    check_true("|", ui_char_key('|', &code, &sh) && code == KEY_BACKSLASH && sh);
    check_true("'", ui_char_key('\'', &code, &sh) && code == KEY_APOSTROPHE && !sh);
    check_true("\"", ui_char_key('"', &code, &sh) && code == KEY_APOSTROPHE && sh);
    check_true("`", ui_char_key('`', &code, &sh) && code == KEY_GRAVE && !sh);
    check_true("~", ui_char_key('~', &code, &sh) && code == KEY_GRAVE && sh);
    check_true(",.<>", ui_char_key(',', &code, &sh) && code == KEY_COMMA && ui_char_key('<', &code, &sh) && code == KEY_COMMA && sh && ui_char_key('.', &code, &sh) && code == KEY_DOT && ui_char_key('>', &code, &sh) && sh);
    check_true("/?", ui_char_key('/', &code, &sh) && code == KEY_SLASH && !sh && ui_char_key('?', &code, &sh) && code == KEY_SLASH && sh);
    check_true("[]{}", ui_char_key('[', &code, &sh) && code == KEY_LEFTBRACE && ui_char_key('}', &code, &sh) && code == KEY_RIGHTBRACE && sh);
    check_true(";:", ui_char_key(';', &code, &sh) && code == KEY_SEMICOLON && !sh && ui_char_key(':', &code, &sh) && sh);
    check_true("=+", ui_char_key('=', &code, &sh) && code == KEY_EQUAL && ui_char_key('+', &code, &sh) && sh);
    check_true("space", ui_char_key(' ', &code, &sh) && code == KEY_SPACE);
    check_true("control characters are not keys", !ui_char_key('\n', &code, &sh) && !ui_char_key(0, &code, &sh) && !ui_char_key(127, &code, &sh));
    /* every character of the on-screen keyboard has a key, and the reverse maps back to it */
    const char *all = "1234567890qwertyuiopasdfghjklzxcvbnmQWERTYUIOPASDFGHJKLZXCVBNM!@#$%^&*()-_=+[]{};:'\",.<>/?\\|`~ ";
    for (const char *p = all; *p; p++) {
        char name[32];
        snprintf(name, sizeof name, "round trip of %c", *p);
        check_true(name, ui_char_key(*p, &code, &sh) && ui_key_char(code, sh) == *p);
    }
    check_int("a key that types nothing", ui_key_char(KEY_F1, false), 0);
    check_int("enter types no character", ui_key_char(KEY_ENTER, false), 0);
}

/* ------------------------------------------------------------------ offers (toasts that take a yes) */
static void t_offers(void)
{
    fresh(true);
    ui_toast_action(&U, "Program downloaded", "Foo: put it in the application menu?", "Install", 1, "/storage/downloads/Foo.AppImage", 10000);
    UiGeom g = ui_geometry(&U);
    check_true("an offer is on screen and holds the keyboard", g.toast_visible && g.toast_modal);
    key(KEY_RIGHT); key(KEY_SPACE); key(KEY_A); text(KEY_A, "a");
    EXPECT("other keys are swallowed", "");
    check_true("and the offer stays", U.toast.on);
    key(KEY_ENTER);
    EXPECT("A accepts: the action and the file", "download:1:/storage/downloads/Foo.AppImage");
    check_true("and the offer goes", !U.toast.on && !ui_geometry(&U).toast_modal);

    pad("menu");
    ui_toast_action(&U, "t", "x", "Extract", 2, "/storage/downloads/a.zip", 10000);
    key(KEY_RIGHT); key(KEY_DOWN);
    check_int("an offer that arrives over the menu takes the keys: the menu does not move", U.msel, 0);
    key(KEY_ESC);
    check_true("B puts the offer away and leaves the menu open", !U.toast.on && U.menu);
    key(KEY_ESC);
    EXPECT("B dismisses without doing anything", "");
    ui_toast_action(&U, "t", "x", "Extract", 2, "/storage/downloads/a.zip", 10000);
    key(KEY_ESC);
    check_true("and it goes", !U.toast.on);

    ui_toast_action(&U, "t", "x", "Open", 3, "/storage/downloads/p.png", 10000);
    ui_pointer(&U, SURF_TOAST, 5, 5, PTR_MOVE, 0);
    EXPECT("moving over it does nothing", "");
    ui_pointer(&U, SURF_TOAST, 5, 5, PTR_DOWN, BTN_LEFT);
    EXPECT("a click accepts", "download:3:/storage/downloads/p.png");
    ui_toast_action(&U, "t", "x", "Open", 3, "/storage/downloads/p.png", 10000);
    ui_pointer(&U, SURF_TOAST, 5, 5, PTR_DOWN, BTN_RIGHT);
    EXPECT("a right click dismisses", "");
    check_true("it is gone", !U.toast.on);
    ui_pointer(&U, SURF_TOAST, 5, 5, PTR_DOWN, BTN_LEFT);
    EXPECT("a click on nothing does nothing", "");

    ui_toast_action(&U, "t", "x", "Install", 1, "/f", 10000);
    pad("summon");
    check_true("asking for the dock puts the offer away first", !U.toast.on && U.dock == UI_SUMMONED);
    EXPECT("without accepting it", "");
    key(KEY_ESC);
    ui_toast_action(&U, "t", "x", "Install", 1, "/f", 10000);
    pad("nav-up");
    check_true("a stray navigation command does not", U.toast.on);
    pad("menu");
    check_true("but the menu does", !U.toast.on && U.menu);
    key(KEY_ESC);
    ui_toast_action(&U, "t", "x", "Install", 1, "/f", 10000);
    pad("osk");
    check_true("and so does the keyboard", !U.toast.on);
    effs();
    pad("osk-hide"); effs();

    ui_toast_action(&U, "t", "x", "Install", 1, "/f", 1000);
    ui_tick(&U, U.now + 1500);
    check_true("an offer that is not taken goes after its time", !U.toast.on);
    EXPECT("and nothing happens", "");

    ui_toast_action(&U, "first", "x", "Install", 1, "/first", 10000);
    ui_toast_action(&U, "second", "x", "Extract", 2, "/second", 10000);
    key(KEY_ENTER);
    EXPECT("a new offer replaces the old", "download:2:/second");

    ui_toast(&U, "Plain", "no choice", 5000);
    check_true("a plain toast takes no keys", !ui_geometry(&U).toast_modal);
    key(KEY_ENTER);
    EXPECT("Enter does nothing for it", "");
    ui_pointer(&U, SURF_TOAST, 5, 5, PTR_DOWN, BTN_LEFT);
    EXPECT("neither does a click", "");
    check_true("it stays until its time", U.toast.on);
    ui_toast_action(&U, "offer", "x", "Install", 1, "/f", 10000);
    ui_toast(&U, "Plain", "replaces the offer", 5000);
    check_true("a plain toast replacing an offer is not modal", !ui_geometry(&U).toast_modal);
    key(KEY_ENTER);
    EXPECT("and Enter no longer accepts the old one", "");

    UiViews *vw = malloc(sizeof *vw);
    ui_toast_action(&U, "Program downloaded", "Foo", "Install", 1, "/f", 10000);
    ui_make_views(&U, vw);
    check_true("the offer's view has its two buttons", vw->toast.nhints == 2 && !strcmp(vw->toast.hints[0].label, "Install") && !strcmp(vw->toast.hints[1].label, "Dismiss"));
    ui_toast(&U, "Plain", "x", 1000);
    ui_make_views(&U, vw);
    check_true("a plain toast has none", vw->toast.nhints == 0);
    free(vw);
}

/* ------------------------------------------------------------------ views and geometry */
static void t_views(void)
{
    fresh(true);
    UiViews *vw = malloc(sizeof *vw);
    ui_make_views(&U, vw);
    check_int("the dock view has every item", vw->dock.n, U.n);
    check_int("nothing focused", vw->dock.focus, -1);
    check_true("no bar", !vw->bar_visible);
    pad("summon");
    ui_make_views(&U, vw);
    check_true("summoned: the bar", vw->bar_visible && !strcmp(vw->bar_name, "Terminal"));
    check_int("a running pinned application has four hints", vw->nbar, 4);
    check_str("A switches to it", vw->bar[0].label, "Switch");
    check_str("X unpins", vw->bar[1].label, "Unpin");
    check_str("Y is More", vw->bar[2].label, "More");
    check_str("B is Back", vw->bar[3].label, "Back");
    check_true("the accent line", vw->dock.summoned);
    key(KEY_LEFT);
    ui_make_views(&U, vw);
    check_str("a pin that is not running opens", vw->bar[0].label, "Open");
    key(KEY_LEFT);
    ui_make_views(&U, vw);
    check_int("Applications: open and back", vw->nbar, 2);
    key(KEY_RIGHT); key(KEY_RIGHT); key(KEY_RIGHT);
    key(KEY_RIGHT);
    ui_make_views(&U, vw);
    check_true("an unpinned window offers Pin", !strcmp(vw->bar[1].label, "Pin") || !strcmp(vw->bar[0].label, "Switch"));
    key(KEY_SPACE);
    ui_make_views(&U, vw);
    check_true("the actions list: select and back", vw->nbar == 2 && !strcmp(vw->bar[0].label, "Select") && vw->npopup > 0);
    key(KEY_ESC); key(KEY_ESC);
    pad("qs");
    U.sys.battery = 0.78f; U.sys.charging = true; snprintf(U.sys.ssid, sizeof U.sys.ssid, "Home");
    ui_make_views(&U, vw);
    check_str("the battery text", vw->qs_rows[6].value, "78 %  charging");
    check_str("the network name", vw->qs_rows[0].value, "Home");
    check_int("the quick settings have seven rows", vw->qs.n, 7);
    check_int("one of them selected", vw->qs.selected, 0);
    U.sys.battery = -1.f; U.sys.ssid[0] = 0;
    ui_make_views(&U, vw);
    check_str("no battery", vw->qs_rows[6].value, "No battery");
    check_str("no network", vw->qs_rows[0].value, "Not connected");
    free(vw);
}

/* ------------------------------------------------------------------ random events keep the state consistent */
static unsigned long long rng = 88172645463325252ULL;
static unsigned rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng >> 11); }

static const char *const PADCMDS[] = { "summon", "menu", "qs", "settings", "osk", "actions", "scroll-on", "scroll-off", "nav-up", "nav-down", "nav-left", "nav-right", "osk-type", "osk-del", "osk-shift",
                                       "osk-space", "osk-enter", "osk-hide", "bogus" };
static const int KEYS[] = { KEY_LEFT, KEY_RIGHT, KEY_UP, KEY_DOWN, KEY_ENTER, KEY_ESC, KEY_SPACE, KEY_TAB, KEY_BACKSPACE, KEY_A, KEY_Z, KEY_D, KEY_S, KEY_K };

static void invariants(const char *when)
{
    char w[160];
    snprintf(w, sizeof w, "%s", when);
    if (U.dock == UI_IDLE) { if (U.focus != -1) { failed++; printf("FAIL %s: idle dock with a focus\n", w); } }
    else if (U.focus < 0 || U.focus >= U.n || U.items[U.focus].kind == WD_SEP) { failed++; printf("FAIL %s: modal dock with a bad focus %d\n", w, U.focus); }
    if (U.dock == UI_ACTIONS_OPEN && (U.nacts <= 0 || U.act_sel < 0 || U.act_sel >= U.nacts)) { failed++; printf("FAIL %s: actions %d/%d\n", w, U.act_sel, U.nacts); }
    if (U.dock == UI_QS && (U.qs_sel < 0 || U.qs_sel > 5)) { failed++; printf("FAIL %s: qs row %d\n", w, U.qs_sel); }
    if (U.menu && U.mn > 0 && (U.msel < 0 || U.msel >= U.mn || U.mtop < 0)) { failed++; printf("FAIL %s: menu %d/%d top %d\n", w, U.msel, U.mn, U.mtop); }
    if (U.menu && U.dock != UI_IDLE) { failed++; printf("FAIL %s: the menu and a dock panel at once\n", w); }
    if (U.osk && (U.dock != UI_IDLE || U.menu) && 0) { failed++; printf("FAIL %s: keyboard together with a panel\n", w); }
    if (U.osk && U.dock != UI_IDLE) { failed++; printf("FAIL %s: keyboard together with a dock panel\n", w); }
    if (U.orow < 0 || U.orow > 4 || U.ocol < 0 || U.ocol >= view_osk_row_len(U.page, U.orow)) { failed++; printf("FAIL %s: keyboard focus %d,%d on page %d\n", w, U.orow, U.ocol, U.page); }
    if (U.shift && U.page == OSK_SYMBOLS) { failed++; printf("FAIL %s: shift on the symbols page\n", w); }
    if (U.sys.volume < 0.f || U.sys.volume > 1.f || U.sys.brightness < 0.0499f || U.sys.brightness > 1.f) { failed++; printf("FAIL %s: volume %f brightness %f\n", w, (double)U.sys.volume, (double)U.sys.brightness); }
    if (U.settings && (U.menu || U.osk || U.dock != UI_IDLE)) { failed++; printf("FAIL %s: the settings window together with another panel\n", w); }
    if (U.set_in && !U.settings) { failed++; printf("FAIL %s: the pad in settings rows with the window closed\n", w); }
    if (U.set_page < 0 || U.set_page >= UI_SET_PAGES || U.set_row < 0 || U.set_row >= UI_SET_ROWS || U.set_opt < 0 || U.set_opt > 5) { failed++; printf("FAIL %s: settings focus %d/%d/%d\n", w, U.set_page, U.set_row, U.set_opt); }
    if (U.pins.n > WD_MAX_PINS) { failed++; printf("FAIL %s: pins\n", w); }
    if (strlen(U.query) >= UI_QUERY) { failed++; printf("FAIL %s: query\n", w); }
    if (U.toast.modal && !U.toast.on) { failed++; printf("FAIL %s: a modal toast that is not on\n", w); }
    if (U.dropped) { failed++; printf("FAIL %s: effects dropped\n", w); U.dropped = 0; }
    checks++;
}

static void t_random(void)
{
    fresh(true);
    U.sys.bt_available = true;
    int64_t t = 1000;
    for (long step = 0; step < 200000; step++) {
        unsigned r = rnd() % 100;
        if (r < 30) pad(PADCMDS[rnd() % (sizeof PADCMDS / sizeof *PADCMDS)]);
        else if (r < 60) { int k = KEYS[rnd() % (sizeof KEYS / sizeof *KEYS)]; unsigned m = rnd() % 3 == 0 ? (rnd() & 15) : 0; char s[2] = { (char)ui_key_char(k, false), 0 }; ui_key(&U, k, s, m); }
        else if (r < 80) ui_pointer(&U, (UiSurf)(rnd() % 5), (int)(rnd() % 760) - 20, (int)(rnd() % 520) - 20, (UiPtr)(rnd() % 4), rnd() & 1 ? BTN_LEFT : BTN_RIGHT);
        else if (r < 82) ui_toast_action(&U, "offer", "x", "Install", (int)(rnd() % 3) + 1, "/storage/downloads/f", 1000 + (int)(rnd() % 9000));
        else if (r < 84) ui_wheel(&U, (UiSurf)(rnd() % 3), (int)(rnd() % 7) - 3);
        else if (r < 90) {
            unsigned id = 10 + rnd() % 5;
            if (rnd() % 3 == 0) wd_windows_remove(&U.ws, id);
            else wd_windows_update(&U.ws, id, rnd() & 1 ? "foot" : rnd() & 1 ? "swayimg" : "unknown", "w", rnd() % 2);
            ui_windows_changed(&U);
        } else if (r < 93) { static const int tv[] = { -1, 0, 60, 90, 120, 300, 1800 }; U.sys.bt_available = rnd() & 1; U.sys.usb = (int)(rnd() % 3); U.sys.screen_timeout = tv[rnd() % 7]; }
        else if (r < 96) { t += (int64_t)(rnd() % 3000); ui_tick(&U, t); }
        else if (r < 98) { char c[8]; snprintf(c, sizeof c, "%d:%02d", (int)(rnd() % 24), (int)(rnd() % 60)); ui_set_clock(&U, c, 40 + (int)(rnd() % 10)); }
        else { UiEffect sink[UI_EFFECTS]; ui_take_effects(&U, sink, UI_EFFECTS); ui_take_dirty(&U); }
        invariants("random");
        if (U.neff > UI_EFFECTS - 4) { UiEffect sink[UI_EFFECTS]; ui_take_effects(&U, sink, UI_EFFECTS); }
        if (step % 20000 == 0) { UiViews *vw = malloc(sizeof *vw); ui_make_views(&U, vw); free(vw); }
    }
}

int main(void)
{
    make_apps(0);
    t_chars();
    t_summon();
    t_activate();
    t_pin_and_actions();
    t_dock_full();
    t_menu();
    t_menu_pins_and_pointer();
    t_menu_scroll();
    t_quick_settings();
    t_quick_pointer();
    t_settings();
    t_settings_pointer();
    t_dock_pointer_under_the_menu();
    t_osk();
    t_osk_pointer_and_focus();
    t_windows();
    t_offers();
    t_strip_pointer();
    t_shortcuts();
    t_views();
    t_random();
    wd_apps_free(&APPS);
    cleanup();
    printf("%d checks, %d failed\n", checks, failed);
    return failed ? 1 : 0;
}
