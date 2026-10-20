/* The desktop shell's UI state machine: everything the shell decides, with no Wayland, no drawing and no I/O, so it is tested natively (os/dock/test_ui.c). Events go in (pad commands, keys,
 * the pointer, window changes, system values, time); state, geometry for the surfaces, views to draw and EFFECTS (launch this, type that, set the volume) come out. The Wayland program
 * (shell.c) only turns surfaces and input into these calls and carries the effects out.
 *
 * Input conventions (docs/WISPOS-OS-DESIGN.md "Controls and the on-screen keyboard in desktop mode"; the pad daemon, os/pad, presents the pad as a keyboard and a mouse):
 *   A is Enter, B is Escape, Y is Space and the D-pad and left stick are the arrow keys, so a summoned dock, the menu and the quick settings are driven by plain key events (the shell has the
 *   keyboard while it is modal); X, Start and Mode arrive as commands from the daemon ("osk", "menu", "summon"), and while the on-screen keyboard has the pad every button arrives as a command
 *   ("nav-up", "osk-type" ...). A keyboard and a mouse reach the same code: gamepad-first, never gamepad-only. */
#ifndef WISPOS_DOCK_UI_H
#define WISPOS_DOCK_UI_H

#include <stdbool.h>
#include <stdint.h>

#include "model.h"
#include "view.h"

#define UI_QUERY 48
#define UI_EFFECTS 64
#define UI_QS_ROWS 7
#define UI_ACTIONS 8
#define UI_SET_PAGES 8                  /* room for the pages of the Settings window */
#define UI_SET_ROWS 6

typedef enum {
    EFF_LAUNCH,             /* a = index into the application list */
    EFF_ACTIVATE,           /* a = window id: bring it forward */
    EFF_CLOSE,              /* a = window id */
    EFF_SAVE_PINS,          /* the pin list changed: write it */
    EFF_GAME_MODE,          /* the way back */
    EFF_TYPE,               /* a = evdev key, b = with Shift, c = with Ctrl: through the pad daemon's keyboard */
    EFF_PAD_OSK,            /* a = 1 the keyboard has the pad, 0 it has not (tells the daemon) */
    EFF_PAD_FOCUS,          /* s = application ID of the window in front (tells the daemon, which picks the browser layer) */
    EFF_SET_VOLUME,         /* f = 0..1 */
    EFF_SET_BRIGHTNESS,     /* f = 0..1 */
    EFF_SET_BLUETOOTH,      /* a = 1 on */
    EFF_SET_SSH,            /* a = 1 on */
    EFF_SET_USB,            /* a = 0 off, 1 computer, 2 host */
    EFF_SET_SCREEN_TIMEOUT, /* a = seconds without input before the panel blanks, 0 never */
    EFF_WIFI,               /* open the Wi-Fi chooser */
    EFF_REFRESH,            /* the quick settings opened: read the system values again */
    EFF_BROWSER_ACTIONS,    /* Select in the browser layer: the list of browser actions */
    EFF_DOWNLOAD,           /* a = the action of the toast that was accepted (DlAction), s = the file: carried out by wisp-download-action */
} UiEffKind;

typedef struct { UiEffKind kind; int a, b, c; float f; char s[WD_ID]; } UiEffect;

typedef enum { UI_IDLE, UI_SUMMONED, UI_ACTIONS_OPEN, UI_QS } UiDock;
typedef enum { SURF_DOCK, SURF_MENU, SURF_OSK, SURF_TOAST, SURF_SETTINGS } UiSurf;
typedef enum { PTR_LEAVE, PTR_MOVE, PTR_DOWN, PTR_UP } UiPtr;
enum { UI_SHIFT = 1, UI_CTRL = 2, UI_ALT = 4, UI_SUPER = 8 };
enum { DIRTY_DOCK = 1, DIRTY_MENU = 2, DIRTY_OSK = 4, DIRTY_TOAST = 8, DIRTY_SETTINGS = 16 };

/* the system values the quick settings and the status group show; the shell reads them and hands them in */
typedef struct {
    int wifi;                       /* signal 0..3 */
    char ssid[48];                  /* empty: not connected */
    float volume;                   /* 0..1 */
    float brightness;               /* 0..1 */
    bool bt_on, bt_available;
    bool ssh_on;
    char ssh_addr[64];              /* "wisp@192.0.2.17", or empty */
    int usb;                        /* 0 off, 1 computer, 2 host */
    float battery;                  /* 0..1, negative when there is none */
    bool charging;
    int screen_timeout;             /* seconds without input before the panel blanks, 0 never, -1 not read yet */
} UiSys;

typedef struct {
    bool dock_visible;
    int dock_h;                     /* the surface's height: the strip alone, or the whole screen while a panel is open (it then also catches the clicks outside) */
    bool dock_modal;                /* the dock holds the keyboard */
    bool menu_visible, osk_visible, toast_visible;
    bool settings_visible;          /* the Settings window above the strip; it holds the keyboard */
    int settings_h;                 /* its height: the screen above the strip */
    bool toast_modal;               /* the toast offers something to accept or dismiss: it holds the keyboard and takes clicks */
    int toast_w, toast_h;
} UiGeom;

