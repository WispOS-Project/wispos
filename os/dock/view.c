/* The desktop shell's views: see view.h. Ported from os/dock/mockup.py; the numbers in it are the locked design, so they are copied as they are and the tokens come from wisp-ui.h. */
#define _GNU_SOURCE
#include "view.h"

#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "../ui/wisp-ui.h"
#include "glyphs.h"

#define C(rgb) RD_RGB(rgb)
#define BG C(WISP_UI_BG)
#define PANEL C(WISP_UI_PANEL)
#define LINE C(WISP_UI_LINE)
#define FIELD C(WISP_UI_FIELD)
#define TEXT C(WISP_UI_TEXT)
#define DIM C(WISP_UI_DIM)
#define GLYPHC C(WISP_UI_GLYPH)
#define ACCENT C(WISP_UI_ACCENT)
#define OK C(WISP_UI_OK)
#define FOCUS C(WISP_UI_FOCUS)
#define FOCUS_FG C(WISP_UI_FOCUS_FG)
#define FOCUS_DIM C(WISP_UI_FOCUS_DIM)
#define FOCUS_OK C(WISP_UI_FOCUS_OK)
#define DOCK_TOP (WISP_UI_H - WISP_UI_DOCK_H)

#define X(v, x) ((float)((x) + (v)->ox))
#define Y(v, y) ((float)((y) + (v)->oy))
#define IX(v, x) ((x) + (v)->ox)
#define IY(v, y) ((y) + (v)->oy)

/* ------------------------------------------------------------------ fonts and glyphs */
int view_fonts_open(ViewFonts *f, const char *dir)
{
    char p[512];
    memset(f, 0, sizeof *f);
    struct { RdFont **slot; const char *file; int px; } want[] = {
        { &f->title, WISP_UI_FONT_BOLD, WISP_UI_SIZE_TITLE }, { &f->body, WISP_UI_FONT_REGULAR, WISP_UI_SIZE_BODY }, { &f->small, WISP_UI_FONT_REGULAR, WISP_UI_SIZE_SMALL },
        { &f->small_bold, WISP_UI_FONT_BOLD, WISP_UI_SIZE_SMALL }, { &f->tiny, WISP_UI_FONT_REGULAR, WISP_UI_SIZE_TINY },
    };
    for (size_t i = 0; i < sizeof want / sizeof *want; i++) {
        snprintf(p, sizeof p, "%s/%s", dir, want[i].file);
        *want[i].slot = rd_font_open(p, want[i].px);
        if (!*want[i].slot) { fprintf(stderr, "wisp-dock: cannot open font %s\n", p); view_fonts_close(f); return -1; }
    }
    return 0;
}

void view_fonts_close(ViewFonts *f)
{
    RdFont **all[] = { &f->title, &f->body, &f->small, &f->small_bold, &f->tiny };
    for (size_t i = 0; i < sizeof all / sizeof *all; i++) { rd_font_close(*all[i]); *all[i] = NULL; }
}

bool view_glyph(const Vctx *v, const char *name, int level, int size, float cx, float cy, RdColor ink)
{
    if (!name) return false;
    for (int i = 0; i < GLYPH_MASK_COUNT; i++) {
        const GlyphMask *m = &GLYPH_MASKS[i];
        if (m->size != size || m->level != level || strcmp(m->name, name) != 0) continue;
        int x0 = (int)lrintf(cx - (float)size / 2.f), y0 = (int)lrintf(cy - (float)size / 2.f);       /* Python's round(): to the even neighbour on a half */
        rd_mask(v->c, m->a, size, size, IX(v, x0), IY(v, y0), ink);
        return true;
    }
    return false;
}

void view_icon(const Vctx *v, const IconSpec *ic, int size, float cx, float cy, bool focused)
{
    if (ic->glyph && view_glyph(v, ic->glyph, ic->level, size, cx, cy, focused ? FOCUS_FG : ic->ink)) return;
    if (ic->image) {
        int x0 = (int)lrintf(cx - (float)size / 2.f), y0 = (int)lrintf(cy - (float)size / 2.f);
        rd_blit_scaled(v->c, ic->image, IX(v, x0), IY(v, y0), size, size);
        return;
    }
    char s[2] = { ic->letter ? ic->letter : '?', 0 };                                         /* no icon: a letter, never a blank tile */
    RdFont *f = size >= 40 ? v->f->title : v->f->small_bold;
    rd_text(v->c, f, (int)lrintf(IX(v, cx)), (int)lrintf(IY(v, cy)), s, focused ? FOCUS_FG : (ic->letter_ink ? ic->letter_ink : GLYPHC), RD_CENTER, RD_MIDDLE);
}

