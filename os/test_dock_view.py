"""Draws the shell's views (os/dock/view.c) with the real renderer and compares them with the locked pictures (os/dock/design/*.png, drawn by mockup.py): each scene is rendered by
os/dock/test_scene.c onto a transparent canvas, put over the same backdrop the picture has under its shell layers (the wallpaper and the terminal window, drawn by mockup.py: they are
the compositor's and foot's, not the shell's), and compared pixel by pixel. Text and shapes are anti-aliased by different code (FreeType and a distance ramp here, Pillow's supersampling there),
so the comparison tolerates a difference at an edge but not a missing or misplaced element: the share of pixels that differ by more than THRESHOLD levels must stay under LIMIT.
Also checks that glyphs.h is the one gen_glyphs.py writes. No device, no Wayland."""
import os, shutil, subprocess, sys, tempfile, unittest
from pathlib import Path
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
DOCK = HERE / 'dock'
WORK = Path(os.environ.get('WISP_WORK', '/mnt/hdd/wispos-staging/wispos-os-20261007'))
FONT_DIRS = ('/usr/share/fonts/truetype/liberation', '/usr/share/fonts/liberation')
INC = ['-I/usr/include/freetype2']
THRESHOLD, LIMIT = 40, 0.0003                     # levels, share of the picture's pixels
BLUR, BLUR_LIMIT = 5, 16.0                        # a window and the most any window of the two pictures may differ by in mean level (finds a missing small element)

APPS = {'browser': ('Browser', 'firefox'), 'terminal': ('Terminal', 'foot'), 'settings': ('Settings', 'wisp-settings'), 'game': ('Game mode', 'wisp-game'),
        'images': ('Images', 'swayimg'), 'monitor': ('Monitor', 'htop'), 'about': ('About', 'wisp-about')}

sys.path.insert(0, str(DOCK)); sys.path.insert(0, str(HERE / 'ui'))
import mockup, wallpaper                      # noqa: E402


def box_blur(a, k):
    """The mean over k x k windows of an H x W x C array (edges repeated)."""
    p = k // 2
    ap = np.pad(a, ((p, p), (p, p), (0, 0)), mode='edge')
    c = np.pad(ap.cumsum(0).cumsum(1), ((1, 0), (1, 0), (0, 0)))
    h, w = a.shape[:2]
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


def backdrops(fonts_dir):
    f = mockup.Fonts(fonts_dir)
    badge = Image.open(HERE / 'setup/assets/wispos-badge.png')
    wall = wallpaper.render(mockup.W, mockup.H, 'carved', badge)
    def terminal(height=None, typed=None):
        c = mockup.desktop_base(f, wall); mockup.terminal_window(c, height, typed); return c.img
    return {'desktop-empty': mockup.desktop_base(f, wall).img, 'desktop-window': terminal(), 'dock-focus': terminal(), 'dock-more': terminal(), 'apps-menu': terminal(),
            'quick-settings': terminal(), 'quick-settings-submenu': terminal(), 'settings-display': mockup.desktop_base(f, wall).img, 'add-game-places': mockup.desktop_base(f, wall).img, 'add-game-pick': mockup.desktop_base(f, wall).img, 'add-game-confirm': mockup.desktop_base(f, wall).img, 'add-game-done': mockup.desktop_base(f, wall).img, 'osk': terminal(mockup.token('WISP_UI_OSK_Y'), 'wisp-usb sta'), 'osk-symbols': terminal(mockup.token('WISP_UI_OSK_Y'), 'ls -la /storage/')}


