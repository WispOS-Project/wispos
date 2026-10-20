/* Draws a scene of the locked pictures (os/dock/design) with the real views onto a transparent 720 x 480 canvas and writes it as raw premultiplied ARGB, for os/test_dock_view.py to put
 * over the pictures' own backdrop and compare with them. Test only; it is not part of the image.
 *   test_scene SCENE FONT_DIR APPS_DIR OUT.raw
 * APPS_DIR holds the desktop files of the invented application list of the pictures (browser, terminal, settings, game, images, monitor, about). */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../ui/wisp-ui.h"
#include "model.h"
#include "view.h"

#define ACC RD_RGB(WISP_UI_ACCENT)
#define GRN RD_RGB(WISP_UI_OK)
#define GLY RD_RGB(WISP_UI_GLYPH)
#define WHT RD_RGB(WISP_UI_TEXT)

static IconSpec spec(const char *name)
{
    IconSpec s = { 0 };
    s.glyph = name;
    s.ink = !strcmp(name, "browser") || !strcmp(name, "game") ? ACC : !strcmp(name, "terminal") ? GRN : !strcmp(name, "apps") ? WHT : GLY;
    return s;
}

static const char *base_name(const char *id, char *buf, size_t cap)        /* "browser.desktop" -> "browser" */
{
    snprintf(buf, cap, "%s", id);
    char *dot = strrchr(buf, '.');
    if (dot) *dot = 0;
    return buf;
}

typedef struct { WdApps apps; WdPins pins; WdWindows ws; WdItem it[WD_MAX_ITEMS]; IconSpec ic[WD_MAX_ITEMS]; int n; } Dock;

static void dock_build(Dock *d, const ViewFonts *f, const char *apps_dir, bool running)
{
    memset(d, 0, sizeof *d);
    const char *dirs[] = { apps_dir };
    WdLoadOpts o = { NULL, NULL, NULL };
    wd_apps_load(&d->apps, dirs, 1, &o);
    wd_pins_parse("browser.desktop\nterminal.desktop\nsettings.desktop\n", &d->pins);
    if (running) {
        wd_windows_update(&d->ws, 11, "terminal", "wisp@WispOS: ~", 1);
        wd_windows_update(&d->ws, 12, "images", "photo.png", 0);
    }
    d->n = wd_dock_build(&d->apps, &d->pins, &d->ws, d->it, WD_MAX_ITEMS);
    wd_dock_layout(d->it, d->n, WISP_UI_W, rd_text_width(f->small, "12:34"));
    for (int i = 0; i < d->n; i++) {
        char b[160];
        const WdItem *it = &d->it[i];
        if (it->kind == WD_APPS) d->ic[i] = spec("apps");
        else if (it->kind == WD_GAME) d->ic[i] = spec("game");
        else if (it->kind == WD_PIN || it->kind == WD_WINDOW) {
            const char *id = it->app >= 0 ? d->apps.v[it->app].id : it->key;
            d->ic[i] = spec(base_name(id, b, sizeof b)[0] ? strdup(b) : "browser");
        }
    }
}

static int find_key(const Dock *d, const char *key)
{
    for (int i = 0; i < d->n; i++) if (!strcmp(d->it[i].key, key)) return i;
    return -1;
}

static void draw_dock(const Vctx *v, Dock *d, int focus, bool summoned)
{
    DockView dv = { d->it, d->ic, d->n, focus, -1, -1, summoned, "12:34", 3, 2, 0.78f };
    view_dock(v, &dv);
}

static const Hint H_FOCUS[] = { { "A", "Open" }, { "X", "Unpin" }, { "Y", "More" }, { "B", "Back" } };
static const Hint H_MORE[] = { { "A", "Select" }, { "B", "Back" } };
static const Hint H_MENU[] = { { "A", "Open" }, { "X", "Unpin" }, { "Y", "Search" }, { "B", "Back" } };
static const Hint H_QS[] = { { "A", "Change" }, { "B", "Close" } };