int view_key_pill(const Vctx *v, int x, int y, const char *key, const char *label, RdColor fill)
{
    int kw = rd_text_width(v->f->tiny, key), pill = kw + 12 > 20 ? kw + 12 : 20;
    rd_pill(v->c, X(v, x), Y(v, y), X(v, x + pill), Y(v, y + 20), fill);
    rd_text(v->c, v->f->tiny, IX(v, x) + (pill + 1) / 2, IY(v, y) + 2, key, GLYPHC, RD_CENTER, RD_TOP);          /* the pill's centre rounds up on a half, as Pillow does */
    rd_text(v->c, v->f->tiny, IX(v, x + pill + 8), IY(v, y) + 2, label, DIM, RD_LEFT, RD_TOP);
    return x + pill + 8 + rd_text_width(v->f->tiny, label) + 18;
}

/* the battery glyph: the outline mask, then the fill at the charge (mockup.py's g_battery draws the fill inside the 24-unit grid) */
static void battery(const Vctx *v, int size, float cx, float cy, RdColor ink, float level)
{
    view_glyph(v, "battery", 0, size, cx, cy, ink);
    float u = (float)size / 24.f, bx = (float)lrintf(cx - (float)size / 2.f), by = (float)lrintf(cy - (float)size / 2.f);
    level = level < 0.f ? 0.f : level > 1.f ? 1.f : level;
    if (level > 0.f) rd_rrect(v->c, X(v, 0) + bx + 4.6f * u, Y(v, 0) + by + 9.f * u, X(v, 0) + bx + (4.6f + 13.2f * level) * u, Y(v, 0) + by + 15.f * u, 0.f, ink);
}

/* ------------------------------------------------------------------ the dock strip */
static void tile_fill(const Vctx *v, const DockView *d, int i, const WdItem *it, RdColor idle)
{
    RdColor fill = i == d->focus ? FOCUS : i == d->press ? ACCENT : i == d->hover ? LINE : idle;
    rd_rrect(v->c, X(v, it->x), Y(v, DOCK_TOP + it->y), X(v, it->x + it->w), Y(v, DOCK_TOP + it->y + it->h), WISP_UI_RADIUS, fill);
}

void view_dock(const Vctx *v, const DockView *d)
{
    const int tile = WISP_UI_DOCK_TILE, icon = WISP_UI_DOCK_ICON, st = WISP_UI_DOCK_STATUS, mid = DOCK_TOP + WISP_UI_DOCK_H / 2 + 1;
    rd_rrect(v->c, X(v, 0), Y(v, DOCK_TOP), X(v, WISP_UI_W), Y(v, DOCK_TOP + WISP_UI_DOCK_H), 0, RD_RGBA(WISP_UI_PANEL, WISP_UI_DOCK_ALPHA));
    rd_hline(v->c, X(v, 0), X(v, WISP_UI_W), Y(v, DOCK_TOP), d->summoned ? 2.f : 1.f, d->summoned ? ACCENT : LINE);
    for (int i = 0; i < d->n; i++) {
        const WdItem *it = &d->items[i];
        if (it->kind == WD_SEP) { rd_vline(v->c, X(v, it->x), Y(v, DOCK_TOP + it->y), Y(v, DOCK_TOP + it->y + it->h), (float)it->w, LINE); continue; }
        bool focused = i == d->focus;
        if (it->kind == WD_STATUS) {
            if (i == d->focus || i == d->hover || i == d->press) tile_fill(v, d, i, it, BG);
            RdColor ink = focused ? FOCUS_FG : GLYPHC;
            float gx = (float)(it->x + 10);
            view_glyph(v, "wifi", d->wifi_level, st, gx + st / 2.f, (float)mid, ink); gx += (float)(st + 8);
            view_glyph(v, "volume", d->vol_level, st, gx + st / 2.f, (float)mid, ink); gx += (float)(st + 8);
            battery(v, st, gx + st / 2.f, (float)mid, ink, d->battery);
            rd_text(v->c, v->f->small, IX(v, it->x + it->w - 10), IY(v, mid), d->clock ? d->clock : "", focused ? FOCUS_FG : TEXT, RD_RIGHT, RD_MIDDLE);
            continue;
        }
        tile_fill(v, d, i, it, BG);
        float cx = (float)it->x + (float)tile / 2.f, cy = (float)(DOCK_TOP + it->y) + (float)tile / 2.f - (it->running ? 2.f : 0.f);
        view_icon(v, &d->icons[i], icon, cx, cy, focused);
        if (it->running) {
            float my = (float)(DOCK_TOP + it->y + tile - 6);
            if (it->active) {
                float half = WISP_UI_DOCK_RUN_BAR / 2.f;
                rd_pill(v->c, X(v, 0) + cx - half, Y(v, 0) + my, X(v, 0) + cx + half, Y(v, 0) + my + WISP_UI_DOCK_RUN_H, focused ? FOCUS_OK : ACCENT);
            } else {
                rd_disc(v->c, X(v, 0) + cx, Y(v, 0) + my + 1.5f, WISP_UI_DOCK_RUN_DOT / 2.f, focused ? FOCUS_DIM : DIM);
            }
        }
    }
}

