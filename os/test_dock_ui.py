"""Runs the desktop shell's UI state machine tests (os/dock/test_ui.c: the dock, its actions list, the application menu, the quick settings, the on-screen keyboard, the pointer, windows and
a random run that checks the state stays consistent) natively under the address and undefined-behaviour sanitizers, checks that ui.c builds for the device, and checks the tests themselves:
each of a list of rule mutants of ui.c (a missing hint, a wrong step, a lost effect ...) must fail them. No device, no Wayland."""
import os, shutil, subprocess, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
DOCK = HERE / 'dock'
WORK = Path(os.environ.get('WISP_WORK', '/mnt/hdd/wispos-staging/wispos-os-20261007'))
INC = ['-I/usr/include/freetype2']
SOURCES = ['ui.c', 'view.c', 'render.c', 'model.c']
LIBS = ['-lfreetype', '-lpng', '-lm']

# (what it breaks, the text in ui.c, what it becomes)
MUTANTS = [
    ('a summoned dock starts on the first item', 'set_focus(u, wd_dock_first_focus(u->items, u->n));\n}\n\nstatic void qs_open', 'set_focus(u, 0);\n}\n\nstatic void qs_open'),
    ('a running application is launched again instead of brought forward', 'if ((it->kind == WD_PIN || it->kind == WD_WINDOW) && it->running && it->window)', 'if (0 && it->running && it->window)'),
    ('X does not unpin in the summoned dock', 'else if (u->dock == UI_SUMMONED) item_pin_toggle(u, u->focus);', ''),
    ('X in the menu opens the keyboard', 'if (u->menu) { if (u->msel >= 0 && u->msel < u->mn)', 'if (0) { if (u->msel >= 0 && u->msel < u->mn)'),
    ('Space with an empty query does not search', 'if (!u->query[0]) { osk_set(u, true); return; }', 'if (0) { osk_set(u, true); return; }'),
    ('Escape never clears a query first', 'if (u->query[0]) { u->query[0] = 0; u->msel = u->mtop = 0; menu_refilter(u); dirty(u, DIRTY_MENU); }\n        else menu_close(u);', 'menu_close(u);'),
    ('the query is unbounded', 'if (len + add >= UI_QUERY) return;', ''),
    ('backspace cuts a letter in half', 'while (len > 0 && ((unsigned char)u->query[len - 1] & 0xc0) == 0x80) len--;          /* a whole code point */', ''),
    ('the volume step is 10 %', '#define VOLUME_STEP 0.05f', '#define VOLUME_STEP 0.10f'),
    ('the brightness can go to black', '#define BRIGHTNESS_MIN 0.05f', '#define BRIGHTNESS_MIN 0.0f'),
    ('USB is applied when nothing changed', 'if (u->qs_sel >= 1 && u->qs_sel <= 3 && u->qs_sel - 1 != u->sys.usb) {', 'if (u->qs_sel >= 1 && u->qs_sel <= 3) {'),
    ('B in the USB submenu closes the panel', 'case KEY_ESC: if (u->qs_page == QS_PAGE_USB) qs_page_set(u, QS_PAGE_LIST, 5); else panel_back(u); break;', 'case KEY_ESC: panel_back(u); break;'),
    ('the USB submenu opens on the first mode', 'case 5: qs_page_set(u, QS_PAGE_USB, 1 + clampi(u->sys.usb, 0, 2)); return;', 'case 5: qs_page_set(u, QS_PAGE_USB, 1); return;'),
    ('the note under the modes can be selected', 'u->qs_page == QS_PAGE_USB ? (row >= 0 && row <= 3)', 'u->qs_page == QS_PAGE_USB ? (row >= 0 && row <= 4)'),
    ('the back row of the USB submenu applies a mode', 'if (u->qs_sel >= 1 && u->qs_sel <= 3 &&', 'if (u->qs_sel >= 0 && u->qs_sel <= 3 &&'),
    ('the quick settings adjust values while a submenu is open', 'if (u->qs_page != QS_PAGE_LIST) return;', ''),
    ('the battery row can be selected', ': (row >= 0 && row <= 5); }', ': (row >= 0 && row <= 6); }'),
    ('Shift lasts for ever', 'if (u->shift) { u->shift = false; if (u->page == OSK_UPPER) u->page = OSK_LOWER; }', ''),
    ('Ctrl lasts for ever', 'eff(u, EFF_TYPE, code, shift, u->ctrl, 0, NULL);\n    u->ctrl = false;', 'eff(u, EFF_TYPE, code, shift, u->ctrl, 0, NULL);'),
    ('left does not wrap on the keyboard', 'if (dir == 2) u->ocol = u->ocol == 0 ? n - 1 : u->ocol - 1;', 'if (dir == 2) u->ocol = u->ocol == 0 ? 0 : u->ocol - 1;'),
    ('B in the actions list closes the whole dock', 'case KEY_ESC: u->dock = UI_SUMMONED; u->nacts = 0; dirty(u, DIRTY_DOCK); break;', 'case KEY_ESC: dock_close(u); break;'),
    ('B in the quick settings always closes the dock', 'if (u->from_pad && u->dock != UI_SUMMONED) { u->dock = UI_SUMMONED; u->nacts = 0; dirty(u, DIRTY_DOCK); }\n    else dock_close(u);', 'dock_close(u);'),
    ('a click outside the quick settings does not close them', '                else dock_close(u);                                                                    /* the status group again closes it, as does a click anywhere else */', ''),
    ('Mode does not toggle the dock', 'if (u->dock == UI_SUMMONED || u->dock == UI_ACTIONS_OPEN || u->dock == UI_QS) dock_close(u);\n        else dock_summon(u, true);', 'dock_summon(u, true);'),
    ('Start does not toggle the menu', 'if (u->menu) menu_close(u); else menu_open(u);', 'menu_open(u);'),
    ('the pad is told the window in front every time', 'if (strcmp(front, u->pad_focus) != 0) {', 'if (1) {'),
    ('a full dock says nothing', 'if (r < 0) { toast_text(u, "The dock is full", "Unpin something first.", TOAST_MS); return; }', 'if (r < 0) return;'),
    ('the pointer does not select in the menu', 'if (ev == PTR_MOVE && hit >= 0 && hit != u->msel) { u->msel = hit; dirty(u, DIRTY_MENU); }', 'if (0) { }'),
    ('a right click in the menu launches', 'else if (button == BTN_RIGHT) { pin_toggle_app(u, u->mlist[hit]); dirty(u, DIRTY_MENU); }', 'else if (button == BTN_RIGHT) { u->msel = hit; menu_launch(u); }'),
    ('a press released elsewhere is a click', 'if (pressed >= 0 && pressed == hit) item_activate(u, hit);', 'if (pressed >= 0) item_activate(u, pressed);'),
    ('the wheel scrolls past the end of the menu', 'u->mtop = clampi(u->mtop - steps, 0, max_top);', 'u->mtop = clampi(u->mtop - steps, 0, 1000);'),
    ('a symbol is on the wrong key', 'static const char DIGIT_SHIFT[] = "!@#$%^&*()";', 'static const char DIGIT_SHIFT[] = "@!#$%^&*()";'),
    ('Shift+Tab goes the wrong way', 'case KEY_TAB: set_focus(u, wd_dock_move(u->items, u->n, u->focus, (mods & UI_SHIFT) ? -1 : 1)); break;', 'case KEY_TAB: set_focus(u, wd_dock_move(u->items, u->n, u->focus, 1)); break;'),
    ('left lowers nothing in the quick settings', 'else qs_adjust(u, -1); break;', 'else qs_adjust(u, 1); break;'),
    ('the quick settings do not ask for fresh values', 'eff(u, EFF_REFRESH, 0, 0, 0, 0, NULL);\n    dirty(u, DIRTY_DOCK);\n}', 'dirty(u, DIRTY_DOCK);\n}'),
    ('a toast stays for ever', 'u->toast.until = u->now + ttl_ms;', 'u->toast.until = u->now + 1000000;'),
    ('the bar labels are swapped', 'it->kind == WD_STATUS ? "Settings" : (it->running ? "Switch" : "Open")', 'it->kind == WD_STATUS ? "Settings" : (it->running ? "Open" : "Switch")'),
    ('the dock surface never grows for a panel', 'g.dock_h = g.dock_modal ? WISP_UI_H : WISP_UI_DOCK_H;', 'g.dock_h = WISP_UI_DOCK_H;'),
    ('the dock gets pointer events under the menu', 'if (!u->menu && !u->osk) pointer_dock(u, x, y, ev, button);', 'pointer_dock(u, x, y, ev, button);'),
    ('Game mode leaves the dock open', 'case WD_GAME: eff(u, EFF_GAME_MODE, 0, 0, 0, 0, NULL); dock_close(u); break;', 'case WD_GAME: eff(u, EFF_GAME_MODE, 0, 0, 0, 0, NULL); break;'),
    ('the focus does not follow a pinned window', 'for (int k = 0; k < u->n && at < 0; k++) if (u->items[k].app == app && (u->items[k].kind == WD_PIN || u->items[k].kind == WD_WINDOW)) at = k;', ''),
    ('the wheel does nothing in the actions list', 'u->act_sel = clampi(u->act_sel - steps, 0, u->nacts - 1);\n        dirty(u, DIRTY_DOCK);', ''),
    ('Space does not open the actions', 'case KEY_SPACE: actions_open(u, u->focus); break;                        /* Y: More */', ''),
    ('Super+D does nothing', 'if ((mods & UI_SUPER) && code == KEY_D) dock_summon(u, false);', 'if (0) dock_summon(u, false);'),
    ('the dragged slider ignores the pointer', 'if (u->qs_sel == 2) { u->sys.volume = t; eff(u, EFF_SET_VOLUME, 0, 0, 0, t, NULL); }', 'if (0) { }'),
    ('A does not accept an offer', 'if (code == KEY_ENTER || code == KEY_KPENTER) toast_end(u, true);\n        else if (code == KEY_ESC) toast_end(u, false);\n        return;', 'if (code == KEY_ESC) toast_end(u, false);\n        return;'),
    ('B accepts an offer', 'else if (code == KEY_ESC) toast_end(u, false);\n        return;', 'else if (code == KEY_ESC) toast_end(u, true);\n        return;'),
    ('an offer lets keys through', 'else if (code == KEY_ESC) toast_end(u, false);\n        return;\n    }\n    if (u->menu)', 'else if (code == KEY_ESC) toast_end(u, false);\n    }\n    if (u->menu)'),
    ('a right click accepts an offer', 'toast_end(u, button == BTN_LEFT);', 'toast_end(u, true);'),
    ('a click on a plain toast does something', 'if (ev == PTR_DOWN && u->toast.on && u->toast.modal) toast_end', 'if (ev == PTR_DOWN && u->toast.on) toast_end'),
    ('asking for the dock leaves the offer', '|| !strcmp(cmd, "qs") || !strcmp(cmd, "osk"))) toast_end(u, false);', '|| !strcmp(cmd, "qs") || !strcmp(cmd, "osk"))) { }'),
    ('an offer is dismissed without being taken when it is accepted', 'if (accept && u->toast.modal && u->toast.label[0]) eff(', 'if (0 && accept && u->toast.modal && u->toast.label[0]) eff('),
    ('an offer is not modal', 'g.toast_modal = u->toast.on && u->toast.modal;', 'g.toast_modal = false;'),
    ('a plain toast keeps the offer\'s action', '    u->toast.modal = false;\n    u->toast.action = 0;', '    u->toast.action = 0;'),
    ('typing a key leaves the shell with the wrong code', 'if (!strcmp(kind, "char")) { if (ui_char_key(k.label[0], &code, &sh)) osk_type_code(u, code, sh); }', 'if (!strcmp(kind, "char")) { if (ui_char_key(k.label[0], &code, &sh)) osk_type_code(u, code, false); }'),
]

