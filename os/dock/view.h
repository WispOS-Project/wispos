/* The desktop shell's views: the dock strip, the focus bar, the item-actions popup, the application menu, the quick settings, the on-screen keyboard and the toast, drawn into a canvas with
 * the renderer (render.h) from the tokens of os/ui/wisp-ui.h. They are the screens of docs/WISPOS-UI-LANGUAGE.md "Desktop mode" (LOCKED), ported from os/dock/mockup.py, and are checked
 * against its pictures (os/test_dock_view.py). A view only draws what it is given: the state (what is focused, hovered, open) lives in ui.c.
 *
 * Coordinates are those of the 720 x 480 screen: the canvas is placed on it by (ox, oy), the screen position of the canvas' top-left corner (a surface anchored to the bottom edge has a
 * negative oy). Every view draws with that offset, so the strip is at y = WISP_UI_H - WISP_UI_DOCK_H whatever surface it is on. */
#ifndef WISPOS_DOCK_VIEW_H
#define WISPOS_DOCK_VIEW_H

#include "model.h"
#include "render.h"

typedef struct {
    RdFont *title, *body, *small, *small_bold, *tiny;            /* Liberation Sans at the sizes of the tokens (Title and Small bold are Bold) */
} ViewFonts;

int  view_fonts_open(ViewFonts *f, const char *dir);             /* 0, or -1 with the first missing file on stderr */
void view_fonts_close(ViewFonts *f);

typedef struct { RdCanvas *c; int ox, oy; const ViewFonts *f; } Vctx;

/* What fills a tile: one of our glyph masks, tinted; a coloured icon; or a letter on the tile (an application with no icon is never blank). */
typedef struct {
    const char *glyph;          /* name in glyphs.h ("browser"), or NULL */
    int level;                  /* wifi 0..3, volume 0..2 */
    RdColor ink;                /* the tint of a mask */
    const RdImage *image;       /* a coloured icon, scaled to the glyph size, or NULL */
    char letter;                /* wd_tile_letter, used when there is neither */
    RdColor letter_ink;
} IconSpec;

/* glyph masks: one is looked up by name, size and level; false when the set has none (then nothing is drawn) */
bool view_glyph(const Vctx *v, const char *name, int level, int size, float cx, float cy, RdColor ink);
void view_icon(const Vctx *v, const IconSpec *ic, int size, float cx, float cy, bool focused);       /* a tile's contents; a focused tile re-tints a mask to the ground colour */
int  view_key_pill(const Vctx *v, int x, int y, const char *key, const char *label, RdColor fill);   /* the setup screen's hint; returns the x after it */

/* ------------------------------------------------------------------ the dock strip */
typedef struct {
    const WdItem *items;        /* built and laid out (wd_dock_build, wd_dock_layout): x, y, w, h in the strip's own coordinates */
    const IconSpec *icons;      /* one for every item */
    int n;
    int focus, hover, press;    /* an item index, or -1: the white tile, the LINE tile of a pointer, the accent tile of a press */
    bool summoned;              /* the strip is the thing being driven: a 2 px accent line along its top edge */
    const char *clock;
    int wifi_level, vol_level;
    float battery;              /* 0..1 */
} DockView;
void view_dock(const Vctx *v, const DockView *d);

typedef struct { const char *key, *label; } Hint;
void view_focus_bar(const Vctx *v, const char *name, const Hint *hints, int n);

typedef struct { IconSpec icon; const char *label; } PopupRow;
void view_popup(const Vctx *v, int x, const PopupRow *rows, int n, int selected, int width);
typedef struct { int x0, y0, x1, y1; } VRect;                                           /* screen coordinates, x1 and y1 exclusive */
VRect view_popup_rect(int x, int n, int width);                                         /* where view_popup puts its panel */
int  view_popup_hit(int x, int n, int width, int px, int py);                           /* the row under the point, -2 on the panel but on no row, -1 outside */

/* ------------------------------------------------------------------ the application menu */
typedef struct { IconSpec icon; const char *name; bool pinned; } MenuEntry;
typedef struct {
    const MenuEntry *e;
    int n, selected, top_row;
    const char *title, *subtitle;
    const Hint *hints;
    int nhints;
    const char *query;          /* the search text, or NULL: when set it replaces the subtitle */
} MenuView;
int  view_menu_rows_visible(void);                    /* whole rows of tiles between the title and the hints */
void view_menu(const Vctx *v, const MenuView *m);
int  view_menu_hit(const MenuView *m, int px, int py);                                  /* the entry whose cell (tile and name) holds the point, or -1 */