void view_focus_bar(const Vctx *v, const char *name, const Hint *hints, int n)
{
    const int bh = WISP_UI_DOCK_BAR;
    rd_rrect(v->c, X(v, 0), Y(v, DOCK_TOP - bh), X(v, WISP_UI_W), Y(v, DOCK_TOP), 0, RD_RGBA(WISP_UI_PANEL, WISP_UI_DOCK_ALPHA));
    rd_text(v->c, v->f->small_bold, IX(v, 12), IY(v, DOCK_TOP - bh / 2), name, TEXT, RD_LEFT, RD_MIDDLE);
    int total = 0, pill[16], adv[16];
    if (n > 16) n = 16;
    for (int i = 0; i < n; i++) {
        int kw = rd_text_width(v->f->tiny, hints[i].key);
        pill[i] = kw + 12 > 20 ? kw + 12 : 20;
        adv[i] = pill[i] + 8 + rd_text_width(v->f->tiny, hints[i].label) + 14;
        total += adv[i];
    }
    int x = WISP_UI_W - 12 - total + 14;
    for (int i = 0; i < n; i++) {
        int y = DOCK_TOP - bh / 2 - 10;
        rd_pill(v->c, X(v, x), Y(v, y), X(v, x + pill[i]), Y(v, y + 20), BG);
        rd_text(v->c, v->f->tiny, IX(v, x) + (pill[i] + 1) / 2, IY(v, y) + 2, hints[i].key, GLYPHC, RD_CENTER, RD_TOP);
        rd_text(v->c, v->f->tiny, IX(v, x + pill[i] + 8), IY(v, y) + 2, hints[i].label, DIM, RD_LEFT, RD_TOP);
        x += adv[i];
    }
}

VRect view_popup_rect(int x, int n, int width)
{
    int x0 = x < 6 ? 6 : x, y1 = DOCK_TOP - WISP_UI_DOCK_BAR - 6, y0 = y1 - (n * WISP_UI_POPUP_ROW + 12);
    if (x0 > WISP_UI_W - width - 6) x0 = WISP_UI_W - width - 6;
    return (VRect){ x0, y0, x0 + width, y1 };
}

int view_popup_hit(int x, int n, int width, int px, int py)
{
    VRect r = view_popup_rect(x, n, width);
    if (px < r.x0 || px >= r.x1 || py < r.y0 || py >= r.y1) return -1;
    for (int i = 0; i < n; i++) {
        int ry = r.y0 + 6 + i * WISP_UI_POPUP_ROW;
        if (py >= ry && py < ry + WISP_UI_POPUP_ROW - 2 && px >= r.x0 + 6 && px < r.x1 - 6) return i;
    }
    return -2;
}

void view_popup(const Vctx *v, int x, const PopupRow *rows, int n, int selected, int width)
{
    const int rh = WISP_UI_POPUP_ROW, h = n * rh + 12;
    VRect pr = view_popup_rect(x, n, width);
    int x0 = pr.x0, y1 = pr.y1, y0 = y1 - h;
    rd_rrect(v->c, X(v, x0), Y(v, y0), X(v, x0 + width), Y(v, y1), WISP_UI_RADIUS, RD_RGBA(WISP_UI_BG, 250));
    rd_rrect_ring(v->c, X(v, x0), Y(v, y0), X(v, x0 + width), Y(v, y1), WISP_UI_RADIUS, 1.f, LINE);
    for (int i = 0; i < n; i++) {
        int ry = y0 + 6 + i * rh;
        bool sel = i == selected;
        if (sel) rd_rrect(v->c, X(v, x0 + 6), Y(v, ry), X(v, x0 + width - 6), Y(v, ry + rh - 2), WISP_UI_RADIUS, FOCUS);
        IconSpec ic = rows[i].icon;
        if (ic.glyph) ic.ink = GLYPHC;
        view_icon(v, &ic, 18, (float)(x0 + 26), (float)ry + (float)rh / 2.f - 1.f, sel);
        rd_text_fit(v->c, v->f->small, IX(v, x0 + 48), IY(v, ry + rh / 2 - 1), width - 48 - 10, rows[i].label, sel ? FOCUS_FG : TEXT, RD_LEFT, RD_MIDDLE);
    }
}

/* ------------------------------------------------------------------ the application menu */
int view_menu_rows_visible(void) { return (WISP_UI_HINT_Y - 8 - WISP_UI_MENU_Y) / WISP_UI_MENU_CELL_H; }