int main(int argc, char **argv)
{
    if (argc < 5) { fprintf(stderr, "usage: test_scene SCENE FONT_DIR APPS_DIR OUT.raw\n"); return 2; }
    const char *scene = argv[1];
    ViewFonts f;
    if (view_fonts_open(&f, argv[2]) != 0) return 1;
    uint32_t *px = calloc((size_t)WISP_UI_W * WISP_UI_H, 4);
    RdCanvas c = { WISP_UI_W, WISP_UI_H, WISP_UI_W, px };
    Vctx v = { &c, 0, 0, &f };
    Dock d;

    if (!strcmp(scene, "desktop-empty")) {
        dock_build(&d, &f, argv[3], false);
        draw_dock(&v, &d, -1, false);
    } else if (!strcmp(scene, "desktop-window")) {
        dock_build(&d, &f, argv[3], true);
        draw_dock(&v, &d, -1, false);
    } else if (!strcmp(scene, "dock-focus")) {
        dock_build(&d, &f, argv[3], true);
        draw_dock(&v, &d, find_key(&d, "settings.desktop"), true);
        view_focus_bar(&v, "Settings", H_FOCUS, 4);
    } else if (!strcmp(scene, "dock-more")) {
        dock_build(&d, &f, argv[3], true);
        int t = find_key(&d, "terminal.desktop");
        draw_dock(&v, &d, t, true);
        view_focus_bar(&v, "Terminal", H_MORE, 2);
        PopupRow rows[] = { { spec("terminal"), "Open" }, { spec("pin"), "Unpin from dock" }, { spec("left"), "Move left" }, { spec("right"), "Move right" }, { spec("close"), "Close window" } };
        view_popup(&v, d.it[t].x - 10, rows, 5, 1, 214);
    } else if (!strcmp(scene, "apps-menu")) {
        static const char *ids[] = { "browser", "terminal", "settings", "game", "images", "monitor", "about" };
        static const char *names[] = { "Browser", "Terminal", "Settings", "Game mode", "Images", "Monitor", "About" };
        MenuEntry e[7];
        for (int i = 0; i < 7; i++) e[i] = (MenuEntry){ spec(ids[i]), names[i], i < 3 };
        MenuView m = { e, 7, 1, 0, "Applications", "Pick an app. X pins it to the dock.", H_MENU, 4, NULL };
        view_menu(&v, &m);
    } else if (!strcmp(scene, "quick-settings") || !strcmp(scene, "quick-settings-submenu") || !strcmp(scene, "quick-settings-power-button") || !strcmp(scene, "quick-settings-power") || !strcmp(scene, "quick-settings-power-confirm")) {
        dock_build(&d, &f, argv[3], true);
        draw_dock(&v, &d, find_key(&d, "status"), false);
        QsRow rows[] = {
            { QS_LINK, "wifi", 3, "Wi-Fi", "Home-WiFi-5G", false, 0 },
            { QS_SWITCH, "bluetooth", 0, "Bluetooth", NULL, false, 0 },
            { QS_SLIDER, "volume", 2, "Volume", NULL, false, 0.62f },
            { QS_SLIDER, "brightness", 0, "Brightness", NULL, false, 0.40f },
            { QS_SWITCH, "ssh", 0, "SSH", "wisp@192.0.2.17", true, 0 },
            { QS_LINK, "usb", 0, "USB port", "Off", false, 0 },
            { QS_TEXT, "battery", 0, "Battery", "78 %  discharging", false, 0.78f },
        };
        QsView list = { rows, 7, 5, H_QS, 2, 0, 1 };
        if (!strcmp(scene, "quick-settings")) view_quick(&v, &list);
        else if (!strcmp(scene, "quick-settings-power-button")) { list.selected = QS_POWER_BTN; list.power_btn = 2; view_quick(&v, &list); }
        else if (!strcmp(scene, "quick-settings-power") || !strcmp(scene, "quick-settings-power-confirm")) {
            bool ask = !strcmp(scene, "quick-settings-power-confirm");
            QsRow sub[] = {
                { QS_BACK, NULL, 0, "Power", NULL, false, 0 },
                { QS_ACTION, "sleep", 0, "Sleep", "screen off, session kept", false, 0 },
                ask ? (QsRow){ QS_ASK, "restart", 0, "Restart", "Unsaved work is lost.", false, 0 } : (QsRow){ QS_ACTION, "restart", 0, "Restart", "close everything, start again", false, 0 },
                { QS_ACTION, "power", 0, "Shut down", "close everything, switch off", false, 0 },
            };
            Hint hints[] = { { "A", "Choose" }, { "B", "Back" } };
            QsView q = { sub, 4, ask ? 2 : 1, hints, 2, view_quick_height(&list), 0 };
            view_quick(&v, &q);
        } else {
            QsRow sub[] = {
                { QS_BACK, NULL, 0, "USB port", NULL, false, 0 },
                { QS_CHOICE, NULL, 0, "Off", "port unpowered, no data", true, 0 },
                { QS_CHOICE, NULL, 0, "Computer", "the handheld is a device", false, 0 },
                { QS_CHOICE, NULL, 0, "Host", "powers a connected device", false, 0 },
                { QS_NOTE, NULL, 0, "Off again at every boot.", NULL, false, 0 },
            };
            Hint hints[] = { { "A", "Choose" }, { "B", "Back" } };
            QsView q = { sub, 5, 1, hints, 2, view_quick_height(&list), 0 };
            view_quick(&v, &q);
        }
    } else if (!strcmp(scene, "settings-display")) {
        dock_build(&d, &f, argv[3], true);
        draw_dock(&v, &d, -1, false);
        static const SetPage pages[] = { { "Display", "brightness", 0 }, { "Date & time", "monitor", 0 }, { "Connections", "wifi", 3 }, { "About", "about", 0 } };
        static const SetRow rows[] = {
            { "Screen timeout", "Turn the screen off after no input", { "Never", "1 min", "2 min", "5 min", "10 min", "30 min" }, 6, 2, 4 },
            { "Desktop size", "Dock, menus and windows", { "100 %", "125 %", "150 %" }, 3, 0, -1 },
        };
        static const Hint hints[] = { { "A", "Change" }, { "B", "Back" }, { "L2/R2", "Page" } };
        SettingsView sv = { pages, 4, 0, rows, 2, 0, true, hints, 3 };
        view_settings(&v, &sv);
    } else if (!strcmp(scene, "osk") || !strcmp(scene, "osk-symbols")) {
        OskView o = { OSK_LOWER, false, false, 2, 4 };
        if (!strcmp(scene, "osk-symbols")) o = (OskView){ OSK_SYMBOLS, false, false, 3, 6 };
        view_osk(&v, &o);
        view_osk_hints(&v);
    } else {
        fprintf(stderr, "unknown scene %s\n", scene);
        return 2;
    }
    FILE *out = fopen(argv[4], "wb");
    if (!out) { perror(argv[4]); return 1; }
    uint32_t hdr[2] = { WISP_UI_W, WISP_UI_H };
    fwrite(hdr, 4, 2, out);
    fwrite(px, 4, (size_t)WISP_UI_W * WISP_UI_H, out);
    fclose(out);
    view_fonts_close(&f);
    free(px);
    return 0;
}
