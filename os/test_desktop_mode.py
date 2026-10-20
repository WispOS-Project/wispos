"""Desktop mode's wiring in the image (docs/WISPOS-OS-DESIGN.md "Desktop mode"; nothing here needs the device or a compositor): the session switch script and the compositor settings script are
RUN against fake systemctl, swaymsg and display tools and what they ask for is compared with what the design says; the units are checked for what keeps the two modes from running together and
what keeps desktop mode from starting by itself; the desktop entries are checked against the icon set and the shell's matching rules; and the build compiles, installs and checks the shell."""
import configparser, os, socket, subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE / 'rootfs'
WORK = Path(os.environ.get('WISP_WORK', '/mnt/hdd/wispos-staging/wispos-os-20261007'))
sys.path.insert(0, str(HERE / 'dock'))


def unit(name):
    c = configparser.ConfigParser(strict=False, interpolation=None, allow_no_value=True); c.optionxform = str
    c.read(ROOT / 'usr/lib/systemd/system' / name); return c


class SessionSelectTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix='wisp-ss-', dir=WORK / 'tmp'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.d, True))
        (self.d / 'systemctl').write_text('#!/bin/sh\necho "$@" >> "%s/calls"\n' % self.d); (self.d / 'systemctl').chmod(0o755)

    def run_select(self, *args, script='usr/bin/wisp-session-select'):
        return subprocess.run(['sh', str(ROOT / script), *args], capture_output=True, text=True, env={**os.environ, 'WISP_SYSTEMCTL': str(self.d / 'systemctl')})

    def calls(self):
        f = self.d / 'calls'; return f.read_text().splitlines() if f.exists() else []

    def test_desktop_and_game_start_the_units_without_waiting_for_them(self):
        for name in ('desktop', 'plasma', 'desktop-wayland'):
            self.assertEqual(self.run_select(name).returncode, 0, name)
        for name in ('game', 'gamescope', 'gamescope-wayland'):
            self.assertEqual(self.run_select(name).returncode, 0, name)
        self.assertEqual(self.calls(), ['start --no-block wisp-desktop.service'] * 3 + ['start --no-block wisp-steam.service'] * 3,
                         'no-block: the call may come from the very session that is being stopped')

    def test_anything_else_is_a_usage_error_and_starts_nothing(self):
        for args in ((), ('banana',), ('desktop', 'extra-but-fine') if False else ('',)):
            r = self.run_select(*args); self.assertEqual(r.returncode, 2, args); self.assertIn('usage', r.stderr)
        self.assertEqual(self.calls(), [])

    def test_steams_own_script_name_is_the_same_thing(self):
        text = (ROOT / 'usr/bin/steamos-session-select').read_text()
        self.assertIn('exec /usr/bin/wisp-session-select "$@"', text)
        for f in ('wisp-session-select', 'steamos-session-select', 'wisp-about'): self.assertTrue(os.access(ROOT / 'usr/bin' / f, os.X_OK), f)


class DesktopSessionScriptTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix='wisp-ds-', dir=WORK / 'tmp'))
        self.addCleanup(lambda: __import__('shutil').rmtree(self.d, True))
        self.sock_path = self.d / 'sway.sock'
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.srv.bind(str(self.sock_path)); self.addCleanup(self.srv.close)
        for name, body in (('swaymsg', 'echo "$1" >> "%s/sway.log"; [ -z "$FAIL" ] || exit 1' % self.d), ('display', 'echo "$1" >> "%s/display.log"' % self.d)):
            (self.d / name).write_text('#!/bin/sh\n' + body + '\n'); (self.d / name).chmod(0o755)

    def run_script(self, mode, **env):
        return subprocess.run(['sh', str(ROOT / 'usr/lib/wispos/wisp-desktop-session'), mode], capture_output=True, text=True, timeout=30,
                              env={**os.environ, 'SWAYSOCK': str(self.sock_path), 'WISP_SWAYMSG': str(self.d / 'swaymsg'), 'WISP_DISPLAY_TOOL': str(self.d / 'display'), 'WISP_SWAY_WAIT': '2', **env})

    def log(self, name):
        f = self.d / name; return f.read_text().splitlines() if f.exists() else []

    def test_start_makes_the_compositor_a_desktop(self):
        r = self.run_script('start'); self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.log('display.log'), ['native'], 'the dock and the keyboard are drawn for the panel\'s own 720x480')
        self.assertEqual(self.log('sway.log'), ['output DSI-1 bg /usr/share/wispos/wallpaper.png fill', 'seat * hide_cursor 0', 'focus_follows_mouse no',
                                            'input "0:0:wisp-pad_mouse" accel_profile flat', 'input "0:0:wisp-pad_mouse" pointer_accel 0',
                                            'input "0:0:wisp-pad_keyboard" repeat_delay 350', 'input "0:0:wisp-pad_keyboard" repeat_rate 25',
                                            'bindsym --release Super_L exec wisp-dock --ctl menu', 'bindsym Mod4+d exec wisp-dock --ctl summon', 'bindsym Mod4+s exec wisp-dock --ctl qs',
                                            'bindsym Mod4+k exec wisp-dock --ctl osk', 'bindsym Mod4+a exec wisp-dock --ctl actions'])

    def test_stop_puts_game_mode_back_the_way_it_was(self):
        self.assertEqual(self.run_script('stop').returncode, 0)
        self.assertEqual(self.log('display.log'), ['default'])
        self.assertEqual(self.log('sway.log'), ['output DSI-1 bg #000000 solid_color', 'seat * hide_cursor 1000', 'focus_follows_mouse yes', 'unbindsym --release Super_L',
                                                'unbindsym Mod4+d', 'unbindsym Mod4+s', 'unbindsym Mod4+k', 'unbindsym Mod4+a'])
        kiosk = (ROOT / 'usr/share/sway/config.kiosk').read_text() if (ROOT / 'usr/share/sway/config.kiosk').exists() else None
        if kiosk: self.assertIn('seat * hide_cursor 1000', kiosk, 'the value restored is the kiosk configuration\'s own')

    def test_a_refused_setting_is_reported_but_does_not_stop_the_mode(self):
        r = self.run_script('start', FAIL='1')
        self.assertEqual(r.returncode, 0, 'the desktop still starts')
        self.assertIn('sway refused', r.stderr); self.assertEqual(r.stderr.count('sway refused'), 12)

    def test_a_compositor_that_never_comes_is_a_failure_with_the_reason(self):
        self.sock_path.unlink()
        r = self.run_script('start'); self.assertEqual(r.returncode, 1); self.assertIn('never appeared', r.stderr)
        self.assertEqual(self.log('sway.log'), [])

    def test_usage(self):
        for mode in ('', 'restart'):
            r = self.run_script(mode); self.assertEqual(r.returncode, 2, mode); self.assertIn('usage', r.stderr)

    def test_the_output_and_wallpaper_can_be_changed_for_another_panel(self):
        r = self.run_script('start', WISP_OUTPUT='HDMI-A-1', WISP_WALLPAPER='/x/y.png'); self.assertEqual(r.returncode, 0)
        self.assertEqual(self.log('sway.log')[0], 'output HDMI-A-1 bg /x/y.png fill')


class UnitTests(unittest.TestCase):
    def test_the_two_modes_exclude_each_other_in_both_directions(self):
        self.assertEqual(unit('wisp-desktop.service')['Unit']['Conflicts'], 'wisp-steam.service')
        self.assertEqual(unit('wisp-steam.service')['Unit']['Conflicts'], 'wisp-desktop.service')
        self.assertIn('wisp-steam.service', unit('wisp-desktop.service')['Unit']['After'].split(), 'ordered after the stop of Steam, so the display is free when the desktop starts')

    def test_desktop_mode_holds_itself_runs_the_compositor_settings_and_pulls_in_the_pad_and_the_shell(self):
        u = unit('wisp-desktop.service')
        self.assertEqual(u['Service']['Type'], 'oneshot'); self.assertEqual(u['Service']['RemainAfterExit'], 'yes')
        self.assertEqual(u['Service']['ExecStart'], '/usr/lib/wispos/wisp-desktop-session start')
        self.assertEqual(u['Service']['ExecStop'], '/usr/lib/wispos/wisp-desktop-session stop')
        self.assertEqual(set(u['Unit']['Wants'].split()), {'wisp-pad.service', 'wisp-dock.service'})
        self.assertNotIn('Install', u.sections(), 'it is started by the switch, never at boot')

    def test_nothing_of_desktop_mode_is_enabled_at_boot(self):
        for d in (ROOT / 'usr/lib/systemd/system').glob('*.wants'):
            for n in ('wisp-desktop.service', 'wisp-dock.service', 'wisp-pad.service'):
                self.assertFalse((d / n).exists(), '%s is enabled in %s' % (n, d.name))
        for n in ('wisp-dock.service', 'wisp-pad.service'): self.assertNotIn('Install', unit(n).sections())

    def test_the_shell_and_the_pad_end_with_the_mode_and_the_shell_takes_its_programs_with_it(self):
        dock, pad = unit('wisp-dock.service'), unit('wisp-pad.service')
        self.assertEqual(dock['Unit']['PartOf'], 'wisp-desktop.service'); self.assertEqual(pad['Unit']['PartOf'], 'wisp-desktop.service')
        self.assertEqual(dock['Service']['KillMode'], 'control-group', 'every program started from the dock is in this group: leaving the mode must end them (memory)')
        self.assertIn('wisp-desktop.service', dock['Unit']['After'].split())
        self.assertEqual(dock['Service']['ExecStart'], '/usr/bin/wisp-dock')
        env = (ROOT / 'usr/lib/systemd/system/wisp-dock.service').read_text()               # several Environment= lines: read them all
        for want in ('WAYLAND_DISPLAY=wayland-1', 'XDG_RUNTIME_DIR=/var/run/0-runtime-dir', 'SWAYSOCK=/var/run/0-runtime-dir/sway-ipc.0.sock', 'MOZ_ENABLE_WAYLAND=1'): self.assertIn(want, env)

    def test_programs_started_from_the_dock_get_a_locale_the_image_has(self):
        """Device, 2026-10-15: the systemd manager's LANG=C.UTF-8 reached the dock and the terminal printed "foot: invalid locale, falling back to 'en_US.UTF-8'" on every start: the image
        installs C, POSIX and en_US.UTF-8, not C.UTF-8. Games keep their own LANG from Steam; this is the desktop mode's only."""
        text = (ROOT / 'usr/lib/systemd/system/wisp-dock.service').read_text()
        lang = [l.split('LANG=', 1)[1].split()[0] for l in text.splitlines() if l.startswith('Environment=') and ' LANG=' in ' ' + l.split('=', 1)[1]]
        self.assertEqual(lang, ['en_US.UTF-8'])
        archive = WORK / 'rootfs/tree/usr/lib/locale/locale-archive'
        if archive.is_file(): self.assertIn(b'en_US.utf8', archive.read_bytes(), 'the locale the unit names is in the staged image')

    def test_a_shell_that_cannot_stay_up_sends_the_device_back_to_game_mode(self):
        dock = unit('wisp-dock.service')
        self.assertEqual(dock['Unit']['OnFailure'], 'wisp-steam.service')
        self.assertEqual(dock['Unit']['StartLimitBurst'], '4'); self.assertEqual(dock['Unit']['StartLimitIntervalSec'], '60')
        self.assertEqual(dock['Service']['Restart'], 'on-failure')