# (what it breaks, the text in view.c, what it becomes)
VIEW_MUTANTS = [
    ('the bar of the window in front is not drawn', 'if (it->active) {', 'if (0) {'),
    ('a running application has no dot', 'rd_disc(v->c, X(v, 0) + cx, Y(v, 0) + my + 1.5f, WISP_UI_DOCK_RUN_DOT / 2.f, focused ? FOCUS_DIM : DIM);', '(void)0;'),
    ('a focused tile keeps the glyph colour', 'focused ? FOCUS_FG : ic->ink', 'ic->ink'),
    ('the summoned strip has no accent line', 'd->summoned ? 2.f : 1.f', '1.f'),
    ('the status group has no wifi glyph', 'view_glyph(v, "wifi", d->wifi_level, st,', 'view_glyph(v, "wifi", d->wifi_level, 99,'),
    ('the battery has no fill', 'if (level > 0.f) rd_rrect(', 'if (0) rd_rrect('),
    ('the clock is left-aligned', 'focused ? FOCUS_FG : TEXT, RD_RIGHT, RD_MIDDLE);', 'focused ? FOCUS_FG : TEXT, RD_LEFT, RD_MIDDLE);'),
    ('the focus bar hints sit on the panel colour', 'rd_pill(v->c, X(v, x), Y(v, y), X(v, x + pill[i]), Y(v, y + 20), BG);', 'rd_pill(v->c, X(v, x), Y(v, y), X(v, x + pill[i]), Y(v, y + 20), PANEL);'),
    ('the popup rows are one pixel low', 'int ry = y0 + 6 + i * rh;', 'int ry = y0 + 7 + i * rh;'),
    ('the popup has no outline', 'rd_rrect_ring(v->c, X(v, x0), Y(v, y0), X(v, x0 + width), Y(v, y1), WISP_UI_RADIUS, 1.f, LINE);', ''),
    ('the pin mark is a pixel left', '(float)(cx + tile / 2 - 11)', '(float)(cx + tile / 2 - 12)'),
    ('the menu scrim is thin', 'RD_RGBA(WISP_UI_BG, WISP_UI_MENU_SCRIM)', 'RD_RGBA(WISP_UI_BG, 200)'),
    ('the selected menu tile is not white', 'sel ? FOCUS : PANEL);\n        view_icon', 'PANEL);\n        view_icon'),
    ('the slider knob is smaller', '(float)WISP_UI_SLIDER_KNOB', '5.f'),
    ('the selected page is not white', 'if (on) rd_rrect(v->c, X(v, 10), Y(v, y), X(v, SET_SIDE - 8), Y(v, y + 40), WISP_UI_RADIUS, FOCUS);', 'if (on) rd_rrect(v->c, X(v, 10), Y(v, y), X(v, SET_SIDE - 8), Y(v, y + 40), WISP_UI_RADIUS, PANEL);'),
    ('the option under the pad has no ring', 'if (sel && k == row->focus) rd_rrect_ring(', 'if (0) rd_rrect_ring('),
    ('the option that applies is not marked', '(on && sel) ? FOCUS : on ? TEXT : PANEL);', '(on && sel) ? FOCUS : PANEL);'),
    ('the settings title is missing', 'rd_text(v->c, v->f->title, IX(v, 20), IY(v, 22), "Settings", TEXT, RD_LEFT, RD_MIDDLE);', ''),
    ('the settings rows are one pixel low', '#define SET_ROW_Y(i) (80 + 90 * (i))', '#define SET_ROW_Y(i) (81 + 90 * (i))'),
    ('the add game hints sit higher', 'view_key_pill(v, hx, DOCK_TOP - 32, a->hints[i].key', 'view_key_pill(v, hx, DOCK_TOP - 34, a->hints[i].key'),
    ('the add game rows are one pixel low', '#define AG_ROW_Y(i) (82 + 42 * (i))', '#define AG_ROW_Y(i) (83 + 42 * (i))'),
    ('the add game title is missing', 'rd_text(v->c, v->f->title, IX(v, 20), IY(v, 22), "Add game", TEXT, RD_LEFT, RD_MIDDLE);', ''),
    ('the focused place is not highlighted', 'if (here) rd_rrect(v->c, X(v, 10), Y(v, y), X(v, SET_SIDE - 8), Y(v, y + 46), WISP_UI_RADIUS, on ? FOCUS : PANEL);', 'if (here) rd_rrect(v->c, X(v, 10), Y(v, y), X(v, SET_SIDE - 8), Y(v, y + 46), WISP_UI_RADIUS, PANEL);'),
    ('a program has no accent colour', 'sel ? FOCUS_FG : exe ? ACCENT : GLYPHC);', 'sel ? FOCUS_FG : GLYPHC);'),
    ('the size note is not drawn', 'if (r->note) rd_text(v->c, v->f->tiny, IX(v, SET_X1 - 14)', 'if (0) rd_text(v->c, v->f->tiny, IX(v, SET_X1 - 14)'),
    ('the path line is not drawn', 'rd_text_fit(v->c, v->f->tiny, IX(v, SET_X0), IY(v, 66), SET_X1 - SET_X0, a->path, DIM, RD_LEFT, RD_MIDDLE);', ''),
    ('the add button is not highlighted', 'rd_rrect(v->c, X(v, ag_btn_x(b)), Y(v, AG_BTN_Y), X(v, ag_btn_x(b) + AG_BTN_W), Y(v, AG_BTN_Y + 34), WISP_UI_RADIUS, on ? FOCUS : LINE);', 'rd_rrect(v->c, X(v, ag_btn_x(b)), Y(v, AG_BTN_Y), X(v, ag_btn_x(b) + AG_BTN_W), Y(v, AG_BTN_Y + 34), WISP_UI_RADIUS, LINE);'),
    ('the dialog does not dim the window', 'rd_rrect(v->c, X(v, 0), Y(v, 0), X(v, WISP_UI_W), Y(v, DOCK_TOP), 0, RD_RGBA(WISP_UI_BG, 150));', ''),
    ('the dialog has no outline', 'rd_rrect_ring(v->c, X(v, x0), Y(v, y0), X(v, x0 + AG_DONE_W), Y(v, y0 + AG_DONE_H), WISP_UI_RADIUS, 1.f, LINE);', ''),
    ('the settings hints sit higher', 'view_key_pill(v, hx, DOCK_TOP - 32, s->hints[i].key', 'view_key_pill(v, hx, DOCK_TOP - 34, s->hints[i].key'),
    ('the battery page icon has no fill', 'battery(v, 18, 32.f, (float)(y + 20), ink, s->pages[i].level);', 'battery(v, 18, 32.f, (float)(y + 20), ink, 0.f);'),
    ('the wifi page icon shows no bars', 'view_glyph(v, s->pages[i].glyph, (int)s->pages[i].level, 18, 32.f,', 'view_glyph(v, s->pages[i].glyph, 0, 18, 32.f,'),
    ('the divider is missing', 'rd_vline(v->c, X(v, SET_SIDE), Y(v, 52), Y(v, DOCK_TOP - 10), 1.f, LINE);\n    for (int r = 0; r < s->nrows; r++) {', '    for (int r = 0; r < s->nrows; r++) {'),
    ('the add game divider is missing', 'rd_vline(v->c, X(v, SET_SIDE), Y(v, 52), Y(v, DOCK_TOP - 10), 1.f, LINE);\n    if (a->phase == 0) {', '    if (a->phase == 0) {'),
    ('a link row has no chevron', 'view_glyph(v, "right", 0, 18, (float)(x0 + pw - 28), (float)top, sel ? FOCUS_FG : DIM);', '(void)0;'),
    ('the submenu panel follows its rows', 'if (q->height > 0) return q->height;', ''),
    ('the back row has no chevron', 'view_glyph(v, "left", 0, 18, (float)(x0 + 28), (float)top, sel ? FOCUS_FG : GLYPHC);', '(void)0;'),
    ('a mode has no second line', 'if (r->value) rd_text_fit(v->c, v->f->tiny, IX(v, x0 + 36), IY(v, top + 11), pw - 36 - 56, r->value, dim, RD_LEFT, RD_MIDDLE);', '(void)0;'),
    ('the mark of the mode that is on is white on the white selected row', 'r->on ? (sel ? FOCUS_FG : ACCENT) : BG);', 'r->on ? (sel ? FOCUS : ACCENT) : BG);'),
    ('the submenu note is one pixel low', 'IY(v, y + 6), r->label, DIM, RD_LEFT, RD_MIDDLE);', 'IY(v, y + 7), r->label, DIM, RD_LEFT, RD_MIDDLE);'),
    ('a mode row is not one and a half rows high', 'if (r->kind == QS_CHOICE) return rh + rh / 2;', 'if (r->kind == QS_CHOICE) return rh + 10;'),
    ('the quick settings hints sit higher', 'view_key_pill(v, hx, y1 - foot + 4,', 'view_key_pill(v, hx, y1 - foot + 2,'),
    ('the keyboard rows are one pixel further apart', 'int y = WISP_UI_OSK_Y + 8 + row * (WISP_UI_KEY_H + WISP_UI_KEY_GAP);', 'int y = WISP_UI_OSK_Y + 8 + row * (WISP_UI_KEY_H + WISP_UI_KEY_GAP + 1);'),
    ('the keyboard special row is shifted', '*out = (VRect){ x, y, x + k.width, y + WISP_UI_KEY_H };', '*out = (VRect){ x + (row == 4), y, x + (row == 4) + k.width, y + WISP_UI_KEY_H };'),
    ('the keyboard hints are on the wrong line', 'x = view_key_pill(v, x, WISP_UI_HINT_Y, h[i].key, h[i].label, PANEL);', 'x = view_key_pill(v, x, WISP_UI_HINT_Y + 2, h[i].key, h[i].label, PANEL);'),
    ('the symbols arrows are not drawn', 'if (k->glyph) view_glyph(v, k->glyph, 0, 22,', 'if (0) view_glyph(v, k->glyph, 0, 22,'),
]