void view_menu(const Vctx *v, const MenuView *m)
{
    const int cols = WISP_UI_MENU_COLS, cw = WISP_UI_MENU_CELL_W, ch = WISP_UI_MENU_CELL_H, tile = WISP_UI_MENU_TILE, icon = WISP_UI_MENU_ICON;
    const int left = WISP_UI_MENU_X + (cw - tile) / 2;
    rd_rrect(v->c, X(v, 0), Y(v, 0), X(v, WISP_UI_W), Y(v, WISP_UI_H), 0, RD_RGBA(WISP_UI_BG, WISP_UI_MENU_SCRIM));
    rd_text(v->c, v->f->title, IX(v, left), IY(v, WISP_UI_TITLE_Y), m->title, TEXT, RD_LEFT, RD_TOP);
    if (m->query) {
        char q[160];
        snprintf(q, sizeof q, "Search: %s_", m->query);
        rd_text_fit(v->c, v->f->small, IX(v, left), IY(v, WISP_UI_SUBTITLE_Y), 560, q, TEXT, RD_LEFT, RD_TOP);
    } else {
        rd_text_fit(v->c, v->f->small, IX(v, left), IY(v, WISP_UI_SUBTITLE_Y), 560, m->subtitle, DIM, RD_LEFT, RD_TOP);
    }
    int rows = view_menu_rows_visible();
    for (int i = 0; i < m->n; i++) {
        int row = i / cols - m->top_row;
        if (row < 0 || row >= rows) continue;
        int cx = WISP_UI_MENU_X + (i % cols) * cw + cw / 2, top = WISP_UI_MENU_Y + row * ch;
        bool sel = i == m->selected;
        rd_rrect(v->c, X(v, cx) - (float)tile / 2.f, Y(v, top), X(v, cx) + (float)tile / 2.f, Y(v, top + tile), WISP_UI_RADIUS, sel ? FOCUS : PANEL);
        view_icon(v, &m->e[i].icon, icon, (float)cx, (float)top + (float)tile / 2.f, sel);
        if (m->e[i].pinned) view_glyph(v, "pin", 0, 14, (float)(cx + tile / 2 - 11), (float)(top + 11), sel ? FOCUS_DIM : DIM);
        rd_text_fit(v->c, v->f->small, IX(v, cx), IY(v, top + tile + 16), cw - 8, m->e[i].name, TEXT, RD_CENTER, RD_MIDDLE);
    }
    int x = left;
    for (int i = 0; i < m->nhints; i++) x = view_key_pill(v, x, WISP_UI_HINT_Y, m->hints[i].key, m->hints[i].label, PANEL);
}

int view_menu_hit(const MenuView *m, int px, int py)
{
    const int cols = WISP_UI_MENU_COLS, cw = WISP_UI_MENU_CELL_W, ch = WISP_UI_MENU_CELL_H, tile = WISP_UI_MENU_TILE;
    int rows = view_menu_rows_visible();
    for (int i = 0; i < m->n; i++) {
        int row = i / cols - m->top_row;
        if (row < 0 || row >= rows) continue;
        int x0 = WISP_UI_MENU_X + (i % cols) * cw, top = WISP_UI_MENU_Y + row * ch;
        if (px >= x0 + 4 && px < x0 + cw - 4 && py >= top && py < top + tile + 30) return i;
    }
    return -1;
}

/* ------------------------------------------------------------------ the quick settings */
static int qs_row_height(const QsRow *r)
{
    int rh = WISP_UI_POPUP_ROW + 6;
    if (r->kind == QS_SWITCH && r->value) return rh + 10;
    if (r->kind == QS_CHOICE) return rh + rh / 2;
    if (r->kind == QS_NOTE) return 30;
    return rh;
}

int view_quick_height(const QsView *q)
{
    if (q->height > 0) return q->height;
    int h = 12 + 34 + 6;
    for (int i = 0; i < q->n; i++) h += qs_row_height(&q->rows[i]);
    return h;
}

VRect view_quick_rect(const QsView *q)
{
    int h = view_quick_height(q), x0 = WISP_UI_W - WISP_UI_QS_W - 8, y1 = DOCK_TOP - 8;
    return (VRect){ x0, y1 - h, x0 + WISP_UI_QS_W, y1 };
}

QsHit view_quick_hit(const QsView *q, int px, int py)
{
    const int pw = WISP_UI_QS_W;
    VRect r = view_quick_rect(q);
    QsHit none = { -1, 0.f };
    if (px < r.x0 || px >= r.x1 || py < r.y0 || py >= r.y1) return none;
    none.row = -2;
    int y = r.y0 + 12;
    for (int i = 0; i < q->n; i++) {
        int hh = qs_row_height(&q->rows[i]);
        if (q->rows[i].kind != QS_NOTE && py >= y && py < y + hh - 6 && px >= r.x0 + 10 && px < r.x0 + pw - 10) {
            QsHit h = { i, 0.f };
            if (q->rows[i].kind == QS_SLIDER) {
                float t = (float)(px - (r.x0 + 62)) / (float)(pw - 28 - 62);
                h.frac = t < 0.f ? 0.f : t > 1.f ? 1.f : t;
            }
            return h;
        }
        y += hh;
    }
    return none;
}

