#!/usr/bin/env python3
"""Mockups of the WispOS desktop-mode shell: the wallpaper, the dock, the application menu and the quick settings (docs/WISPOS-UI-LANGUAGE.md "Desktop mode",
docs/WISPOS-OS-DESIGN.md "The dock and the application menu").

  mockup.py --out DIR [--fonts DIR] [--badge PNG]       writes the PNGs listed in SCREENS to DIR (default os/dock/design)

These are DESIGN pictures, not screenshots of a running program (nothing of the dock exists yet): drawn at the panel's 720x480 from the tokens of os/ui/wisp-ui.h and the setup
screen's components, in Liberation Sans, so a reviewer can judge the look before any Wayland code and so the real dock can later be compared against them. The glyph drawings
are placeholders for the icon set (flat shapes in palette colours, drawn on a 24-unit grid); the application list is invented. The settings screens (quick-settings-short, quick-settings-submenu, settings-display, settings-datetime) are the approved proposal of issue #39. Deterministic: two runs give identical files.
Needs Pillow and numpy (the host's)."""
import argparse, re, sys
from pathlib import Path
import numpy as np
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
UI = HERE.parent / 'ui'
sys.path.insert(0, str(UI))
import wallpaper

SS = 4                                   # supersampling of shapes and glyphs; text is drawn at 1x like SDL_ttf
HEADER = (UI / 'wisp-ui.h').read_text()


def token(name):
    m = re.search(r'#define\s+%s\s+(\S+)' % name, HEADER)
    if not m: sys.exit('token %s not found in wisp-ui.h' % name)
    return int(m.group(1), 0)


def colour(name, alpha=255):
    c = token(name)
    return ((c >> 16) & 255, (c >> 8) & 255, c & 255, alpha)


W, H = token('WISP_UI_W'), token('WISP_UI_H')
RADIUS = token('WISP_UI_RADIUS')
BG, PANEL, LINE, FIELD = colour('WISP_UI_BG'), colour('WISP_UI_PANEL'), colour('WISP_UI_LINE'), colour('WISP_UI_FIELD')
TEXT, DIM, DIM2, GLYPH = colour('WISP_UI_TEXT'), colour('WISP_UI_DIM'), colour('WISP_UI_DIM2'), colour('WISP_UI_GLYPH')
ACCENT, OK, BAD = colour('WISP_UI_ACCENT'), colour('WISP_UI_OK'), colour('WISP_UI_BAD')
FOCUS, FOCUS_FG, FOCUS_DIM, FOCUS_OK = colour('WISP_UI_FOCUS'), colour('WISP_UI_FOCUS_FG'), colour('WISP_UI_FOCUS_DIM'), colour('WISP_UI_FOCUS_OK')
DOCK_H = token('WISP_UI_DOCK_H')
DOCK_TOP = H - DOCK_H


class Fonts:
    def __init__(self, directory):
        d = Path(directory)
        load = lambda f, size: ImageFont.truetype(str(d / f), size, layout_engine=ImageFont.Layout.BASIC)
        self.title = load('LiberationSans-Bold.ttf', token('WISP_UI_SIZE_TITLE'))
        self.body = load('LiberationSans-Regular.ttf', token('WISP_UI_SIZE_BODY'))
        self.small = load('LiberationSans-Regular.ttf', token('WISP_UI_SIZE_SMALL'))
        self.tiny = load('LiberationSans-Regular.ttf', token('WISP_UI_SIZE_TINY'))
        self.small_bold = load('LiberationSans-Bold.ttf', token('WISP_UI_SIZE_SMALL'))
        self.mono = load('LiberationMono-Regular.ttf', 16)
        self.mono_bold = load('LiberationMono-Bold.ttf', 16)


def _rr(d, box, radius=0, fill=None, outline=None, width=0):
    """Pillow's rounded_rectangle: a radius of about half the shorter side is a full round (a pill, or a disc on a square); anything larger bends the ends into an ellipse (an egg), so it is clamped to exactly half.
    Keep this in step with the shell's renderer: view.c draws pills with rd_pill (render.c), whose radius is half the shorter side too. os/test_dock_view.py compares the two pixel by pixel, so a change on one side only shows up there."""
    x0, y0, x1, y1 = box
    side = min(x1 - x0, y1 - y0)
    if radius < 0.5: d.rectangle((x0, y0, x1, y1), fill=fill, outline=outline, width=width)
    else: d.rounded_rectangle(box, radius=side / 2 if 2 * radius >= side - 3 else radius, fill=fill, outline=outline, width=width)      # about half a side = fully round