SYSINFO_MUTANTS = [
    ('a mute marker on another line counts', 'memmem(p, (size_t)(line_end(p) - p), "[MUTED]", 7) != NULL', 'strstr(p, "[MUTED]") != NULL'),
    ('the volume is not limited to full', '*volume = v > 1.f ? 1.f : v;', '*volume = v;'),
    ('a good signal is 50 %', 'percent >= 67 ? 3 : percent >= 34 ? 2', 'percent >= 50 ? 3 : percent >= 34 ? 2'),
    ('a weak signal is 0', 'percent >= 1 ? 1 : 0', 'percent >= 20 ? 1 : 0'),
    ('a colon in a network name splits it', "if (*p == '\\\\' && p + 1 < line_end(line))", 'if (0)'),
    ('SSH off shows an address', 'if (*on && p) {', 'if (p) {'),
    ('computer is host', 'if (!strncmp(m, "computer", 8)) return 1;', 'if (!strncmp(m, "computer", 8)) return 2;'),
    ('a powered line without a controller counts', '*on = *available && p &&', '*on = p &&'),
    ('a full battery is charging', 'strcmp(t, "Charging") == 0', 'strcmp(t, "Charging") == 0 || strcmp(t, "Full") == 0'),
    ('a capacity past 100 is not limited', '*charge = (float)(cap < 0 ? 0 : cap > 100 ? 100 : cap) / 100.f;', '*charge = (float)(cap < 0 ? 0 : cap) / 100.f;'),
    ('the brightness floor is ignored', 'if (level < floor) level = floor;', ''),
    ('brightness 0 can be written', 'if (raw < 1) raw = 1;', ''),
    ('a backlight with no maximum is used', 'return read_text(p, t, sizeof t) && atoi(t) > 0;', 'return read_text(p, t, sizeof t);'),
    ('the battery is the first power supply', 'return read_text(p, t, sizeof t) && strcmp(t, "Battery") == 0;', 'return read_text(p, t, sizeof t);'),
]