void view_quick(const Vctx *v, const QsView *q)
{
    const int pw = WISP_UI_QS_W, foot = 34;
    int h = view_quick_height(q), x0 = WISP_UI_W - pw - 8, y1 = DOCK_TOP - 8, y0 = y1 - h;
    rd_rrect(v->c, X(v, x0), Y(v, y0), X(v, x0 + pw), Y(v, y1), WISP_UI_RADIUS, RD_RGBA(WISP_UI_BG, 250));
    rd_rrect_ring(v->c, X(v, x0), Y(v, y0), X(v, x0 + pw), Y(v, y1), WISP_UI_RADIUS, 1.f, LINE);
    int y = y0 + 12;
    for (int i = 0; i < q->n; i++) {
        const QsRow *r = &q->rows[i];
        int hh = qs_row_height(r);
        bool sel = i == q->selected;
        if (r->kind == QS_NOTE) {
            rd_text(v->c, v->f->tiny, IX(v, x0 + 18), IY(v, y + 6), r->label, DIM, RD_LEFT, RD_MIDDLE);
            y += hh;
            continue;
        }
        rd_rrect(v->c, X(v, x0 + 10), Y(v, y), X(v, x0 + pw - 10), Y(v, y + hh - 6), WISP_UI_RADIUS, sel ? FOCUS : PANEL);
        RdColor fg = sel ? FOCUS_FG : TEXT, dim = sel ? FOCUS_DIM : DIM;
        int top = y + (hh - 6) / 2;
        if (r->kind == QS_BACK) {
            view_glyph(v, "left", 0, 18, (float)(x0 + 28), (float)top, sel ? FOCUS_FG : GLYPHC);
            rd_text(v->c, v->f->small_bold, IX(v, x0 + 50), IY(v, top), r->label, fg, RD_LEFT, RD_MIDDLE);
            y += hh;
            continue;
        }
        if (r->kind == QS_CHOICE) {
            rd_text(v->c, v->f->small, IX(v, x0 + 36), IY(v, top - 8), r->label, fg, RD_LEFT, RD_MIDDLE);
            if (r->value) rd_text_fit(v->c, v->f->tiny, IX(v, x0 + 36), IY(v, top + 11), pw - 36 - 56, r->value, dim, RD_LEFT, RD_MIDDLE);
            rd_disc(v->c, X(v, x0 + pw - 34), Y(v, top), 8.f, r->on ? (sel ? FOCUS_FG : ACCENT) : BG);       /* the mode that is on has a filled mark, the others a dark one */
            y += hh;
            continue;
        }
        if (r->glyph && strcmp(r->glyph, "battery") == 0) battery(v, 18, (float)(x0 + 32), (float)top, sel ? FOCUS_FG : GLYPHC, r->slider);
        else view_glyph(v, r->glyph, r->level, 18, (float)(x0 + 32), (float)top, sel ? FOCUS_FG : GLYPHC);
        if (r->kind == QS_SLIDER) {
            int tx0 = x0 + 62, tx1 = x0 + pw - 28;
            float val = r->slider < 0.f ? 0.f : r->slider > 1.f ? 1.f : r->slider, kx = (float)tx0 + (float)(tx1 - tx0) * val;
            rd_pill(v->c, X(v, tx0), Y(v, top - 2), X(v, tx1), Y(v, top + 2), sel ? FOCUS_DIM : LINE);
            rd_pill(v->c, X(v, tx0), Y(v, top - 2), X(v, 0) + kx, Y(v, top + 2), sel ? FOCUS_FG : ACCENT);
            rd_disc(v->c, X(v, 0) + kx, Y(v, top), (float)WISP_UI_SLIDER_KNOB, sel ? FOCUS_FG : TEXT);
        } else {
            bool two = r->kind == QS_SWITCH && r->value;
            rd_text(v->c, v->f->small, IX(v, x0 + 62), IY(v, top - (two ? 8 : 0)), r->label, fg, RD_LEFT, RD_MIDDLE);
            if (two) rd_text_fit(v->c, v->f->tiny, IX(v, x0 + 62), IY(v, top + 11), pw - 62 - 76, r->value, dim, RD_LEFT, RD_MIDDLE);
            if (r->kind == QS_SWITCH) {
                int sx = x0 + pw - 28 - WISP_UI_SWITCH_W;
                rd_pill(v->c, X(v, sx), Y(v, top - 11), X(v, sx + WISP_UI_SWITCH_W), Y(v, top + 11), r->on ? (sel ? FOCUS_OK : OK) : LINE);
                rd_disc(v->c, X(v, sx + (r->on ? 29 : 11)), Y(v, top), 8.f, (sel && r->on) ? FOCUS : TEXT);
            } else if (r->kind == QS_LINK) {
                if (r->value) rd_text_fit(v->c, v->f->tiny, IX(v, x0 + pw - 40), IY(v, top), pw - 62 - 40 - 28, r->value, dim, RD_RIGHT, RD_MIDDLE);
                view_glyph(v, "right", 0, 18, (float)(x0 + pw - 28), (float)top, sel ? FOCUS_FG : DIM);
            } else if (r->value) {
                rd_text_fit(v->c, v->f->tiny, IX(v, x0 + pw - 22), IY(v, top), pw - 62 - 22 - 70, r->value, dim, RD_RIGHT, RD_MIDDLE);
            }
        }
        y += hh;
    }
    int hx = x0 + 14;
    for (int i = 0; i < q->nhints; i++) hx = view_key_pill(v, hx, y1 - foot + 4, q->hints[i].key, q->hints[i].label, PANEL);
}