class Canvas:
    """An RGBA picture with anti-aliased shape primitives (drawn at SS times the size, scaled down) and plain text."""
    def __init__(self, base, fonts):
        self.img = base.convert('RGBA'); self.f = fonts

    # ------------------------------------------------------------------ shapes
    def _patch(self, box, margin=2):
        x0, y0, x1, y1 = box
        ox, oy = int(np.floor(x0)) - margin, int(np.floor(y0)) - margin
        w, h = int(np.ceil(x1)) + margin - ox, int(np.ceil(y1)) + margin - oy
        return ox, oy, w, h, Image.new('RGBA', (w * SS, h * SS), (0, 0, 0, 0))

    def _put(self, ox, oy, w, h, patch):
        self.img.alpha_composite(patch.resize((w, h), Image.LANCZOS), (ox, oy))

    def rrect(self, box, r, fill=None, outline=None, ow=1):
        x0, y0, x1, y1 = box
        ox, oy, w, h, p = self._patch(box)
        d = ImageDraw.Draw(p)
        b = [(x0 - ox) * SS, (y0 - oy) * SS, (x1 - ox) * SS - 1, (y1 - oy) * SS - 1]
        _rr(d, b, r * SS, fill=fill, outline=outline, width=int(ow * SS) if outline else 0)
        self._put(ox, oy, w, h, p)

    def pill(self, box, fill=None, outline=None, ow=1):
        self.rrect(box, (box[3] - box[1]) / 2, fill, outline, ow)

    def disc(self, cx, cy, r, fill):
        self.rrect((cx - r, cy - r, cx + r, cy + r), r, fill)

    def hline(self, x0, x1, y, colour_, th=1):
        self.rrect((x0, y, x1, y + th), 0, colour_)

    def vline(self, x, y0, y1, colour_, th=1):
        self.rrect((x, y0, x + th, y1), 0, colour_)

    # ------------------------------------------------------------------ text
    def text(self, xy, s, font, fill, anchor='la'):
        ImageDraw.Draw(self.img, 'RGBA').text(xy, s, font=font, fill=fill, anchor=anchor)

    def width(self, s, font):
        return int(round(font.getlength(s)))

    # ------------------------------------------------------------------ glyphs
    def icon(self, name, cx, cy, size, fill, **kw):
        """A glyph of the placeholder set, centred on (cx, cy), `size` px square, in one colour."""
        x0, y0 = int(round(cx - size / 2)), int(round(cy - size / 2))
        mask = Image.new('L', (size * SS, size * SS), 0)
        GLYPHS[name](ImageDraw.Draw(mask), size * SS / 24.0, **kw)
        mask = mask.resize((size, size), Image.LANCZOS)
        layer = Image.new('RGBA', (size, size), fill[:3] + (0,))
        a = np.asarray(mask, np.float32) * (fill[3] / 255.0)
        layer.putalpha(Image.fromarray(a.astype(np.uint8), 'L'))
        self.img.alpha_composite(layer, (x0, y0))

    def key_pill(self, x, y, key, label, on=None):
        """The setup screen's hint: a round pill with the key, then the label; returns the x after it."""
        kw = self.width(key, self.f.tiny); pill = max(20, kw + 12)
        self.pill((x, y, x + pill, y + 20), PANEL)
        self.text((x + pill / 2, y + 2), key, self.f.tiny, GLYPH, 'ma')
        self.text((x + pill + 8, y + 2), label, self.f.tiny, DIM)
        return x + pill + 8 + self.width(label, self.f.tiny) + 18


# ---------------------------------------------------------------------------------------------------------------------------- placeholder glyphs (24-unit grid)
def _round_line(d, u, pts, width):
    d.line([(x * u, y * u) for x, y in pts], fill=255, width=int(round(width * u)), joint='curve')
    for x, y in (pts[0], pts[-1]):
        r = width * u / 2; d.ellipse((x * u - r, y * u - r, x * u + r, y * u + r), fill=255)


def g_apps(d, u):
    for cx in (5, 12, 19):
        for cy in (5, 12, 19):
            d.ellipse(((cx - 2.1) * u, (cy - 2.1) * u, (cx + 2.1) * u, (cy + 2.1) * u), fill=255)


def g_browser(d, u):
    d.ellipse((2.6 * u, 2.6 * u, 21.4 * u, 21.4 * u), outline=255, width=int(1.9 * u))
    d.ellipse((7.9 * u, 2.6 * u, 16.1 * u, 21.4 * u), outline=255, width=int(1.6 * u))
    _round_line(d, u, [(3, 12), (21, 12)], 1.6)
    _round_line(d, u, [(4.6, 7.2), (19.4, 7.2)], 1.4); _round_line(d, u, [(4.6, 16.8), (19.4, 16.8)], 1.4)


def g_terminal(d, u):
    _round_line(d, u, [(4.5, 7), (11, 12), (4.5, 17)], 2.7)
    _round_line(d, u, [(13, 18), (20, 18)], 2.7)


def g_settings(d, u):
    import math
    pts = []
    for k in range(8):                                   # eight teeth: a root, a short plateau, a plateau, a root
        for dt, r in ((-0.36, 7.9), (-0.20, 10.7), (0.20, 10.7), (0.36, 7.9)):
            a = k * math.pi / 4 + dt
            pts.append(((12 + r * math.cos(a)) * u, (12 + r * math.sin(a)) * u))
    d.polygon(pts, fill=255)
    d.ellipse((3.9 * u, 3.9 * u, 20.1 * u, 20.1 * u), fill=255)
    d.ellipse((8.3 * u, 8.3 * u, 15.7 * u, 15.7 * u), fill=0)


def g_game(d, u):
    r = 4.9 * u                                                                             # a capsule: two end discs and the rectangle between them
    d.ellipse((2.2 * u, 6.6 * u, 2.2 * u + 2 * r, 6.6 * u + 2 * r), fill=255); d.ellipse((21.8 * u - 2 * r, 6.6 * u, 21.8 * u, 6.6 * u + 2 * r), fill=255)
    d.rectangle((2.2 * u + r, 6.6 * u, 21.8 * u - r, 16.4 * u), fill=255)
    d.ellipse((2.6 * u, 10.5 * u, 9.4 * u, 20.6 * u), fill=255); d.ellipse((14.6 * u, 10.5 * u, 21.4 * u, 20.6 * u), fill=255)
    d.rectangle((5.0 * u, 10.9 * u, 10.6 * u, 12.9 * u), fill=0); d.rectangle((6.8 * u, 9.1 * u, 8.8 * u, 14.7 * u), fill=0)       # d-pad
    d.ellipse((15.0 * u, 9.4 * u, 17.2 * u, 11.6 * u), fill=0); d.ellipse((17.7 * u, 11.9 * u, 19.9 * u, 14.1 * u), fill=0)         # buttons


def g_images(d, u):
    _rr(d, (3 * u, 4.2 * u, 21 * u, 19.8 * u), 2 * u, outline=255, width=int(1.9 * u))
    d.ellipse((6.6 * u, 7.4 * u, 10.4 * u, 11.2 * u), fill=255)
    d.polygon([(u * x, u * y) for x, y in ((4.8, 18.4), (10.2, 12.8), (13.6, 16.2), (16.2, 13.6), (19.4, 17.6), (19.4, 18.4))], fill=255)


def g_monitor(d, u):
    _rr(d, (2.6 * u, 4 * u, 21.4 * u, 17.2 * u), 2 * u, outline=255, width=int(1.9 * u))
    _round_line(d, u, [(4.6, 13), (8.2, 9.4), (11, 12.2), (15, 7.8), (19.2, 11.2)], 1.6)
    _round_line(d, u, [(8, 20.4), (16, 20.4)], 1.9)