@unittest.skipUnless(shutil.which('gcc'), 'needs a native gcc')
class ViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fonts = next((d for d in FONT_DIRS if (Path(d) / 'LiberationSans-Regular.ttf').exists()), None)
        assert cls.fonts, 'no Liberation fonts on the host'
        cls.d = Path(tempfile.mkdtemp(prefix='wisp-view-', dir=WORK / 'tmp'))
        apps = cls.d / 'apps'; apps.mkdir()
        for k, (name, exe) in APPS.items():
            extra = 'StartupWMClass=swayimg\n' if k == 'images' else ''
            (apps / (k + '.desktop')).write_text('[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nIcon=%s\n%s' % (name, exe, k, extra))
        cls.exe = cls.d / 'test_scene'
        r = subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined'] + INC +
                           ['-o', str(cls.exe)] + [str(DOCK / f) for f in ('test_scene.c', 'view.c', 'render.c', 'model.c')] + ['-lfreetype', '-lpng', '-lm'], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.backdrops = backdrops(cls.fonts)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def render(self, scene, exe=None):
        out = self.d / (scene + '.raw')
        r = subprocess.run([str(exe or self.exe), scene, self.fonts, str(self.d / 'apps'), str(out)], capture_output=True, text=True, env={**os.environ, 'ASAN_OPTIONS': 'detect_leaks=0'})
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        raw = np.fromfile(out, np.uint32)
        w, h = int(raw[0]), int(raw[1])
        argb = raw[2:].reshape(h, w)
        layer = np.stack([(argb >> 16) & 255, (argb >> 8) & 255, (argb >> 0) & 255, (argb >> 24) & 255], -1).astype(np.float32)
        back = np.asarray(self.backdrops[scene].convert('RGB'), np.float32)
        out = layer[..., :3] + back * (1.0 - layer[..., 3:4] / 255.0)             # the layer is premultiplied
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    def measure(self, scene, ignore=(), exe=None):
        """(the share of pixels that differ by more than THRESHOLD, the largest mean difference of any window, the picture)."""
        got = self.render(scene, exe)
        want = np.asarray(Image.open(DOCK / 'design' / (scene + '.png')).convert('RGB'), np.int16)
        for x0, y0, x1, y1 in ignore:
            got[y0:y1, x0:x1] = want[y0:y1, x0:x1]
        diff = np.abs(got.astype(np.int16) - want).max(-1)
        blurred = float(np.abs(box_blur(got.astype(np.float64), BLUR) - box_blur(want.astype(np.float64), BLUR)).max())
        return float((diff > THRESHOLD).mean()), blurred, got, diff

    def compare(self, scene, ignore=()):
        """ignore: boxes (x0, y0, x1, y1) that are not compared, each with a reason where it is passed."""
        share, blurred, got, diff = self.measure(scene, ignore)
        self.assertLess(blurred, BLUR_LIMIT, '%s: a %dx%d window of the two pictures differs by %.1f levels: an element is missing, misplaced or the wrong colour' % (scene, BLUR, BLUR, blurred))
        if share >= LIMIT:
            Image.fromarray(got).save(self.d / (scene + '-got.png'))
            Image.fromarray(np.clip(diff * 4, 0, 255).astype(np.uint8)).save(self.d / (scene + '-diff.png'))
            keep = Path(os.environ.get('WISP_VIEW_DIFF', '/mnt/hdd/wispos-staging/wispos-os-20261007/tmp/view-diff')); keep.mkdir(parents=True, exist_ok=True)
            shutil.copy(self.d / (scene + '-got.png'), keep); shutil.copy(self.d / (scene + '-diff.png'), keep)
        self.assertLess(share, LIMIT, '%s: %.3f%% of the pixels differ by more than %d levels (the pictures are in %s)' % (scene, share * 100, THRESHOLD, os.environ.get('WISP_VIEW_DIFF', 'tmp/view-diff')))
        return diff

    def test_glyphs_header_is_what_the_generator_writes(self):
        r = subprocess.run([sys.executable, '-I', str(DOCK / 'gen_glyphs.py'), '--check'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    SCENES = {'desktop-empty': [(436, 258, 460, 290)], 'desktop-window': [], 'dock-focus': [], 'dock-more': [], 'apps-menu': [], 'quick-settings': [], 'quick-settings-submenu': [], 'settings-display': [], 'add-game-places': [], 'add-game-pick': [], 'add-game-confirm': [], 'add-game-done': [], 'osk': [], 'osk-symbols': []}

    def test_the_comparison_notices_changes_to_the_views(self):
        """Each of these edits of view.c (a missing bar, a shifted row, a wrong colour ...) must make at least one scene fail, or the comparison above could not be relied on."""
        src = (DOCK / 'view.c').read_text()
        survived = []
        for i, (what, old, new) in enumerate(VIEW_MUTANTS):
            self.assertEqual(src.count(old), 1, '"%s" does not match view.c exactly once (%d)' % (what, src.count(old)))
            m = self.d / ('view-mutant%d.c' % i); m.write_text(src.replace(old, new)); exe = self.d / ('view-mutant%d' % i)
            r = subprocess.run(['gcc', '-std=gnu11', '-O1', '-w', '-I', str(DOCK)] + INC + ['-o', str(exe), str(DOCK / 'test_scene.c'), str(m), str(DOCK / 'render.c'), str(DOCK / 'model.c'), '-lfreetype', '-lpng', '-lm'],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, '"%s" does not compile: %s' % (what, r.stderr[:300]))
            caught = False
            for scene, ignore in self.SCENES.items():
                share, blurred, _, _ = self.measure(scene, ignore, exe)
                if share >= LIMIT or blurred >= BLUR_LIMIT: caught = True; break
            if not caught: survived.append(what)
        self.assertEqual(survived, [], 'the picture comparison does not notice these changes to view.c')

    def test_desktop_empty(self):        self.compare('desktop-empty', ignore=[(436, 258, 460, 290)])     # the pointer is the compositor's
    def test_desktop_window(self):       self.compare('desktop-window')
    def test_dock_focus(self):           self.compare('dock-focus')
    def test_dock_more(self):            self.compare('dock-more')
    def test_apps_menu(self):            self.compare('apps-menu')
    def test_quick_settings(self):
        self.compare('quick-settings')
    def test_quick_settings_submenu(self): self.compare('quick-settings-submenu')
    def test_settings_display(self):     self.compare('settings-display')
    def test_add_game_places(self):      self.compare('add-game-places')
    def test_add_game_pick(self):        self.compare('add-game-pick')
    def test_add_game_confirm(self):     self.compare('add-game-confirm')
    def test_add_game_done(self):        self.compare('add-game-done')
    def test_osk(self):                  self.compare('osk')
    def test_osk_symbols(self):          self.compare('osk-symbols')


if __name__ == '__main__':
    unittest.main()
