"""Tests for the desktop-mode design pictures (os/dock/mockup.py, os/ui/wallpaper.py; docs/WISPOS-UI-LANGUAGE.md "Desktop mode").

The pictures are design references, not screenshots, so what is tested is what makes them trustworthy: they are the panel's size and the same every time; the committed
ones are current; the colours come from the one header; the wallpaper stays "very subtle" (the operator's word) and dithered; the strip is exactly the token's height; and the
keyboard keeps the first-time setup keyboard's rows, its black special keys, its accent Enter and its white focused key. No device, no network, no root.
"""
import ast, importlib.util, os, re, shutil, sys, tempfile, unittest
from pathlib import Path
import numpy as np
from PIL import Image

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get('WISP_WORK', '/mnt/hdd/wispos-staging/wispos-os-20261007'))
EXPECTED = {'wallpaper', 'wallpaper-variants', 'desktop-empty', 'desktop-window', 'dock-focus', 'dock-more', 'apps-menu', 'quick-settings', 'quick-settings-submenu', 'settings-display', 'add-game-places', 'add-game-pick', 'add-game-confirm', 'add-game-done', 'settings-datetime', 'osk', 'osk-symbols'}


def load(path, name):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path); mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


mockup = load(HERE / 'dock/mockup.py', 'wisp_mockup')
wallpaper = mockup.wallpaper
BADGE = Image.open(HERE / 'setup/assets/wispos-badge.png')


def px(path):
    with Image.open(path) as im: return np.asarray(im.convert('RGB'), np.int16)


class Pictures(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix='wisp-design-', dir=WORK / 'tmp'))
        assert mockup.main(['--out', str(cls.out)]) == 0
        cls.again = Path(tempfile.mkdtemp(prefix='wisp-design-', dir=WORK / 'tmp'))
        assert mockup.main(['--out', str(cls.again)]) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True); shutil.rmtree(cls.again, True)

    def test_the_set_is_what_the_documents_name_and_every_picture_is_the_panel_size(self):
        self.assertEqual({p.stem for p in self.out.glob('*.png')}, EXPECTED)
        for p in self.out.glob('*.png'):
            with Image.open(p) as im: self.assertEqual(im.size, (mockup.W, mockup.H), p.name)
        self.assertEqual((mockup.W, mockup.H), (720, 480))

    def test_two_renders_are_identical(self):
        for p in self.out.glob('*.png'): self.assertEqual(p.read_bytes(), (self.again / p.name).read_bytes(), p.name)

    def test_the_committed_pictures_are_current(self):
        design = HERE / 'dock/design'
        self.assertEqual({p.stem for p in design.glob('*.png')}, EXPECTED)
        for p in sorted(design.glob('*.png')):
            # not byte-for-byte: another FreeType may rasterise a glyph a shade differently; a changed token or layout moves far more than that
            self.assertLess(float(np.abs(px(p) - px(self.out / p.name)).mean()), 0.6, '%s is out of date: run os/dock/mockup.py' % p.name)

    def test_colours_come_from_the_header_not_from_literals(self):
        for f in ('dock/mockup.py', 'ui/wallpaper.py'):
            text = (HERE / f).read_text()
            self.assertFalse(re.findall(r'0x[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{6}\b', text), '%s has a hex colour literal' % f)
            tree = ast.parse(text); loops = {id(n.iter) for n in ast.walk(tree) if isinstance(n, ast.For)}          # `for cx in (5, 12, 19)` is geometry
            for node in ast.walk(tree):                     # nor an RGB(A) tuple of plain numbers other than black, white and transparent
                if id(node) in loops: continue
                if isinstance(node, ast.Tuple) and 3 <= len(node.elts) <= 4 and all(isinstance(e, ast.Constant) and isinstance(e.value, int) for e in node.elts):
                    values = [e.value for e in node.elts]
                    if all(0 <= v <= 255 for v in values): self.assertTrue(all(v in (0, 255) for v in values), '%s line %d: %s' % (f, node.lineno, values))

    def test_the_strip_is_exactly_the_dock_height_and_the_wallpaper_shows_above_it(self):
        im = px(self.out / 'desktop-empty.png'); h = mockup.token('WISP_UI_DOCK_H'); top = mockup.H - h
        above, strip = im[top - 3, 300].sum(), im[top + 8, 300].sum()
        self.assertGreater(strip, above + 40)                                   # the surface is clearly lighter than the dark wallpaper
        self.assertLess(abs(int(im[top - 3, 300].sum()) - int(im[top - 30, 300].sum())), 30)        # and nothing but wallpaper above the strip

    def test_the_dock_is_the_same_strip_in_every_screen_that_shows_it(self):
        top = mockup.H - mockup.token('WISP_UI_DOCK_H')
        strips = [px(self.out / (n + '.png'))[top + 6:, 300:400].mean() for n in ('desktop-empty', 'desktop-window', 'dock-more', 'quick-settings')]      # the bare middle of the strip
        self.assertLess(max(strips) - min(strips), 6)