def g_about(d, u):
    d.ellipse((2.6 * u, 2.6 * u, 21.4 * u, 21.4 * u), outline=255, width=int(1.9 * u))
    d.ellipse((10.7 * u, 6.2 * u, 13.3 * u, 8.8 * u), fill=255); _rr(d, (10.8 * u, 10.4 * u, 13.2 * u, 17.8 * u), 1.1 * u, fill=255)


def g_wifi(d, u, level=3):
    for i, r in enumerate((4.6, 8.8, 13.0)):
        if i < level:
            d.arc(((12 - r) * u, (19.6 - r) * u, (12 + r) * u, (19.6 + r) * u), 225, 315, fill=255, width=int(2.1 * u))
    d.ellipse((10.3 * u, 17.9 * u, 13.7 * u, 21.3 * u), fill=255)


def g_volume(d, u, level=2):
    d.polygon([(u * x, u * y) for x, y in ((3, 9.2), (7.2, 9.2), (12.4, 4.6), (12.4, 19.4), (7.2, 14.8), (3, 14.8))], fill=255)
    for i, r in enumerate((5.4, 9.0)):
        if i < level:
            d.arc(((12.4 - r) * u, (12 - r) * u, (12.4 + r) * u, (12 + r) * u), -48, 48, fill=255, width=int(1.9 * u))


def g_battery(d, u, level=0.78):
    _rr(d, (2.2 * u, 6.6 * u, 20.2 * u, 17.4 * u), 1.8 * u, outline=255, width=int(1.8 * u))
    d.rectangle((20.9 * u, 10 * u, 22.4 * u, 14 * u), fill=255)
    d.rectangle((4.6 * u, 9 * u, (4.6 + 13.2 * level) * u, 15 * u), fill=255)


def g_brightness(d, u):
    import math
    d.ellipse((7.6 * u, 7.6 * u, 16.4 * u, 16.4 * u), fill=255)
    for i in range(8):
        a = i * math.pi / 4
        _round_line(d, u, [(12 + 7.6 * math.cos(a), 12 + 7.6 * math.sin(a)), (12 + 10.4 * math.cos(a), 12 + 10.4 * math.sin(a))], 1.7)


def g_bluetooth(d, u):
    _round_line(d, u, [(6.6, 8), (17, 16), (12, 20.4), (12, 3.6), (17, 8), (6.6, 16)], 1.9)


def g_usb(d, u):
    _rr(d, (7.2 * u, 10.2 * u, 16.8 * u, 20.6 * u), 1.6 * u, fill=255)
    d.rectangle((8.6 * u, 3.4 * u, 10.8 * u, 10.2 * u), fill=255); d.rectangle((13.2 * u, 3.4 * u, 15.4 * u, 10.2 * u), fill=255)


def g_ssh(d, u):
    _rr(d, (2.6 * u, 4.4 * u, 21.4 * u, 19.6 * u), 2 * u, outline=255, width=int(1.9 * u))
    _round_line(d, u, [(6.4, 9.2), (9.8, 12), (6.4, 14.8)], 1.8); _round_line(d, u, [(11.8, 15.2), (16.6, 15.2)], 1.8)


def g_pin(d, u):
    d.ellipse((7 * u, 3 * u, 17 * u, 13 * u), fill=255)
    d.polygon([(u * x, u * y) for x, y in ((10, 11.5), (14, 11.5), (12, 21))], fill=255)


def g_left(d, u):
    _round_line(d, u, [(14.5, 5.5), (8, 12), (14.5, 18.5)], 2.4)


def g_right(d, u):
    _round_line(d, u, [(9.5, 5.5), (16, 12), (9.5, 18.5)], 2.4)


def g_up(d, u):
    _round_line(d, u, [(5.5, 14.5), (12, 8), (18.5, 14.5)], 2.4)


def g_down(d, u):
    _round_line(d, u, [(5.5, 9.5), (12, 16), (18.5, 9.5)], 2.4)


def g_close(d, u):
    _round_line(d, u, [(6, 6), (18, 18)], 2.2); _round_line(d, u, [(18, 6), (6, 18)], 2.2)


def g_power(d, u):
    d.arc((3.6 * u, 4.6 * u, 20.4 * u, 21.4 * u), start=300, end=240, fill=255, width=int(round(1.9 * u)))
    _round_line(d, u, [(12, 3), (12, 11.6)], 2.0)


def g_restart(d, u):
    d.arc((4 * u, 4.4 * u, 20 * u, 20.4 * u), start=40, end=335, fill=255, width=int(round(1.9 * u)))
    d.polygon([(21.6 * u, 3.6 * u), (21.6 * u, 10.6 * u), (14.8 * u, 7.6 * u)], fill=255)


def g_sleep(d, u):
    d.ellipse((3.4 * u, 3.4 * u, 20.6 * u, 20.6 * u), fill=255)
    d.ellipse((9.6 * u, 0.6 * u, 25.6 * u, 16.6 * u), fill=0)


GLYPHS = {'apps': g_apps, 'browser': g_browser, 'terminal': g_terminal, 'settings': g_settings, 'game': g_game, 'images': g_images, 'monitor': g_monitor, 'about': g_about,
          'wifi': g_wifi, 'down': g_down, 'up': g_up, 'volume': g_volume, 'battery': g_battery, 'brightness': g_brightness, 'bluetooth': g_bluetooth, 'usb': g_usb, 'ssh': g_ssh, 'pin': g_pin, 'close': g_close, 'power': g_power, 'restart': g_restart, 'sleep': g_sleep, 'left': g_left, 'right': g_right}

# the application list of the mockups: name, glyph, glyph colour (palette only)
APPS = {'browser': ('Browser', ACCENT), 'terminal': ('Terminal', OK), 'settings': ('Settings', GLYPH), 'game': ('Game mode', ACCENT),
        'images': ('Images', GLYPH), 'monitor': ('Monitor', GLYPH), 'about': ('About', GLYPH)}