/* ------------------------------------------------------------------ the quick settings */
typedef enum { QS_TEXT, QS_SWITCH, QS_SLIDER, QS_LINK, QS_BACK, QS_CHOICE, QS_NOTE } QsKind;
/* QS_LINK: a row that opens a submenu: label, the current value at the right and a chevron. QS_BACK: the first row of a submenu: a left chevron and the title in bold (label).
 * QS_CHOICE: a row of a submenu: label, a second line (value) and a mark at the right when `on`. QS_NOTE: a line of small text on no row (label); it is never selected. */
typedef struct {
    QsKind kind;
    const char *glyph;
    int level;
    const char *label;
    const char *value;          /* TEXT: at the right; SWITCH: a second line under the label (or NULL) */
    bool on;                    /* SWITCH */
    float slider;               /* SLIDER 0..1; for the battery glyph, the charge 0..1 */
} QsRow;
typedef struct { const QsRow *rows; int n, selected; const Hint *hints; int nhints; int height; } QsView;   /* height: the panel's height when it must not follow its rows (a submenu keeps the list's size), else 0 */
int  view_quick_height(const QsView *q);              /* the panel's height */
void view_quick(const Vctx *v, const QsView *q);      /* above the strip's right end */
VRect view_quick_rect(const QsView *q);
typedef struct { int row; float frac; } QsHit;   /* row: -1 outside the panel, -2 on it but on no row; frac: 0..1 along a slider's track */
QsHit view_quick_hit(const QsView *q, int px, int py);

/* ------------------------------------------------------------------ the settings panel */
/* A window above the strip: a title, a list of categories at the left and the selected category's rows at the right. A row is a setting whose options sit side by side;
 * `on` is the option that applies, `focus` the one under the pad while the row has it. Drawn from the approved pictures (os/dock/design/settings-*.png). */
typedef struct { const char *name, *glyph; float level; } SetPage;      /* level: the signal bars of a wifi glyph (0..3) or the charge of a battery glyph (0..1) */
typedef struct { const char *title, *note; const char *opt[6]; int nopt, on, focus; } SetRow;
typedef struct { const SetPage *pages; int npages, page; const SetRow *rows; int nrows, row; bool in_content; const Hint *hints; int nhints; } SettingsView;
int  view_settings_height(void);                         /* the window's height: the screen above the strip */
void view_settings(const Vctx *v, const SettingsView *s);
typedef struct { int page, row, opt; } SetHit;           /* page >= 0: a category; row >= 0 and opt >= 0: an option; all -1: nothing under the point */
SetHit view_settings_hit(const SettingsView *s, int px, int py);

/* ------------------------------------------------------------------ the on-screen keyboard */
typedef enum { OSK_LOWER, OSK_UPPER, OSK_SYMBOLS } OskPage;
typedef struct { OskPage page; bool shift, ctrl; int row, col; } OskView;       /* the focus is a (row, column); row 4 is the specials */
void view_osk(const Vctx *v, const OskView *o);
void view_osk_hints(const Vctx *v);
/* The keyboard's key table: what is at (row, col) of a page, for the shell (what to type) and the tests. Returns false past the end of a row. */
typedef struct { const char *label; int width; const char *glyph; RdColor base; const char *kind; } OskKey;    /* kind: "char" for the letter rows, else the special's name */
bool view_osk_key(OskPage page, bool shift, bool ctrl, int row, int col, OskKey *out);
int  view_osk_row_len(OskPage page, int row);
bool view_osk_rect(OskPage page, int row, int col, VRect *out);                         /* where a key is on the screen */
bool view_osk_hit(OskPage page, int px, int py, int *row, int *col);                    /* the key under the point */
int  view_osk_col_near(OskPage page, int row, int cx);                                  /* the column of `row` whose key is centred nearest to x = cx */
VRect view_osk_panel(void);

/* ------------------------------------------------------------------ the toast */
typedef struct { const char *title, *text; const Hint *hints; int nhints; } ToastView;
void view_toast_size(const ViewFonts *f, const ToastView *t, int *w, int *h);
void view_toast(const Vctx *v, const ToastView *t);   /* drawn at the canvas' own top-left corner (a toast has a surface of its own) */

#endif