/* ------------------------------------------------------------------ the settings panel */
#define SET_SIDE 176                                      /* the category list's right edge */
#define SET_ROW_Y(i) (80 + 90 * (i))                      /* a row's title line */
#define SET_X0 (SET_SIDE + 18)
#define SET_X1 (WISP_UI_W - 18)

int view_settings_height(void) { return DOCK_TOP; }

void view_settings(const Vctx *v, const SettingsView *s)
{
    rd_rrect(v->c, X(v, 0), Y(v, 0), X(v, WISP_UI_W), Y(v, DOCK_TOP), 0, BG);
    rd_text(v->c, v->f->title, IX(v, 20), IY(v, 22), "Settings", TEXT, RD_LEFT, RD_MIDDLE);
    rd_hline(v->c, X(v, 0), X(v, WISP_UI_W), Y(v, 44), 1.f, LINE);
    for (int i = 0; i < s->npages; i++) {
        int y = 56 + 44 * i;
        bool on = i == s->page;
        if (on) rd_rrect(v->c, X(v, 10), Y(v, y), X(v, SET_SIDE - 8), Y(v, y + 40), WISP_UI_RADIUS, FOCUS);
        RdColor ink = on ? FOCUS_FG : GLYPHC;
        if (strcmp(s->pages[i].glyph, "battery") == 0) battery(v, 18, 32.f, (float)(y + 20), ink, s->pages[i].level);
        else view_glyph(v, s->pages[i].glyph, (int)s->pages[i].level, 18, 32.f, (float)(y + 20), ink);
        rd_text(v->c, v->f->small, IX(v, 52), IY(v, y + 20), s->pages[i].name, on ? FOCUS_FG : TEXT, RD_LEFT, RD_MIDDLE);
    }
    rd_vline(v->c, X(v, SET_SIDE), Y(v, 52), Y(v, DOCK_TOP - 10), 1.f, LINE);
    for (int r = 0; r < s->nrows; r++) {
        const SetRow *row = &s->rows[r];
        int y = SET_ROW_Y(r);
        bool sel = s->in_content && r == s->row;
        rd_text(v->c, v->f->small_bold, IX(v, SET_X0), IY(v, y), row->title, TEXT, RD_LEFT, RD_MIDDLE);
        if (row->note) rd_text(v->c, v->f->tiny, IX(v, SET_X0), IY(v, y + 18), row->note, DIM, RD_LEFT, RD_MIDDLE);
        float w = (float)(SET_X1 - SET_X0) / (float)row->nopt;
        for (int k = 0; k < row->nopt; k++) {
            float sx = (float)SET_X0 + (float)k * w;
            bool on = k == row->on;
            rd_rrect(v->c, X(v, 0) + sx, Y(v, y + 34), X(v, 0) + sx + w - 4.f, Y(v, y + 64), WISP_UI_RADIUS, (on && sel) ? FOCUS : on ? TEXT : PANEL);
            if (sel && k == row->focus) rd_rrect_ring(v->c, X(v, 0) + sx, Y(v, y + 34), X(v, 0) + sx + w - 4.f, Y(v, y + 64), WISP_UI_RADIUS, 2.f, ACCENT);
            rd_text(v->c, v->f->tiny, (int)floorf(X(v, 0) + sx + (w - 4.f) / 2.f + 0.5f), IY(v, y + 49), row->opt[k], on ? FOCUS_FG : DIM, RD_CENTER, RD_MIDDLE);
        }
    }
    int hx = SET_X0;
    for (int i = 0; i < s->nhints; i++) hx = view_key_pill(v, hx, DOCK_TOP - 32, s->hints[i].key, s->hints[i].label, PANEL);
}

SetHit view_settings_hit(const SettingsView *s, int px, int py)
{
    SetHit none = { -1, -1, -1 };
    if (py < 0 || py >= DOCK_TOP) return none;
    for (int i = 0; i < s->npages; i++) {
        int y = 56 + 44 * i;
        if (px >= 10 && px < SET_SIDE - 8 && py >= y && py < y + 40) return (SetHit){ i, -1, -1 };
    }
    for (int r = 0; r < s->nrows; r++) {
        int y = SET_ROW_Y(r) + 34;
        if (py < y || py >= y + 30 || px < SET_X0 || px >= SET_X1) continue;
        int k = (int)((float)(px - SET_X0) / ((float)(SET_X1 - SET_X0) / (float)s->rows[r].nopt));
        if (k >= 0 && k < s->rows[r].nopt) return (SetHit){ -1, r, k };
    }
    return none;
}