@unittest.skipUnless(shutil.which('gcc'), 'needs a native gcc')
class SysinfoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix='wisp-sysinfo-', dir=WORK / 'tmp'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def build(self, source, out, san=True):
        flags = ['-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined'] if san else ['-O1', '-w']
        return subprocess.run(['gcc', '-std=gnu11'] + flags + ['-I', str(DOCK), '-o', str(out), str(DOCK / 'test_sysinfo.c'), str(source), '-lm'], capture_output=True, text=True)

    def test_the_readers_pass_their_checks_under_the_sanitizers(self):
        r = self.build(DOCK / 'sysinfo.c', self.d / 'test_sysinfo'); self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([str(self.d / 'test_sysinfo')], capture_output=True, text=True, errors='replace', timeout=300, env={**os.environ, 'TMPDIR': str(self.d)})
        self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])
        self.assertRegex(run.stdout, r'\d{2,} checks, 0 failed')

    def test_every_rule_mutant_of_the_readers_is_caught(self):
        src = (DOCK / 'sysinfo.c').read_text(); survived = []
        for i, (what, old, new) in enumerate(SYSINFO_MUTANTS):
            self.assertEqual(src.count(old), 1, 'the mutant "%s" matches sysinfo.c %d times' % (what, src.count(old)))
            m = self.d / ('sysinfo-mutant%d.c' % i); m.write_text(src.replace(old, new))
            r = self.build(m, self.d / ('sysinfo-mutant%d' % i), san=False); self.assertEqual(r.returncode, 0, '"%s": %s' % (what, r.stderr[:300]))
            try: run = subprocess.run([str(self.d / ('sysinfo-mutant%d' % i))], capture_output=True, text=True, errors='replace', timeout=60, env={**os.environ, 'TMPDIR': str(self.d)})
            except subprocess.TimeoutExpired: continue
            if run.returncode == 0: survived.append(what)
        self.assertEqual(survived, [], 'the tests do not notice these changes to sysinfo.c')

    @unittest.skipUnless(shutil.which('aarch64-linux-gnu-gcc'), 'needs the aarch64 cross compiler')
    def test_it_builds_for_the_device_without_warnings(self):
        r = subprocess.run(['aarch64-linux-gnu-gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-c', '-o', os.devnull, str(DOCK / 'sysinfo.c')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

SWAYIPC_MUTANTS = [
    ('the frame has no length', 'memcpy(out + 6, &len, 4);', ''),
    ('a message is taken before it is whole', 'if (b->len < 14 + (size_t)len) return 0;', ''),
    ('a stream that is not IPC is accepted', 'if (b->len >= 6 && memcmp(b->buf, MAGIC, 6) != 0) { b->len = 0; return -1; }', ''),
    ('a payload is not cut to the room', 'size_t take = len < cap - 1 ? len : cap - 1;', 'size_t take = len;'),
    ('only a user-floated window floats', 'w->floating = !strcmp(fl, "auto_on") || !strcmp(fl, "user_on");', 'w->floating = !strcmp(fl, "user_on");'),
    ('the focus is ignored', 'w->focused = fo && !strncmp(fo, "true", 4);', 'w->focused = false;'),
    ('an escaped quote ends a string', "if (*s == '\\\\' && s[1]) { s++; continue; }", ''),
    ('a nested object is searched for the key', 's = skip_value(s);\n        if (!s) return NULL;\n        s = ws(s);', 's = s + 1;\n        if (!s) return NULL;\n        s = ws(s);'),
    ('a message too large is held', 'if (len > sizeof b->buf - 14) { b->len = 0; return -1; }', ''),
    ('a full buffer is not noticed', 'if (n > sizeof b->buf - b->len) return false;', ''),
]


@unittest.skipUnless(shutil.which('gcc'), 'needs a native gcc')
class SwayIpcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix='wisp-swayipc-', dir=WORK / 'tmp'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def build(self, source, out, san=True):
        flags = ['-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined'] if san else ['-O1', '-w']
        return subprocess.run(['gcc', '-std=gnu11'] + flags + ['-I', str(DOCK), '-o', str(out), str(DOCK / 'test_swayipc.c'), str(source)], capture_output=True, text=True)

    def test_the_ipc_reader_passes_its_checks_under_the_sanitizers(self):
        r = self.build(DOCK / 'swayipc.c', self.d / 'test_swayipc'); self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([str(self.d / 'test_swayipc')], capture_output=True, text=True, errors='replace', timeout=300)
        self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])
        self.assertRegex(run.stdout, r'\d{3,} checks, 0 failed')

    def test_every_rule_mutant_of_the_ipc_reader_is_caught(self):
        src = (DOCK / 'swayipc.c').read_text(); survived = []
        for i, (what, old, new) in enumerate(SWAYIPC_MUTANTS):
            self.assertEqual(src.count(old), 1, 'the mutant "%s" matches swayipc.c %d times' % (what, src.count(old)))
            m = self.d / ('swayipc-mutant%d.c' % i); m.write_text(src.replace(old, new))
            r = self.build(m, self.d / ('swayipc-mutant%d' % i), san=False); self.assertEqual(r.returncode, 0, '"%s": %s' % (what, r.stderr[:300]))
            try: run = subprocess.run([str(self.d / ('swayipc-mutant%d' % i))], capture_output=True, text=True, errors='replace', timeout=60)
            except subprocess.TimeoutExpired: continue
            if run.returncode == 0: survived.append(what)
        self.assertEqual(survived, [], 'the tests do not notice these changes to swayipc.c')

    @unittest.skipUnless(shutil.which('aarch64-linux-gnu-gcc'), 'needs the aarch64 cross compiler')
    def test_it_builds_for_the_device_without_warnings(self):
        r = subprocess.run(['aarch64-linux-gnu-gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-c', '-o', os.devnull, str(DOCK / 'swayipc.c')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

DOWNLOADS_MUTANTS = [
    ('a partial file is offered', 'static const char *const skip[] = { ".part", ".crdownload",', 'static const char *const skip[] = { ".crdownload",'),
    ('a hidden file is offered', "if (!*b || b[0] == '.') return true;", 'if (!*b) return true;'),
    ('arm and x86 are swapped', 'if (machine == 0xb7) return DL_APPIMAGE_ARM;\n        if (machine == 0x3e) return DL_APPIMAGE_X86;', 'if (machine == 0x3e) return DL_APPIMAGE_ARM;\n        if (machine == 0xb7) return DL_APPIMAGE_X86;'),
    ('a 32-bit ARM AppImage is offered', 'return DL_OTHER;                                                              /* an architecture this device cannot run at all */', 'return DL_APPIMAGE_ARM;'),
    ('a big-endian ELF is read as little-endian', 'unsigned machine = h[5] == 1 ? (unsigned)(h[18] | (h[19] << 8)) : (unsigned)((h[18] << 8) | h[19]);', 'unsigned machine = (unsigned)(h[18] | (h[19] << 8));'),
    ('an archive name over other bytes is an archive', 'if (is_archive_name(b) && is_archive_magic(h, n)) return DL_ARCHIVE;', 'if (is_archive_name(b)) return DL_ARCHIVE;'),
    ('a jar is an archive', 'static const char *const ext[] = { ".zip", ".7z",', 'static const char *const ext[] = { ".jar", ".zip", ".7z",'),
    ('any MZ file is a Windows program', 'h[1] == \'Z\' && (ends_with(b, ".exe") || ends_with(b, ".msi"))', 'h[1] == \'Z\''),
    ('a jpeg is not a picture', 'h[0] == 0xff && h[1] == 0xd8 && h[2] == 0xff', 'false'),
    ('a wav is a picture', 'if (n >= 12 && !memcmp(h, "RIFF", 4) && !memcmp(h + 8, "WEBP", 4)) return true;', 'if (n >= 12 && !memcmp(h, "RIFF", 4)) return true;'),
    ('a double extension leaves half of it', '".AppImage", ".tar.gz", ".tar.xz",', '".AppImage", ".tar.xz",'),
    ('control characters reach the toast', "    for (char *p = o->text; *p; p++) if ((unsigned char)*p < 0x20 || *p == 0x7f) *p = '?';\n", ''),
    ('the stem is not cut to the room', 'if (n >= cap) n = cap - 1;', ''),
    ('an exe is offered to Steam by name only', 'case DL_WINDOWS:\n        snprintf(o->title, sizeof o->title, "Windows program downloaded");', 'case DL_WINDOWS:\n        o->action = DL_ACT_INSTALL;\n        snprintf(o->title, sizeof o->title, "Windows program downloaded");'),
]


@unittest.skipUnless(shutil.which('gcc'), 'needs a native gcc')
class DownloadsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix='wisp-downloads-', dir=WORK / 'tmp'))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def build(self, source, out, san=True):
        flags = ['-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined'] if san else ['-O1', '-w']
        return subprocess.run(['gcc', '-std=gnu11'] + flags + ['-I', str(DOCK), '-o', str(out), str(DOCK / 'test_downloads.c'), str(source)], capture_output=True, text=True)

    def test_the_classifier_passes_its_checks_under_the_sanitizers(self):
        r = self.build(DOCK / 'downloads.c', self.d / 'test_downloads'); self.assertEqual(r.returncode, 0, r.stderr)
        run = subprocess.run([str(self.d / 'test_downloads')], capture_output=True, text=True, errors='replace', timeout=300)
        self.assertEqual(run.returncode, 0, run.stdout[-2000:] + run.stderr[-2000:])
        self.assertRegex(run.stdout, r'\d{2,} checks, 0 failed')

    def test_every_rule_mutant_of_the_classifier_is_caught(self):
        src = (DOCK / 'downloads.c').read_text(); survived = []
        for i, (what, old, new) in enumerate(DOWNLOADS_MUTANTS):
            self.assertEqual(src.count(old), 1, 'the mutant "%s" matches downloads.c %d times' % (what, src.count(old)))
            m = self.d / ('downloads-mutant%d.c' % i); m.write_text(src.replace(old, new))
            r = self.build(m, self.d / ('downloads-mutant%d' % i), san=False); self.assertEqual(r.returncode, 0, '"%s": %s' % (what, r.stderr[:300]))
            try: run = subprocess.run([str(self.d / ('downloads-mutant%d' % i))], capture_output=True, text=True, errors='replace', timeout=60)
            except subprocess.TimeoutExpired: continue
            if run.returncode == 0: survived.append(what)
        self.assertEqual(survived, [], 'the tests do not notice these changes to downloads.c')

    @unittest.skipUnless(shutil.which('aarch64-linux-gnu-gcc'), 'needs the aarch64 cross compiler')
    def test_it_builds_for_the_device_without_warnings(self):
        r = subprocess.run(['aarch64-linux-gnu-gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-c', '-o', os.devnull, str(DOCK / 'downloads.c')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)


@unittest.skipUnless(shutil.which('gcc'), 'needs a native gcc')
class UiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = Path(tempfile.mkdtemp(prefix='wisp-ui-', dir=WORK / 'tmp'))
        cls.exe = cls.d / 'test_ui'
        r = subprocess.run(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror', '-fsanitize=address,undefined', '-fno-sanitize-recover=undefined'] + INC +
                           ['-o', str(cls.exe), str(DOCK / 'test_ui.c')] + [str(DOCK / f) for f in SOURCES] + LIBS, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        cls.run_ = subprocess.run([str(cls.exe)], capture_output=True, text=True, errors='replace', timeout=600, env={**os.environ, 'TMPDIR': str(cls.d)})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.d, True)

    def test_all_checks_pass_with_no_sanitizer_report(self):
        self.assertEqual(self.run_.returncode, 0, self.run_.stdout[-3000:] + self.run_.stderr[-3000:])
        self.assertRegex(self.run_.stdout, r'\d{3,} checks, 0 failed')
        self.assertNotIn('runtime error', self.run_.stderr)
        self.assertNotIn('LeakSanitizer', self.run_.stderr)

    @unittest.skipUnless(shutil.which('aarch64-linux-gnu-gcc'), 'needs the aarch64 cross compiler')
    def test_it_builds_for_the_device_without_warnings(self):
        r = subprocess.run(['gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-fsyntax-only'] + INC + [str(DOCK / 'ui.c')], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_every_rule_mutant_is_caught(self):
        src = (DOCK / 'ui.c').read_text()
        survived, broken = [], []
        for i, (what, old, new) in enumerate(MUTANTS):
            self.assertEqual(src.count(old), 1, 'the mutant "%s" matches ui.c %d times, not once' % (what, src.count(old)))
            m = self.d / ('ui-mutant%d.c' % i); m.write_text(src.replace(old, new)); exe = self.d / ('ui-mutant%d' % i)
            r = subprocess.run(['gcc', '-std=gnu11', '-O1', '-w', '-I', str(DOCK)] + INC + ['-o', str(exe), str(DOCK / 'test_ui.c'), str(m)] + [str(DOCK / f) for f in SOURCES[1:]] + LIBS,
                               capture_output=True, text=True)
            if r.returncode != 0:
                broken.append('%s: %s' % (what, r.stderr[:200])); continue
            try:
                run = subprocess.run([str(exe)], capture_output=True, text=True, errors='replace', timeout=120, env={**os.environ, 'TMPDIR': str(self.d)})
            except subprocess.TimeoutExpired:
                continue
            if run.returncode == 0: survived.append(what)
        self.assertEqual(broken, [], 'mutants that do not compile test nothing')
        self.assertEqual(survived, [], 'the tests do not notice these changes to ui.c')


if __name__ == '__main__':
    unittest.main()