class DesktopEntryTests(unittest.TestCase):
    def entries(self):
        out = {}
        for f in sorted((ROOT / 'usr/share/applications').glob('*.desktop')):
            c = configparser.ConfigParser(strict=False, interpolation=None); c.optionxform = str; c.read(f); out[f.name] = c['Desktop Entry']
        return out

    def test_there_is_the_whole_set_and_each_entry_is_complete(self):
        e = self.entries()
        self.assertEqual(set(e), {'wisp-%s.desktop' % n for n in ('browser', 'terminal', 'settings', 'images', 'monitor', 'about', 'game')})
        for name, d in e.items():
            self.assertEqual(d['Type'], 'Application', name); self.assertTrue(d['Name'] and d['Exec'], name)
            self.assertNotIn('NoDisplay', d, name)

    def test_each_icon_is_a_glyph_of_the_set_the_shell_has(self):
        import gen_glyphs
        glyphs = {w[0] for w in gen_glyphs.WANT}
        for name, d in self.entries().items():
            self.assertTrue(d['Icon'].startswith('wisp-') and d['Icon'][5:] in glyphs, '%s: %s' % (name, d['Icon']))
            self.assertEqual('wisp-' + d['Icon'][5:] + '.desktop', name, 'the glyph is named after the entry')
            for size in (18, 22, 44):
                self.assertIn((d['Icon'][5:], size), {(w[0], s) for w in gen_glyphs.WANT for s in w[1]}, '%s at %d px' % (d['Icon'], size))

    def test_a_window_is_matched_to_its_entry_by_the_class_the_program_gives(self):
        e = self.entries()
        self.assertEqual(e['wisp-terminal.desktop']['StartupWMClass'], 'foot')
        self.assertEqual(e['wisp-images.desktop']['StartupWMClass'], 'swayimg')
        self.assertEqual(e['wisp-browser.desktop']['StartupWMClass'], 'wisp-browser', 'wisp-browser starts Firefox with --class wisp-browser')
        # a second use of the terminal program has its own application ID, so the dock does not show a monitor window as the terminal
        self.assertIn('--app-id=wisp-monitor', e['wisp-monitor.desktop']['Exec']); self.assertEqual(e['wisp-monitor.desktop']['StartupWMClass'], 'wisp-monitor')
        self.assertIn('--app-id=wisp-about', e['wisp-about.desktop']['Exec']); self.assertEqual(e['wisp-about.desktop']['StartupWMClass'], 'wisp-about')

    def test_the_rocknix_entries_are_pruned_and_ours_are_not(self):
        prune = (HERE / 'rootfs-prune.txt').read_text().splitlines()
        for n in ('cups', 'htop', 'mpv', 'nvtop', 'org.freedesktop.Xwayland', 'swayimg', 'wine'): self.assertIn('usr/share/applications/%s.desktop' % n, prune)
        self.assertFalse([l for l in prune if not l.startswith('#') and 'wisp-' in l and 'applications' in l])

    def test_every_program_an_entry_runs_is_in_the_image_or_ours(self):
        tree = WORK / 'rootfs/tree/usr/bin'
        if not (tree / 'foot').exists(): self.skipTest('no built SYSTEM tree')
        ours = {'wisp-browser', 'wisp-dock', 'wisp-session-select', 'wisp-about'}
        for name, d in self.entries().items():
            prog = d['Exec'].split()[0]
            self.assertTrue(prog in ours or (tree / prog).exists() or (ROOT / 'usr/bin' / prog).exists(), '%s runs %s' % (name, prog))
            for word in d['Exec'].split()[1:]:
                if not word.startswith('-') and word not in ('--ctl', 'qs', 'settings', 'game'): self.assertTrue(word in ours or (tree / word).exists() or (ROOT / 'usr/bin' / word).exists(), '%s runs %s' % (name, word))