/* ------------------------------------------------------------------ the on-screen keyboard */
static const char *const ROWS_LOWER[4] = { "1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm" };
static const char *const ROWS_UPPER[4] = { "1234567890", "QWERTYUIOP", "ASDFGHJKL", "ZXCVBNM" };
static const char *const ROWS_SYM[4] = { "1234567890", "!@#$%^&*()", "-_=+[]{};:", "'\",.<>/?\\|" };
static const char CH[128][2] = {
#define C1(n) [n] = { (char)(n), 0 }
#define C8(n) C1(n), C1(n + 1), C1(n + 2), C1(n + 3), C1(n + 4), C1(n + 5), C1(n + 6), C1(n + 7)
    C8(32), C8(40), C8(48), C8(56), C8(64), C8(72), C8(80), C8(88), C8(96), C8(104), C8(112), C8(120),
};

static const char *const *osk_rows(OskPage p) { return p == OSK_SYMBOLS ? ROWS_SYM : p == OSK_UPPER ? ROWS_UPPER : ROWS_LOWER; }

typedef struct { const char *label; int w; const char *glyph; int base; const char *kind; } Special;   /* base: 0 field, 1 accent */
static const Special SPEC_LETTERS[7] = {
    { "123", 70, NULL, 0, "123" }, { "shift", 78, NULL, 0, "shift" }, { "ctrl", 62, NULL, 0, "ctrl" }, { "space", 178, NULL, 0, "space" },
    { "tab", 62, NULL, 0, "tab" }, { "esc", 62, NULL, 0, "esc" }, { "enter", 100, NULL, 1, "enter" },
};
static const Special SPEC_SYMBOLS[10] = {
    { "abc", 70, NULL, 0, "abc" }, { "`", 52, NULL, 0, "char" }, { "~", 52, NULL, 0, "char" }, { "", 52, "left", 0, "left" }, { "", 52, "down", 0, "down" },
    { "", 52, "up", 0, "up" }, { "", 52, "right", 0, "right" }, { "space", 110, NULL, 0, "space" }, { "del", 64, NULL, 0, "del" }, { "enter", 90, NULL, 1, "enter" },
};

int view_osk_row_len(OskPage page, int row)
{
    if (row < 0 || row > 4) return 0;
    if (row == 4) return page == OSK_SYMBOLS ? 10 : 7;
    return (int)strlen(osk_rows(page)[row]) + (row == 3 && page != OSK_SYMBOLS ? 1 : 0);
}

bool view_osk_key(OskPage page, bool shift, bool ctrl, int row, int col, OskKey *out)
{
    if (col < 0 || col >= view_osk_row_len(page, row)) return false;
    memset(out, 0, sizeof *out);
    out->base = FIELD;
    if (row < 4) {
        const char *r = osk_rows(page)[row];
        if (col < (int)strlen(r)) {
            out->label = CH[(unsigned char)r[col] & 127];
            out->width = WISP_UI_KEY_W;
            out->base = BG;
            out->kind = "char";
            return true;
        }
        out->label = "del"; out->width = 92; out->kind = "del";
        return true;
    }
    const Special *s = page == OSK_SYMBOLS ? &SPEC_SYMBOLS[col] : &SPEC_LETTERS[col];
    out->label = (strcmp(s->kind, "char") == 0) ? CH[(unsigned char)s->label[0] & 127] : s->label;
    out->width = s->w;
    out->glyph = s->glyph;
    out->kind = s->kind;
    out->base = s->base ? ACCENT : FIELD;
    if (page != OSK_SYMBOLS) {
        if (col == 1 && (shift || page == OSK_UPPER)) out->base = ACCENT;
        if (col == 2 && ctrl) out->base = ACCENT;
    }
    return true;
}

/* the left edge and width of every key of a row, as the keyboard lays them out (centred) */
static void osk_row_layout(OskPage page, int row, int *x0, int *total)
{
    int n = view_osk_row_len(page, row), t = 0;
    for (int i = 0; i < n; i++) { OskKey k; view_osk_key(page, false, false, row, i, &k); t += k.width + (i ? WISP_UI_KEY_GAP : 0); }
    *total = t;
    *x0 = (WISP_UI_W - t) / 2;
}

VRect view_osk_panel(void)
{
    return (VRect){ 12, WISP_UI_OSK_Y, WISP_UI_W - 12, WISP_UI_OSK_Y + 4 * (WISP_UI_KEY_H + WISP_UI_KEY_GAP) + WISP_UI_KEY_H + 24 };
}

bool view_osk_rect(OskPage page, int row, int col, VRect *out)
{
    if (col < 0 || col >= view_osk_row_len(page, row)) return false;
    int x, total;
    osk_row_layout(page, row, &x, &total);
    for (int i = 0; i < col; i++) { OskKey k; view_osk_key(page, false, false, row, i, &k); x += k.width + WISP_UI_KEY_GAP; }
    OskKey k;
    view_osk_key(page, false, false, row, col, &k);
    int y = WISP_UI_OSK_Y + 8 + row * (WISP_UI_KEY_H + WISP_UI_KEY_GAP);
    *out = (VRect){ x, y, x + k.width, y + WISP_UI_KEY_H };
    return true;
}