# ---------------------------------------------------------------------------------------------------------------------------- the pieces
def cursor(c, x, y):
    pts = [(0, 0), (0, 17), (4.2, 13.2), (7, 19.6), (9.6, 18.5), (6.8, 12.4), (12.2, 12.4)]
    ox, oy, w, h, p = c._patch((x, y, x + 14, y + 22))
    d = ImageDraw.Draw(p); poly = [((x + a - ox) * SS, (y + b - oy) * SS) for a, b in pts]
    d.polygon(poly, fill=(0, 0, 0, 255)); d.line(poly + [poly[0]], fill=(0, 0, 0, 255), width=int(2.2 * SS), joint='curve')
    inner = [((x + 1.6 + a * 0.84 - ox) * SS, (y + 2.4 + b * 0.84 - oy) * SS) for a, b in pts]
    d.polygon([((x + 0.9 + a * 0.86 - ox) * SS, (y + 1.6 + b * 0.86 - oy) * SS) for a, b in pts], fill=(255, 255, 255, 255))
    c._put(ox, oy, w, h, p)


def draw_dock(c, pins, running=(), active=None, focus=None, summoned=False, clock='12:34'):
    """The strip. pins: app ids in order; running: ids with a window; active: the id in front; focus: the id ('apps', 'game', 'status' or an app) with the white tile.
    summoned: the strip is the thing being driven (an accent line along its top edge). Returns the x of each item."""
    tile, pitch, inset, icon = token('WISP_UI_DOCK_TILE'), token('WISP_UI_DOCK_PITCH'), token('WISP_UI_DOCK_INSET'), token('WISP_UI_DOCK_ICON')
    c.img.alpha_composite(Image.new('RGBA', (W, DOCK_H), PANEL[:3] + (token('WISP_UI_DOCK_ALPHA'),)), (0, DOCK_TOP))
    c.hline(0, W, DOCK_TOP, ACCENT if summoned else LINE, 2 if summoned else 1)
    ty = DOCK_TOP + (DOCK_H - tile) // 2 + 1
    mid = DOCK_TOP + DOCK_H // 2 + 1
    positions = {}

    def item(key, x, glyph, glyph_colour):
        focused = focus == key
        c.rrect((x, ty, x + tile, ty + tile), RADIUS, FOCUS if focused else BG)
        c.icon(glyph, x + tile / 2, ty + tile / 2 - (2 if key in running else 0), icon, FOCUS_FG if focused else glyph_colour)
        if key in running:
            my = ty + tile - 6
            if key == active:
                half = token('WISP_UI_DOCK_RUN_BAR') / 2
                c.pill((x + tile / 2 - half, my, x + tile / 2 + half, my + token('WISP_UI_DOCK_RUN_H')), FOCUS_OK if focused else ACCENT)
            else:
                c.disc(x + tile / 2, my + 1.5, token('WISP_UI_DOCK_RUN_DOT') / 2, FOCUS_DIM if focused else DIM)
        positions[key] = x

    def separator(x): c.vline(x, DOCK_TOP + 11, DOCK_TOP + DOCK_H - 9, LINE)

    x = inset
    item('apps', x, 'apps', TEXT); x += pitch
    separator(x - 4); x += 4
    for app in pins:
        item(app, x, app, APPS[app][1]); x += pitch
    others = [a for a in running if a not in pins]
    if others:
        separator(x - 4); x += 4
        for app in others:
            item(app, x, app, APPS[app][1]); x += pitch
    # the right end: the status group (one focusable item that opens the quick settings), a divider, the way back to Game mode
    st = token('WISP_UI_DOCK_STATUS'); focused = focus == 'status'
    clock_w = c.width(clock, c.f.small); group_w = 10 + 3 * st + 2 * 8 + 12 + clock_w + 10
    gx1 = W - inset + 2; gx0 = gx1 - group_w
    if focused: c.rrect((gx0, ty, gx1, ty + tile), RADIUS, FOCUS)
    ink = FOCUS_FG if focused else GLYPH
    gx = gx0 + 10
    for glyph, kw in (('wifi', {}), ('volume', {}), ('battery', {'level': 0.78})):
        c.icon(glyph, gx + st / 2, mid, st, ink, **kw); gx += st + 8
    c.text((gx1 - 10, mid), clock, c.f.small, FOCUS_FG if focused else TEXT, 'rm')
    positions['status'] = gx0
    separator(gx0 - 7)
    item('game', gx0 - 7 - 5 - tile, 'game', ACCENT)
    return positions


def focus_bar(c, name, hints):
    """Shown above the strip while it is summoned: the focused item's name at the left, the buttons at the right."""
    bh = token('WISP_UI_DOCK_BAR')
    c.img.alpha_composite(Image.new('RGBA', (W, bh), PANEL[:3] + (token('WISP_UI_DOCK_ALPHA'),)), (0, DOCK_TOP - bh))
    c.text((12, DOCK_TOP - bh / 2), name, c.f.small_bold, TEXT, 'lm')
    parts, total = [], 0
    for key, label in hints:
        pill = max(20, c.width(key, c.f.tiny) + 12); adv = pill + 8 + c.width(label, c.f.tiny) + 14
        parts.append((key, label, pill, adv)); total += adv
    x = W - 12 - total + 14
    for key, label, pill, adv in parts:
        y = DOCK_TOP - bh / 2 - 10
        c.pill((x, y, x + pill, y + 20), BG)
        c.text((x + pill / 2, y + 2), key, c.f.tiny, GLYPH, 'ma')
        c.text((x + pill + 8, y + 2), label, c.f.tiny, DIM)
        x += adv