class MimeDatabaseTests(unittest.TestCase):
    """The wallpaper was black on the device (2026-10-15): swaybg logged "Couldn't recognize the image file format" because the image carried only /usr/share/mime/packages and
    never the generated database, which this gdk-pixbuf (2.42) needs to recognise a PNG. The build must generate it, and must not ship a tree without it."""
    PNG_XML = ('<?xml version="1.0"?><mime-info xmlns="http://www.freedesktop.org/standards/shared-mime-info"><mime-type type="image/png"><comment>PNG image</comment>'
               '<magic priority="50"><match type="string" value="\\x89PNG" offset="0"/></magic><glob pattern="*.png"/></mime-type></mime-info>')

    def setUp(self):
        sys.path.insert(0, str(HERE))
        import stage_rootfs
        self.stage = stage_rootfs
        self.tmp = tempfile.TemporaryDirectory(); self.tree = Path(self.tmp.name)
    def tearDown(self): self.tmp.cleanup()

    def test_the_tree_gets_a_generated_database_that_knows_png(self):
        packages = self.tree / 'usr/share/mime/packages'; packages.mkdir(parents=True)
        (packages / 'freedesktop.org.xml').write_text(self.PNG_XML)
        self.stage.install_mime_database(self.tree)
        cache = self.tree / 'usr/share/mime/mime.cache'
        self.assertTrue(cache.is_file(), 'mime.cache is generated from the tree\'s own packages')
        self.assertIn(b'image/png', cache.read_bytes())

    def test_a_tree_with_nothing_to_build_it_from_fails_loudly(self):
        (self.tree / 'usr/share/mime').mkdir(parents=True)
        with self.assertRaises(SystemExit): self.stage.install_mime_database(self.tree)

    def test_the_build_runs_it_and_the_check_demands_the_result(self):
        self.assertIn('install_mime_database(tree)', (HERE / 'stage_rootfs.py').read_text().split('def main(')[1])
        self.assertIn('usr/share/mime/mime.cache', (HERE / 'check_rootfs.py').read_text())


class BuildWiringTests(unittest.TestCase):
    def test_the_build_compiles_installs_and_checks_the_shell(self):
        build = (HERE / 'build-rootfs.sh').read_text()
        line = [l for l in build.splitlines() if 'wisp-dock' in l and 'aarch64-linux-gnu-gcc' in l][0]
        self.assertIn('-Werror', line)
        block = build[build.index('dock/prepare-build.sh'):build.index('export WISP_DOCK_BIN')]
        for src in ('shell.c', 'ui.c', 'view.c', 'render.c', 'model.c', 'sysinfo.c', 'swayipc.c', 'downloads.c'): self.assertIn('dock/' + src, block, src)
        for lib in ('libwayland-client.so.0', 'libfreetype.so.6', 'libpng16.so.16'): self.assertIn(lib, block, 'linked against the image\'s own ' + lib)
        for proto in ('wlr-layer-shell', 'wlr-foreign-toplevel-management', 'xdg-shell', 'cursor-shape-v1', 'tablet-v2'): self.assertIn(proto, block)
        stage = (HERE / 'stage_rootfs.py').read_text()
        self.assertIn("WISP_DOCK_BIN", stage); self.assertIn("usr/bin/wisp-dock", stage); self.assertIn("dock/design/wallpaper.png", stage)
        check = (HERE / 'check_rootfs.py').read_text()
        for f in ('usr/bin/wisp-dock', 'usr/lib/systemd/system/wisp-dock.service', 'usr/lib/systemd/system/wisp-desktop.service', 'usr/lib/wispos/wisp-desktop-session',
                  'usr/bin/wisp-session-select', 'usr/share/wispos/wallpaper.png'): self.assertIn(f, check, f)


if __name__ == '__main__':
    unittest.main()