class Wallpaper(unittest.TestCase):
    def test_the_badge_stays_very_subtle(self):
        plain = np.asarray(wallpaper.render(720, 480, 'plain'), np.int16)
        for variant in ('ghost', 'carved'):
            d = np.abs(np.asarray(wallpaper.render(720, 480, variant, BADGE), np.int16) - plain)
            self.assertLessEqual(int(d.max()), 6, variant)                       # a few levels of 255, the operator's "very subtle"
            self.assertGreater(int(d.max()), 2, variant)                         # but not nothing
            self.assertGreater(int((d.max(axis=2) > 1).sum()), 20000, variant)   # and a real shape, not a speck

    def test_the_picture_is_dark_and_follows_the_palette(self):
        im = np.asarray(wallpaper.render(720, 480, 'carved', BADGE), np.int16)
        self.assertLess(int(im.max()), 70)
        top, bottom = im[:40].mean(axis=(0, 1)), im[-40:].mean(axis=(0, 1))
        self.assertGreater(top.sum(), bottom.sum() + 15)                         # lighter at the top, a gradient
        self.assertGreater(top[2], top[0] + 8)                                   # and blue

    def test_dither_is_one_level_at_most_and_present(self):
        base = np.rint(wallpaper.gradient(720, 480))
        d = np.asarray(wallpaper.render(720, 480, 'plain'), np.float32) - base
        self.assertLessEqual(float(np.abs(d).max()), 1.0)
        self.assertGreater(float((np.abs(d) > 0.5).mean()), 0.2)

    def test_the_variant_needs_the_badge(self):
        with self.assertRaises(ValueError): wallpaper.render(720, 480, 'ghost')

    def test_another_panel_size_gets_the_same_picture(self):
        a = wallpaper.render(1280, 800, 'carved', BADGE)
        self.assertEqual(a.size, (1280, 800))


class Keyboard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.out = Path(tempfile.mkdtemp(prefix='wisp-design-', dir=WORK / 'tmp')); assert mockup.main(['--out', str(cls.out)]) == 0

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.out, True)

    def test_the_rows_are_the_first_time_setup_keyboards(self):
        c = (HERE / 'setup/kb.h').read_text()                                                   # the setup screen's keyboard layout (wisp-setup.c includes it)
        for name, c_name in (('ROWS_LOWER', 'KB_LOWER'), ('ROWS_UPPER', 'KB_UPPER'), ('ROWS_SYM', 'KB_SYM')):
            body = re.search(r'%s\[KB_ROWS\]\s*=\s*\{(.*)\};$' % c_name, c, re.M).group(1)      # to the end of the line: `};` also occurs inside a string of the symbols row
            rows = tuple(ast.literal_eval(s) for s in re.findall(r'"(?:[^"\\]|\\.)*"', body))
            if name == 'ROWS_SYM': rows = tuple(r.replace('~', '').replace('`', '') for r in rows)    # the setup page carries these two in its rows (11 narrower keys); the desktop keyboard on its special row
            self.assertEqual(getattr(mockup, name), rows, name)

    def test_every_printable_ascii_character_can_be_typed_on_the_desktop_keyboard_too(self):
        view = (HERE / 'dock/view.c').read_text()
        specials = set(re.findall(r'\{ "(.)", \d+, NULL, \d, "char" \}', view))                      # the characters on the symbols page's special row
        self.assertEqual(specials, {'`', '~'})
        typable = set(''.join(mockup.ROWS_LOWER + mockup.ROWS_UPPER + mockup.ROWS_SYM)) | specials | {' '}
        self.assertEqual([chr(c) for c in range(32, 127) if chr(c) not in typable], [])

    def test_keys_are_drawn_as_the_setup_screen_draws_them(self):
        im = px(self.out / 'osk.png'); kw, kh, gap = (mockup.token('WISP_UI_KEY_' + n) for n in ('W', 'H', 'GAP'))
        py = mockup.token('WISP_UI_OSK_Y'); ky = py + 8
        # row 2 is nine letter keys centred; the focused one (g, the fifth) is white
        x0 = (mockup.W - (9 * (kw + gap) - gap)) // 2
        self.assertEqual(tuple(im[ky + 2 * (kh + gap) + 4, x0 + 4 * (kw + gap) + 4]), (255, 255, 255))
        self.assertEqual(tuple(im[ky + 2 * (kh + gap) + 4, x0 + 3 * (kw + gap) + 4]), mockup.BG[:3])            # its neighbour: a key in the ground colour
        sy = ky + 4 * (kh + gap) + 4
        specials = [70, 78, 62, 178, 62, 62, 100]; total = sum(specials) + 4 * 6; x = (mockup.W - total) // 2
        self.assertEqual(tuple(im[sy, x + 4]), mockup.FIELD[:3])                                                 # "123": a black special key
        enter = x + sum(specials[:6]) + 4 * 6 + 4
        self.assertEqual(tuple(im[sy, enter]), mockup.ACCENT[:3])                                                # Enter: the accent, as Connect is

    def test_the_symbols_page_fits_the_panel(self):
        # found while drawing it: wisp-setup.c's symbols page put ten keys and `del` in one row: 504 px in a 450 px panel (fixed 2026-10-12: del moved among the special keys, os/setup/kb.h, tested by test_kb.c)
        kw, gap = mockup.token('WISP_UI_KEY_W'), mockup.token('WISP_UI_KEY_GAP')
        widest = max(len(r) for r in mockup.ROWS_SYM) * (kw + gap) - gap
        self.assertLessEqual(widest, mockup.W - 24)
        specials = 70 + 52 * 6 + 110 + 64 + 90 + 4 * 9
        self.assertLessEqual(specials, mockup.W - 24)


if __name__ == '__main__':
    unittest.main()