ROWS_LOWER = ('1234567890', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm')
ROWS_UPPER = ('1234567890', 'QWERTYUIOP', 'ASDFGHJKL', 'ZXCVBNM')
ROWS_SYM = ('1234567890', '!@#$%^&*()', '-_=+[]{};:', '\'",.<>/?\\|')


def key(c, x, y, w, label, selected=False, base=None, glyph=None):
    """The setup screen's key: a surface in the ground colour (black for the special ones, the accent for an active one), white when focused."""
    kh = token('WISP_UI_KEY_H')
    c.rrect((x, y, x + w, y + kh), RADIUS, FOCUS if selected else (base or BG))
    ink = FOCUS_FG if selected else TEXT
    if glyph: c.icon(glyph, x + w / 2, y + kh / 2, 22, ink)
    else: c.text((x + w / 2, y + kh / 2 - 1), label, c.f.body, ink, 'mm')


def draw_osk(c, focus=(2, 5), page='lower', shift=False, ctrl=False):
    """The desktop keyboard, laid out as the setup screen's: five rows of 40 px keys 4 px apart on a surface, the focused key white. Full width, above the hints; the dock is hidden
    while it is open. focus = (row, column); page = 'lower' | 'upper' | 'symbols'. Types into whatever has the keyboard focus (no field of its own)."""
    kw, kh, gap = token('WISP_UI_KEY_W'), token('WISP_UI_KEY_H'), token('WISP_UI_KEY_GAP')
    py = token('WISP_UI_OSK_Y'); ph = 4 * (kh + gap) + kh + 24
    c.rrect((12, py, W - 12, py + ph), RADIUS, PANEL)
    rows = {'lower': ROWS_LOWER, 'upper': ROWS_UPPER, 'symbols': ROWS_SYM}[page]
    ky = py + 8
    for r in range(4):
        n = len(rows[r]); del_w = 92; with_del = r == 3 and page != 'symbols'          # on the symbols page `del` moves to the bottom row: ten symbols fill a row
        total = n * (kw + gap) - gap + (del_w + gap if with_del else 0)
        x = (W - total) // 2
        for i, ch in enumerate(rows[r]):
            key(c, x + i * (kw + gap), ky + r * (kh + gap), kw, ch, focus == (r, i))
        if with_del: key(c, x + n * (kw + gap), ky + r * (kh + gap), del_w, 'del', focus == (3, n), FIELD)
    sy = ky + 4 * (kh + gap)
    if page == 'symbols':
        specials = [('abc', 70, None, FIELD), ('`', 52, None, FIELD), ('~', 52, None, FIELD), ('', 52, 'left', FIELD), ('', 52, 'down', FIELD), ('', 52, 'up', FIELD), ('', 52, 'right', FIELD),
                    ('space', 110, None, FIELD), ('del', 64, None, FIELD), ('enter', 90, None, ACCENT)]
    else:
        specials = [('123', 70, None, FIELD), ('shift', 78, None, ACCENT if shift or page == 'upper' else FIELD), ('ctrl', 62, None, ACCENT if ctrl else FIELD),
                    ('space', 178, None, FIELD), ('tab', 62, None, FIELD), ('esc', 62, None, FIELD), ('enter', 100, None, ACCENT)]
    total = sum(w for _, w, _, _ in specials) + gap * (len(specials) - 1)
    x = (W - total) // 2
    for i, (label, w, glyph, base) in enumerate(specials):
        key(c, x, sy, w, label, focus == (4, i), base, glyph); x += w + gap


def osk_hints(c):
    x = 12 + 10
    for key_, label in (('A', 'Type'), ('B', 'Delete'), ('X', 'Shift'), ('Y', 'Space'), ('Start', 'Enter'), ('Select', 'Hide')):
        x = c.key_pill(x, token('WISP_UI_HINT_Y'), key_, label)


def popup(c, x, rows, selected, width=214):
    """A list above an item: rows of WISP_UI_POPUP_ROW, the selected one white."""
    rh = token('WISP_UI_POPUP_ROW'); h = len(rows) * rh + 12
    x0 = min(max(x, 6), W - width - 6); y1 = DOCK_TOP - token('WISP_UI_DOCK_BAR') - 6; y0 = y1 - h
    c.rrect((x0, y0, x0 + width, y1), RADIUS, BG[:3] + (250,), LINE)
    for i, (glyph, label) in enumerate(rows):
        ry = y0 + 6 + i * rh
        sel = i == selected
        if sel: c.rrect((x0 + 6, ry, x0 + width - 6, ry + rh - 2), RADIUS, FOCUS)
        c.icon(glyph, x0 + 26, ry + rh / 2 - 1, 18, FOCUS_FG if sel else GLYPH)
        c.text((x0 + 48, ry + rh / 2 - 1), label, c.f.small, FOCUS_FG if sel else TEXT, 'lm')


def terminal_window(c, height=None, typed=None):
    """A foot window filling the area above the dock (or above the keyboard), with real command output; when it does not fit the last lines are shown, as a terminal does."""
    height = DOCK_TOP if height is None else height
    c.img.paste(BG, (0, 0, W, height))
    prompt = [('wisp@WispOS', OK), (':', TEXT), ('~', ACCENT), ('$ ', TEXT)]
    lines = [prompt + [('uname -sr', TEXT)], [('Linux 7.2.0', TEXT)], prompt + [('wisp-usb status', TEXT)],
             [('usb: off', TEXT)], [('host: available', TEXT)], [('computer: available', TEXT)],
             prompt + [('free -m', TEXT)], [('               total        used        free   available', DIM)],
             [('Mem:             987         512         118         396', TEXT)], [('Swap:            512         140         372', TEXT)],
             prompt + ([(typed, TEXT)] if typed else [])]
    fit = (height - 14) // 21
    lines = lines[-fit:]
    y = 10
    for line in lines:
        x = 12
        for s_, col in line:
            c.text((x, y), s_, c.f.mono, col); x += c.width(s_, c.f.mono)
        last_x, last_y = x, y
        y += 21
    c.img.paste(TEXT, (last_x, last_y + 2, last_x + 9, last_y + 19))


def desktop_base(fonts, wall):
    return Canvas(wall, fonts)


# ---------------------------------------------------------------------------------------------------------------------------- the screens
PINS = ['browser', 'terminal', 'settings']


def screen_empty(f, wall):
    c = desktop_base(f, wall); draw_dock(c, PINS); cursor(c, 440, 262); return c


def screen_window(f, wall):
    c = desktop_base(f, wall); terminal_window(c); draw_dock(c, PINS, running=['terminal', 'images'], active='terminal'); return c


def screen_focus(f, wall):
    c = desktop_base(f, wall); terminal_window(c)
    draw_dock(c, PINS, running=['terminal', 'images'], active='terminal', focus='settings', summoned=True)
    focus_bar(c, 'Settings', [('A', 'Open'), ('X', 'Unpin'), ('Y', 'More'), ('B', 'Back')])
    return c


def screen_more(f, wall):
    c = desktop_base(f, wall); terminal_window(c)
    pos = draw_dock(c, PINS, running=['terminal', 'images'], active='terminal', focus='terminal', summoned=True)
    focus_bar(c, 'Terminal', [('A', 'Select'), ('B', 'Back')])
    popup(c, pos['terminal'] - 10, [('terminal', 'Open'), ('pin', 'Unpin from dock'), ('left', 'Move left'), ('right', 'Move right'), ('close', 'Close window')], 1)
    return c


def screen_menu(f, wall, menu_selected='terminal', hints=(('A', 'Open'), ('X', 'Unpin'), ('Y', 'Search'), ('B', 'Back'))):
    c = desktop_base(f, wall); terminal_window(c)
    c.img.alpha_composite(Image.new('RGBA', (W, H), BG[:3] + (token('WISP_UI_MENU_SCRIM'),)))
    cols, cw, ch, tile, icon = token('WISP_UI_MENU_COLS'), token('WISP_UI_MENU_CELL_W'), token('WISP_UI_MENU_CELL_H'), token('WISP_UI_MENU_TILE'), token('WISP_UI_MENU_ICON')
    left = token('WISP_UI_MENU_X') + (cw - tile) // 2                  # the first tile's left edge: the title, subtitle and hints line up with it
    c.text((left, token('WISP_UI_TITLE_Y')), 'Applications', f.title, TEXT)
    c.text((left, token('WISP_UI_SUBTITLE_Y')), 'Pick an app. X pins it to the dock.', f.small, DIM)
    order = ['browser', 'terminal', 'settings', 'game', 'images', 'monitor', 'about']
    selected = menu_selected
    for i, app in enumerate(order):
        cx = token('WISP_UI_MENU_X') + (i % cols) * cw + cw // 2
        top = token('WISP_UI_MENU_Y') + (i // cols) * ch
        sel = app == selected
        c.rrect((cx - tile / 2, top, cx + tile / 2, top + tile), RADIUS, FOCUS if sel else PANEL)
        c.icon(app, cx, top + tile / 2, icon, FOCUS_FG if sel else APPS[app][1])
        if app in PINS: c.icon('pin', cx + tile / 2 - 11, top + 11, 14, FOCUS_DIM if sel else DIM)          # pinned to the dock
        c.text((cx, top + tile + 16), APPS[app][0], f.small, TEXT, 'mm')
    x = left
    for key, label in hints:
        x = c.key_pill(x, token('WISP_UI_HINT_Y'), key, label)
    return c


def screen_osk(f, wall):
    c = desktop_base(f, wall); terminal_window(c, token('WISP_UI_OSK_Y'), typed='wisp-usb sta')
    draw_osk(c, focus=(2, 4)); osk_hints(c); return c


def screen_osk_symbols(f, wall):
    c = desktop_base(f, wall); terminal_window(c, token('WISP_UI_OSK_Y'), typed='ls -la /storage/')
    draw_osk(c, focus=(3, 6), page='symbols'); osk_hints(c); return c


# ---------------------------------------------------------------------------------------------------------------------------- settings proposal: a short quick settings with submenus, and a dedicated settings app
def qs_panel(c, rows_h, foot=34):
    """The quick-settings panel box for a list of rows whose heights add up to rows_h; a submenu is drawn with the list's rows_h, so nothing moves when it opens: returns (x0, y0, x1, y1)."""
    pw = token('WISP_UI_QS_W'); h = rows_h + 12 + foot + 6
    x0, y1 = W - pw - 8, DOCK_TOP - 8
    c.rrect((x0, y1 - h, x0 + pw, y1), RADIUS, BG[:3] + (250,), LINE)
    return x0, y1 - h, x0 + pw, y1


def qs_row(c, x0, x1, y, hh, glyph, label, value=None, sel=False, chevron=False, toggle=None, slider=None, two=False, **kw):
    f = c.f; fg, dim = (FOCUS_FG, FOCUS_DIM) if sel else (TEXT, DIM)
    c.rrect((x0 + 10, y, x1 - 10, y + hh - 6), RADIUS, FOCUS if sel else PANEL)
    mid = y + (hh - 6) // 2
    c.icon(glyph, x0 + 32, mid, 18, FOCUS_FG if sel else GLYPH, **kw)
    if slider is not None:
        tx0, tx1 = x0 + 62, x1 - 28
        c.pill((tx0, mid - 2, tx1, mid + 2), FOCUS_DIM if sel else LINE)
        c.pill((tx0, mid - 2, tx0 + (tx1 - tx0) * slider, mid + 2), FOCUS_FG if sel else ACCENT); c.disc(tx0 + (tx1 - tx0) * slider, mid, 6, FOCUS_FG if sel else TEXT)
        return
    c.text((x0 + 62, mid - (8 if two else 0)), label, f.small, fg, 'lm')
    if two: c.text((x0 + 62, mid + 11), value, f.tiny, dim, 'lm')
    elif value and chevron: c.text((x1 - 40, mid), value, f.tiny, dim, 'rm')
    elif value: c.text((x1 - 22, mid), value, f.tiny, dim, 'rm')
    if chevron: c.icon('right', x1 - 28, mid, 18, fg if sel else DIM)
    if toggle is not None:
        sx = x1 - 28 - 40
        c.pill((sx, mid - 11, sx + 40, mid + 11), (FOCUS_OK if sel else OK) if toggle else LINE)
        c.disc(sx + (29 if toggle else 11), mid, 8, FOCUS if (sel and toggle) else TEXT)


def qs_footer(c, x0, y1, hints, power=None):
    """The footer's hints at the left; with `power` (None, 'idle' or 'focus') the small power button at the right: A on it opens the Power submenu."""
    hx = x0 + 14
    for key, label in hints: hx = c.key_pill(hx, y1 - 34 + 4, key, label)
    if power:
        pw = token('WISP_UI_QS_W'); bx1 = x0 + pw - 12; by0 = y1 - 31
        foc = power == 'focus'
        c.rrect((bx1 - 40, by0, bx1, by0 + 26), RADIUS, FOCUS if foc else PANEL)
        c.icon('power', bx1 - 20, by0 + 13, 18, FOCUS_FG if foc else GLYPH)


QS_RH = token('WISP_UI_POPUP_ROW') + 6


def qs_list_height():
    return QS_RH * 6 + QS_RH + 10                       # six single rows and the SSH row, which has a second line


def screen_quick(f, wall, sel_row=5, power='idle'):
    """Quick settings reduced to what is touched often; rows that lead somewhere (Wi-Fi, USB port) carry a chevron, and USB port opens a submenu in place."""
    c = desktop_base(f, wall); terminal_window(c); draw_dock(c, PINS, running=['terminal', 'images'], active='terminal', focus='status')
    x0, y0, x1, y1 = qs_panel(c, qs_list_height())
    y = y0 + 12
    rows = [(QS_RH, dict(glyph='wifi', label='Wi-Fi', value='Home-WiFi-5G', chevron=True, level=3)), (QS_RH, dict(glyph='bluetooth', label='Bluetooth', toggle=False)),
            (QS_RH, dict(glyph='volume', label='Volume', slider=0.62, level=2)), (QS_RH, dict(glyph='brightness', label='Brightness', slider=0.40)),
            (QS_RH + 10, dict(glyph='ssh', label='SSH', value='wisp@192.0.2.17', toggle=True, two=True)), (QS_RH, dict(glyph='usb', label='USB port', value='Off', chevron=True)),
            (QS_RH, dict(glyph='battery', label='Battery', value='78 %  discharging', level=0.78))]
    for i, (hh, r) in enumerate(rows):
        qs_row(c, x0, x1, y, hh, sel=i == sel_row, **r); y += hh
    qs_footer(c, x0, y1, (('A', 'Change'), ('B', 'Close')), power=power)
    return c


def screen_quick_power_button(f, wall):
    """The list with the pad on the small power button (Down from the USB port row skips the battery row and lands here); A opens the Power submenu."""
    return screen_quick(f, wall, sel_row=None, power='focus')


def screen_quick_submenu(f, wall):
    """The same panel after A on 'USB port': same size, same place, the content replaced; B (or the back row) returns to the list."""
    c = desktop_base(f, wall); terminal_window(c); draw_dock(c, PINS, running=['terminal', 'images'], active='terminal', focus='status')
    x0, y0, x1, y1 = qs_panel(c, qs_list_height())
    y = y0 + 12
    c.rrect((x0 + 10, y, x1 - 10, y + QS_RH - 6), RADIUS, PANEL)
    c.icon('left', x0 + 28, y + (QS_RH - 6) // 2, 18, GLYPH); c.text((x0 + 50, y + (QS_RH - 6) // 2), 'USB port', f.small_bold, TEXT, 'lm'); y += QS_RH
    ch = QS_RH + QS_RH // 2
    for i, (label, sub, on) in enumerate((('Off', 'port unpowered, no data', True), ('Computer', 'the handheld is a device', False), ('Host', 'powers a connected device', False))):
        sel = i == 0
        fg, dim = (FOCUS_FG, FOCUS_DIM) if sel else (TEXT, DIM)
        c.rrect((x0 + 10, y, x1 - 10, y + ch - 6), RADIUS, FOCUS if sel else PANEL)
        mid = y + (ch - 6) // 2                          # the shell works in whole pixels
        c.text((x0 + 36, mid - 8), label, f.small, fg, 'lm'); c.text((x0 + 36, mid + 11), sub, f.tiny, dim, 'lm')
        c.disc(x1 - 34, mid, 8, (FOCUS_FG if sel else ACCENT) if on else BG)
        y += ch
    c.text((x0 + 18, y + 6), 'Off again at every boot.', f.tiny, DIM, 'lm')
    qs_footer(c, x0, y1, (('A', 'Choose'), ('B', 'Back')))
    return c


def screen_quick_power(f, wall, confirm=False):
    """The same panel after A on the small power button: same size, same place, the content replaced by Sleep, Restart and Shut down; Restart and Shut down ask first; B (or the back row) returns to the list."""
    c = desktop_base(f, wall); terminal_window(c); draw_dock(c, PINS, running=['terminal', 'images'], active='terminal', focus='status')
    x0, y0, x1, y1 = qs_panel(c, qs_list_height())
    y = y0 + 12
    c.rrect((x0 + 10, y, x1 - 10, y + QS_RH - 6), RADIUS, PANEL)
    c.icon('left', x0 + 28, y + (QS_RH - 6) // 2, 18, GLYPH); c.text((x0 + 50, y + (QS_RH - 6) // 2), 'Power', f.small_bold, TEXT, 'lm'); y += QS_RH
    ch = QS_RH + QS_RH // 2
    for i, (glyph, label, sub) in enumerate((('sleep', 'Sleep', 'screen off, session kept'), ('restart', 'Restart', 'close everything, start again'), ('power', 'Shut down', 'close everything, switch off'))):
        sel = i == (1 if confirm else 0)
        fg, dim = (FOCUS_FG, FOCUS_DIM) if (sel and not confirm) else (TEXT, DIM)
        asking = confirm and i == 1
        c.rrect((x0 + 10, y, x1 - 10, y + ch - 6), RADIUS, FOCUS if (sel and not confirm) else PANEL)
        mid = y + (ch - 6) // 2
        c.icon(glyph, x0 + 32, mid, 18, FOCUS_FG if (sel and not confirm) else GLYPH)
        if asking:
            c.text((x0 + 58, mid - 17), 'Restart?', f.small, TEXT, 'lm'); c.text((x0 + 58, mid - 1), 'Unsaved work is lost.', f.tiny, DIM, 'lm')
            bw = 70; bx = x1 - 10 - 2 * bw - 14; by = mid + 9
            c.rrect((bx, by, bx + bw, by + 18), RADIUS, FOCUS, ACCENT, 2); c.text((bx + bw / 2, by + 9), 'Cancel', f.tiny, FOCUS_FG, 'mm')
            c.rrect((bx + bw + 6, by, bx + 2 * bw + 6, by + 18), RADIUS, LINE); c.text((bx + bw + 6 + bw / 2, by + 9), 'Restart', f.tiny, TEXT, 'mm')
        else:
            c.text((x0 + 58, mid - 8), label, f.small, fg, 'lm'); c.text((x0 + 58, mid + 11), sub, f.tiny, dim, 'lm')
        y += ch
    qs_footer(c, x0, y1, (('A', 'Choose'), ('B', 'Back')))
    return c


def screen_quick_power_confirm(f, wall): return screen_quick_power(f, wall, confirm=True)


SETTINGS_PAGES = [('Display', 'brightness'), ('Date & time', 'monitor'), ('Connections', 'wifi'), ('About', 'about')]


def settings_window(c, page, content):
    """The settings app: a window above the dock; categories at the left (D-pad up/down), the page at the right (D-pad right, A change, B back)."""
    f = c.f
    c.img.paste(BG, (0, 0, W, DOCK_TOP))
    c.text((20, 22), 'Settings', f.title, TEXT, 'lm'); c.hline(0, W, 44, LINE)
    side = 176; y = 56
    for name, glyph in SETTINGS_PAGES:
        on = name == page
        c.rrect((10, y, side - 8, y + 40), RADIUS, FOCUS if on else None)
        c.icon(glyph, 32, y + 20, 18, FOCUS_FG if on else GLYPH); c.text((52, y + 20), name, f.small, FOCUS_FG if on else TEXT, 'lm'); y += 44
    c.vline(side, 52, DOCK_TOP - 10, LINE)
    content(c, side + 18, W - 18)
    hx = side + 18
    for key, label in (('A', 'Change'), ('B', 'Back'), ('L2/R2', 'Page')): hx = c.key_pill(hx, DOCK_TOP - 32, key, label)


def seg_row(c, x0, x1, y, title, note, options, active, sel=False, focus=None):
    f = c.f
    c.text((x0, y), title, f.small_bold, TEXT, 'lm'); c.text((x0, y + 18), note, f.tiny, DIM, 'lm')
    n = len(options); w = (x1 - x0) / n
    for i, name in enumerate(options):
        sx = x0 + i * w; on = i == active
        c.rrect((sx, y + 34, sx + w - 4, y + 64), RADIUS, FOCUS if (on and sel) else TEXT if on else PANEL, ACCENT if (sel and i == focus) else None, 2)
        c.text((sx + (w - 4) / 2, y + 49), name, f.tiny, FOCUS_FG if on else DIM, 'mm')


def screen_settings_display(f, wall):
    c = desktop_base(f, wall); draw_dock(c, PINS, running=['terminal', 'images'], active='terminal')
    def content(c, x0, x1):
        seg_row(c, x0, x1, 80, 'Screen timeout', 'Turn the screen off after no input', ['Never', '1 min', '2 min', '5 min', '10 min', '30 min'], 2, sel=True, focus=4)
        seg_row(c, x0, x1, 170, 'Desktop size', 'Dock, menus and windows', ['100 %', '125 %', '150 %'], 0)
    settings_window(c, 'Display', content); return c


def screen_settings_datetime(f, wall):
    c = desktop_base(f, wall); draw_dock(c, PINS, running=['terminal', 'images'], active='terminal')
    def content(c, x0, x1):
        c.text((x0, 80), 'Time zone', c.f.small_bold, TEXT, 'lm')
        c.text((x0, 98), 'Current: Europe/Berlin   12:34', c.f.tiny, DIM, 'lm')
        for i, z in enumerate(('Europe/Berlin', 'Europe/Lisbon', 'Europe/London', 'America/New_York', 'America/Chicago')):
            y = 118 + i * 38; sel = i == 2
            c.rrect((x0, y, x1, y + 34), RADIUS, FOCUS if sel else PANEL)
            c.text((x0 + 14, y + 17), z, c.f.small, FOCUS_FG if sel else TEXT, 'lm')
            if i == 0: c.text((x1 - 14, y + 17), 'current', c.f.tiny, FOCUS_DIM if sel else DIM, 'rm')
    settings_window(c, 'Date & time', content); return c


SCREENS = {'desktop-empty': screen_empty, 'desktop-window': screen_window, 'dock-focus': screen_focus, 'dock-more': screen_more, 'apps-menu': screen_menu, 'quick-settings-power-button': screen_quick_power_button, 'quick-settings-power': screen_quick_power, 'quick-settings-power-confirm': screen_quick_power_confirm,  'quick-settings': screen_quick, 'quick-settings-submenu': screen_quick_submenu, 'osk': screen_osk, 'osk-symbols': screen_osk_symbols,
           'settings-display': screen_settings_display, 'settings-datetime': screen_settings_datetime}


def variants_sheet(f, badge):
    """The wallpaper alternatives side by side, so one can be chosen: plain, ghost, carved and a stronger carve."""
    sheet = Canvas(Image.new('RGB', (W, H), BG[:3]), f)
    cells = [('plain', 'plain', None), ('ghost 1.8 %', 'ghost', 1.8), ('carved 3 (default)', 'carved', 3.0), ('carved 5', 'carved', 5.0)]
    for i, (name, variant, strength) in enumerate(cells):
        im = wallpaper.render(W, H, variant, badge, strength).resize((W // 2, H // 2), Image.LANCZOS)
        x, y = (i % 2) * (W // 2), (i // 2) * (H // 2)
        sheet.img.paste(im.convert('RGBA'), (x, y))
        w = sheet.width(name, f.tiny) + 20
        sheet.rrect((x + 8, y + H // 2 - 30, x + 8 + w, y + H // 2 - 8), RADIUS, PANEL)
        sheet.text((x + 18, y + H // 2 - 19), name, f.tiny, TEXT, 'lm')
    return sheet


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--out', default=str(HERE / 'design'))
    ap.add_argument('--fonts', default=None, help='directory with the Liberation fonts (default: the first of the usual host places)')
    ap.add_argument('--badge', default=str(HERE.parent / 'setup/assets/wispos-badge.png'))
    a = ap.parse_args(argv)
    fonts_dir = a.fonts or next((d for d in ('/usr/share/fonts/truetype/liberation', '/usr/share/fonts/liberation') if (Path(d) / 'LiberationSans-Regular.ttf').exists()), None)
    if not fonts_dir: sys.exit('no Liberation fonts found: pass --fonts DIR')
    f = Fonts(fonts_dir); badge = Image.open(a.badge)
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    wall = wallpaper.render(W, H, 'carved', badge)
    wall.save(out / 'wallpaper.png', optimize=True)
    variants_sheet(f, badge).img.convert('RGB').save(out / 'wallpaper-variants.png', optimize=True)
    for name, fn in SCREENS.items():
        fn(f, wall).img.convert('RGB').save(out / (name + '.png'), optimize=True)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