bool view_osk_hit(OskPage page, int px, int py, int *row, int *col)
{
    for (int r = 0; r < 5; r++)
        for (int c = 0; c < view_osk_row_len(page, r); c++) {
            VRect k;
            if (view_osk_rect(page, r, c, &k) && px >= k.x0 && px < k.x1 && py >= k.y0 && py < k.y1) { *row = r; *col = c; return true; }
        }
    return false;
}

int view_osk_col_near(OskPage page, int row, int cx)
{
    int best = 0, bestd = 1 << 30;
    for (int c = 0; c < view_osk_row_len(page, row); c++) {
        VRect k;
        if (!view_osk_rect(page, row, c, &k)) continue;
        int d = abs((k.x0 + k.x1) / 2 - cx);
        if (d < bestd) { bestd = d; best = c; }
    }
    return best;
}

static void osk_key(const Vctx *v, int x, int y, const OskKey *k, bool selected)
{
    const int kh = WISP_UI_KEY_H;
    rd_rrect(v->c, X(v, x), Y(v, y), X(v, x + k->width), Y(v, y + kh), WISP_UI_RADIUS, selected ? FOCUS : k->base);
    RdColor ink = selected ? FOCUS_FG : TEXT;
    if (k->glyph) view_glyph(v, k->glyph, 0, 22, (float)x + (float)k->width / 2.f, (float)y + (float)kh / 2.f, ink);
    else rd_text(v->c, v->f->body, IX(v, x + k->width / 2), IY(v, y + kh / 2 - 1), k->label, ink, RD_CENTER, RD_MIDDLE);
}

void view_osk(const Vctx *v, const OskView *o)
{
    VRect pn = view_osk_panel();
    rd_rrect(v->c, X(v, pn.x0), Y(v, pn.y0), X(v, pn.x1), Y(v, pn.y1), WISP_UI_RADIUS, PANEL);
    for (int r = 0; r < 5; r++) {
        for (int i = 0; i < view_osk_row_len(o->page, r); i++) {
            OskKey k;
            VRect rc;
            view_osk_key(o->page, o->shift, o->ctrl, r, i, &k);
            view_osk_rect(o->page, r, i, &rc);
            osk_key(v, rc.x0, rc.y0, &k, o->row == r && o->col == i);
        }
    }
}

void view_osk_hints(const Vctx *v)
{
    static const Hint h[] = { { "A", "Type" }, { "B", "Delete" }, { "X", "Shift" }, { "Y", "Space" }, { "Start", "Enter" }, { "Select", "Hide" } };
    int x = 12 + 10;
    for (size_t i = 0; i < sizeof h / sizeof *h; i++) x = view_key_pill(v, x, WISP_UI_HINT_Y, h[i].key, h[i].label, PANEL);
}

/* ------------------------------------------------------------------ the toast */
void view_toast_size(const ViewFonts *f, const ToastView *t, int *w, int *h)
{
    int tw = rd_text_width(f->small_bold, t->title), xw = t->text ? rd_text_width(f->tiny, t->text) : 0, hw = 0;
    for (int i = 0; i < t->nhints; i++) {
        int kw = rd_text_width(f->tiny, t->hints[i].key);
        hw += (kw + 12 > 20 ? kw + 12 : 20) + 8 + rd_text_width(f->tiny, t->hints[i].label) + 18;
    }
    int ww = (tw > xw ? tw : xw);
    if (hw > ww) ww = hw;
    ww += 24;
    *w = ww < 240 ? 240 : ww > 420 ? 420 : ww;
    *h = 10 + 22 + (t->text ? 20 : 0) + (t->nhints ? 30 : 0) + 6;
}

void view_toast(const Vctx *v, const ToastView *t)
{
    int w, h;
    view_toast_size(v->f, t, &w, &h);
    rd_rrect(v->c, X(v, 0), Y(v, 0), X(v, w), Y(v, h), WISP_UI_RADIUS, RD_RGBA(WISP_UI_BG, 250));
    rd_rrect_ring(v->c, X(v, 0), Y(v, 0), X(v, w), Y(v, h), WISP_UI_RADIUS, 1.f, LINE);
    int y = 10;
    rd_text_fit(v->c, v->f->small_bold, IX(v, 12), IY(v, y), w - 24, t->title, TEXT, RD_LEFT, RD_TOP);
    y += 22;
    if (t->text) { rd_text_fit(v->c, v->f->tiny, IX(v, 12), IY(v, y), w - 24, t->text, DIM, RD_LEFT, RD_TOP); y += 20; }
    int x = 12;
    for (int i = 0; i < t->nhints; i++) x = view_key_pill(v, x, y + 4, t->hints[i].key, t->hints[i].label, PANEL);
}