typedef struct Ui Ui;
typedef IconSpec (*UiIconFn)(void *ctx, const WdApp *app, const char *kind_key);        /* kind_key: "apps", "game" or an application's ID */

struct Ui {
    WdApps *apps;                   /* not owned */
    WdPins pins;
    WdWindows ws;
    UiSys sys;
    char clock[8];
    int clock_w;
    UiIconFn icon_fn;
    void *icon_ctx;
    const ViewFonts *fonts;

    WdItem items[WD_MAX_ITEMS];
    int n;
    UiDock dock;
    bool from_pad;                  /* the panel was opened from the pad (so B goes back to the summoned dock) */
    int focus;                      /* the dock item with the white tile while summoned, else -1 */
    char focus_key[WD_ID];
    int hover, press;
    WdAction acts[UI_ACTIONS];
    int nacts, act_sel;
    int qs_sel;
    int qs_page;                    /* 0 the list of rows, 1 the USB port submenu (the panel keeps its size, its content changes) */
    bool dragging;                  /* a slider is being dragged with the pointer */

    bool settings;                  /* the Settings window is open */
    bool set_in;                    /* the pad is in the page's rows, not in the list of pages */
    int set_page, set_row, set_opt;

    bool menu;
    int mlist[WD_MAX_APPS], mn, msel, mtop;
    char query[UI_QUERY];

    bool osk;
    OskPage page;
    bool shift, ctrl;
    int orow, ocol;

    struct { bool on, modal; char title[64], text[96], label[16]; int action; char arg[WD_PATH]; int64_t until; } toast;
    bool scroll_mode;

    char pad_focus[WD_ID];
    UiEffect eff[UI_EFFECTS];
    int neff, dropped;
    unsigned dirty;
    int64_t now;
};

void ui_init(Ui *u, WdApps *apps, const ViewFonts *fonts);
void ui_rebuild(Ui *u);                                         /* after the application list, the pins, the windows or the clock changed */
void ui_set_clock(Ui *u, const char *text, int width);
void ui_windows_changed(Ui *u);                                 /* after wd_windows_update / remove on u->ws */

void ui_pad(Ui *u, const char *cmd);
void ui_key(Ui *u, int code, const char *utf8, unsigned mods);
void ui_pointer(Ui *u, UiSurf s, int x, int y, UiPtr ev, int button);       /* screen coordinates; button 0x110 left, 0x111 right */
void ui_wheel(Ui *u, UiSurf s, int steps);                                  /* positive: away from the user (up) */
void ui_tick(Ui *u, int64_t now_ms);
int  ui_next_wake(const Ui *u);                                            /* ms until ui_tick is wanted, or -1 */
void ui_toast(Ui *u, const char *title, const char *text, int ttl_ms);
/* A toast that offers one thing: A (Enter) or a click accepts it (an EFF_DOWNLOAD effect with `action` and `arg`), B (Escape) or a right click dismisses it. It holds the keyboard until then. */
void ui_toast_action(Ui *u, const char *title, const char *text, const char *label, int action, const char *arg, int ttl_ms);

UiGeom ui_geometry(const Ui *u);
unsigned ui_take_dirty(Ui *u);
int  ui_take_effects(Ui *u, UiEffect *out, int cap);

/* the views, filled from the state; the strings and arrays live in the scratch the caller provides and last until it is reused */
typedef struct {
    DockView dock;
    IconSpec icons[WD_MAX_ITEMS];
    Hint bar[5];
    int nbar;
    char bar_name[WD_NAME];
    bool bar_visible;
    PopupRow popup[UI_ACTIONS];
    int npopup, popup_x;
    MenuEntry menu_e[WD_MAX_APPS];
    MenuView menu;
    Hint menu_hints[4];
    QsRow qs_rows[UI_QS_ROWS];
    char qs_text[UI_QS_ROWS][64];
    QsView qs;
    Hint qs_hints[2];
    SetPage set_pages[UI_SET_PAGES];
    SetRow set_rows[UI_SET_ROWS];
    char set_note[UI_SET_ROWS][64];
    Hint set_hints[3];
    SettingsView settings;
    OskView osk;
    Hint toast_hints[2];
    ToastView toast;
} UiViews;
void ui_make_views(const Ui *u, UiViews *v);
void ui_draw_dock(const Ui *u, const Vctx *v, const UiViews *vw);       /* the strip and whatever panel is open above it */
void ui_draw_settings(const Ui *u, const Vctx *v, const UiViews *vw);   /* the Settings window (its own surface, above the strip) */

/* US keyboard: the keys behind the characters of the on-screen keyboard and the characters behind a key press */
bool ui_char_key(char ch, int *code, bool *shift);
char ui_key_char(int code, bool shift);                                 /* 0 for a key that types nothing */

#endif
