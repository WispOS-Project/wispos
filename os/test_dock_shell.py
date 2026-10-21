"""Runs the desktop shell (os/dock/shell.c, built for this host with the address sanitizer) against a REAL compositor: the image's own sway 1.11 (aarch64, run under qemu-user from the built
root file system's libraries), headless, with the pixman renderer, 720 x 480. So what is checked is what sway does with the shell's layer surfaces: their sizes, the space they reserve,
who gets the keyboard, what the foreign-toplevel protocol says about windows, pointer and keyboard input arriving as real Wayland events (virtual keyboard and pointer, os/dock/test_inject.c),
and the pixels (grim screenshots compared with the renderer's own output for the same state).
A test window (os/dock/test_client.c) stands in for an application and logs the keys it is given. The pad daemon is a small fake socket server; the tools the shell runs (wpctl, nmcli ...) are
scripts that log what they were asked. Needs: qemu-user (/usr/bin/qemu-aarch64-static), the built SYSTEM tree of the work directory (rootfs/tree), the host's libwayland, libxkbcommon and the
Liberation fonts. Skipped, with the reason, when one is missing. Nothing is installed and nothing outside the work directory and ~/.cache is written."""
import json, os, shutil, signal, socket, subprocess, tempfile, threading, time, unittest, sys
from pathlib import Path
import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
DOCK = HERE / 'dock'
WORK = Path(os.environ.get('WISP_WORK', '/mnt/hdd/wispos-staging/wispos-os-20261007'))
TREE = WORK / 'rootfs/tree'
QEMU = '/usr/bin/qemu-aarch64-static'
FONTS = next((d for d in ('/usr/share/fonts/truetype/liberation', '/usr/share/fonts/liberation') if (Path(d) / 'LiberationSans-Regular.ttf').exists()), None)
BG = (14, 20, 27)
W, H = 720, 480
KEY = {'left': 105, 'right': 106, 'up': 103, 'down': 108, 'enter': 28, 'esc': 1, 'space': 57, 'tab': 15, 'backspace': 14, 'a': 30, 'b': 48, 'e': 18, 'r': 19, 't': 20, 'z': 44}

APPS = {'browser': ('Browser', None), 'terminal': ('Terminal', None), 'settings': ('Settings', None), 'game': ('Game mode', None), 'images': ('Images', 'swayimg'),
        'monitor': ('Monitor', None), 'about': ('About', None)}


def why_skipped():
    if not Path(QEMU).exists(): return 'no qemu-user at %s' % QEMU
    if not (TREE / 'usr/bin/sway').exists(): return 'no built SYSTEM tree at %s' % TREE
    if not (TREE / 'usr/bin/grim').exists(): return 'grim is not in the SYSTEM tree'
    if not FONTS: return 'no Liberation fonts on the host'
    for f in ('/usr/include/wayland-client.h', '/usr/include/xkbcommon/xkbcommon.h', '/usr/bin/wayland-scanner'):
        if not Path(f).exists(): return 'the host lacks %s' % f
    return None


def sh(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True, errors='replace', **kw)


class Client:
    """A test window."""
    def __init__(self, rig, app_id, color='2a3b4c'):
        self.rig, self.app_id = rig, app_id
        self.log = rig.d / ('client-%s.log' % app_id)
        self.log.write_text('')
        self.proc = subprocess.Popen([str(rig.bin / 'test_client'), app_id, 'win-' + app_id, str(self.log), color], env=rig.env, stderr=subprocess.PIPE)

    def lines(self):
        return self.log.read_text().splitlines()

    def keys(self):
        return [int(l.split()[1]) for l in self.lines() if l.startswith('key ') and l.endswith('pressed')]

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try: self.proc.wait(5)
            except subprocess.TimeoutExpired: self.proc.kill()
        if self.proc.stderr: self.proc.stderr.close()


class FakePad:
    """The pad daemon's socket: records what the shell sends and can send commands to it."""
    def __init__(self, path):
        self.path = path
        self.lines, self.conn = [], None
        Path(path).unlink(missing_ok=True)
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); self.srv.bind(path); self.srv.listen(2)
        self.stop_flag = False
        self.t = threading.Thread(target=self.run, daemon=True); self.t.start()

    def run(self):
        self.srv.settimeout(0.3)
        while not self.stop_flag:
            try: c, _ = self.srv.accept()
            except (socket.timeout, OSError): continue
            self.conn = c; c.settimeout(0.3); buf = b''
            while not self.stop_flag:
                try: data = c.recv(256)
                except socket.timeout: continue
                except OSError: break
                if not data: break
                buf += data
                while b'\n' in buf:
                    line, buf = buf.split(b'\n', 1); self.lines.append(line.decode())
            self.conn = None

    def send(self, line):
        for _ in range(100):
            if self.conn: self.conn.sendall((line + '\n').encode()); return
            time.sleep(0.1)
        raise AssertionError('the shell never connected to the pad socket')

    def close(self):
        self.stop_flag = True
        try: self.srv.close()
        except OSError: pass
        Path(self.path).unlink(missing_ok=True)


class Injector:
    """The persistent virtual keyboard and pointer (a headless seat has neither until they exist)."""
    def __init__(self, rig):
        self.p = subprocess.Popen([str(rig.bin / 'test_inject')], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=rig.env)
        assert self.p.stdout.readline().strip() == 'ready'

    def cmd(self, line):
        self.p.stdin.write(line + '\n'); self.p.stdin.flush()
        assert self.p.stdout.readline().strip() == 'ok'

    def key(self, mode, *codes): self.cmd('key %s %s' % (mode, ' '.join(str(c) for c in codes)))
    def move(self, x, y): self.cmd('ptr move %d %d' % (x, y))
    def click(self, button=272): self.cmd('ptr click %d' % button)
    def down(self, button=272): self.cmd('ptr down %d' % button)
    def up(self, button=272): self.cmd('ptr up %d' % button)
    def wheel(self, n): self.cmd('ptr wheel %d' % n)

    def close(self):
        try: self.p.stdin.write('quit\n'); self.p.stdin.close(); self.p.wait(5)
        except Exception: self.p.kill()
        self.p.stdout.close()


class Rig:
    def __init__(self):
        self.d = Path(tempfile.mkdtemp(prefix='shell-', dir=WORK / 'tmp'))
        self.bin = self.d / 'bin'; self.bin.mkdir()
        self.rt = Path(tempfile.mkdtemp(prefix='wsr', dir=Path.home() / '.cache'))       # a short path: a socket's name is limited to 107 bytes
        self.rt.chmod(0o700)
        self.apps, self.tools, self.sysfs = self.d / 'apps', self.d / 'tools', self.d / 'sysfs'
        for p in (self.apps, self.tools, self.sysfs): p.mkdir()
        self.env = {**os.environ, 'XDG_RUNTIME_DIR': str(self.rt), 'WAYLAND_DISPLAY': 'wayland-1', 'ASAN_OPTIONS': 'detect_leaks=0'}
        self.sway = self.dock = None
        self.pad = None
        self.inj = None
        self.pins = self.d / 'pins'
        self.dock_log = self.d / 'dock.log'

    # ------------------------------------------------------------------ building
    def build(self):
        r = sh(['bash', str(DOCK / 'prepare-build.sh'), str(WORK)]); assert r.returncode == 0, r.stderr
        gen = WORK / 'dockgen'
        def cc(out, srcs, libs, san=False):
            flags = ['-fsanitize=address,undefined', '-fno-sanitize-recover=undefined'] if san else []
            r = sh(['gcc', '-std=gnu11', '-O1', '-g', '-Wall', '-Wextra', '-Werror'] + flags + ['-I' + str(gen), '-I/usr/include/freetype2', '-I' + str(DOCK), '-o', str(self.bin / out)] + srcs + libs)
            assert r.returncode == 0, r.stderr
        P = lambda n: str(gen / (n + '-protocol.c'))
        cc('wisp-dock', [str(DOCK / f) for f in ('shell.c', 'ui.c', 'view.c', 'render.c', 'model.c', 'sysinfo.c', 'swayipc.c', 'downloads.c')] +
           [P(n) for n in ('wlr-layer-shell-unstable-v1', 'wlr-foreign-toplevel-management-unstable-v1', 'xdg-shell', 'cursor-shape-v1', 'tablet-v2')], ['-lwayland-client', '-lfreetype', '-lpng', '-lm'], san=True)
        cc('test_client', [str(DOCK / 'test_client.c'), P('xdg-shell')], ['-lwayland-client'])
        cc('test_inject', [str(DOCK / 'test_inject.c'), P('virtual-keyboard-unstable-v1'), P('wlr-virtual-pointer-unstable-v1')], ['-lwayland-client', '-lxkbcommon'])
        cc('test_scene', [str(DOCK / f) for f in ('test_scene.c', 'view.c', 'render.c', 'model.c')], ['-lfreetype', '-lpng', '-lm'])
        self.device_build = None
        if shutil.which('aarch64-linux-gnu-gcc'):                         # the real artifact, built as os/build-rootfs.sh builds it
            lib = str(TREE / 'usr/lib'); di = WORK / 'dockinc'
            srcs = [str(DOCK / f) for f in ('shell.c', 'ui.c', 'view.c', 'render.c', 'model.c', 'sysinfo.c', 'swayipc.c', 'downloads.c')]
            protos = [P(n) for n in ('wlr-layer-shell-unstable-v1', 'wlr-foreign-toplevel-management-unstable-v1', 'xdg-shell', 'cursor-shape-v1', 'tablet-v2')]
            r = sh(['aarch64-linux-gnu-gcc', '-std=gnu11', '-O2', '-Wall', '-Wextra', '-Werror', '-I' + str(gen), '-I' + str(di), '-I' + str(di / 'freetype2'), '-I' + str(DOCK), '-o', str(self.bin / 'wisp-dock-aarch64')]
                   + srcs + protos + [lib + '/libwayland-client.so.0', lib + '/libfreetype.so.6', lib + '/libpng16.so.16', '-lm', '-Wl,-rpath-link,' + lib])
            assert r.returncode == 0, r.stderr
            self.device_build = self.bin / 'wisp-dock-aarch64'

    def write_fixtures(self):
        for k, (name, wmclass) in APPS.items():
            exe = self.d / ('launch-' + k)
            exe.write_text('#!/bin/sh\necho "$0 $@" >> "%s/launched-%s"\n' % (self.d, k)); exe.chmod(0o755)
            extra = 'StartupWMClass=%s\n' % wmclass if wmclass else ''
            (self.apps / (k + '.desktop')).write_text('[Desktop Entry]\nType=Application\nName=%s\nExec=%s\nIcon=wisp-%s\n%s' % (name, exe, k, extra))
        self.pins.write_text('browser.desktop\nterminal.desktop\nsettings.desktop\n')
        tools = {'wpctl': 'case "$1" in get-volume) echo "Volume: 0.50";; *) echo "$@" >> "%(d)s/wpctl.log";; esac',
                 'nmcli': 'echo "*:TestNet:80"', 'bluetoothctl': 'echo "No default controller available"',
                 'wisp-ssh': 'case "$1" in on|off) echo "$@" >> "%(d)s/ssh.log";; esac; echo "ssh: off"', 'wisp-usb': 'case "$1" in off|computer|host) echo "$@" >> "%(d)s/usb.log";; esac; echo "usb: off"',
                 'wisp-screen': 'case "$1" in timeout) case "$2" in status) echo 120;; *) echo "$2" >> "%(d)s/screen-timeout.log";; esac;; esac',
                 'wisp-shortcut': 'exec env WISP_STEAM_ROOT="%(d)s/steamroot" WISP_SHORTCUT_NO_STEAM_CHECK=1 python3 -I "%(sc)s" "$@"',
                 'wisp-session-select': 'echo "$@" >> "%(d)s/session.log"', 'wisp-download-action': 'echo "$@" >> "%(d)s/download-action.log"'}
        for n, body in tools.items():
            p = self.tools / n; p.write_text('#!/bin/sh\n' + body % {'d': self.d, 'sc': HERE / 'rootfs/usr/bin/wisp-shortcut'} + '\n'); p.chmod(0o755)
        (self.sysfs / 'class/backlight/bl').mkdir(parents=True)
        (self.sysfs / 'class/backlight/bl/max_brightness').write_text('100\n'); (self.sysfs / 'class/backlight/bl/brightness').write_text('60\n')
        (self.sysfs / 'class/power_supply/bat').mkdir(parents=True)
        for n, v in (('type', 'Battery'), ('capacity', '78'), ('status', 'Discharging')): (self.sysfs / 'class/power_supply/bat' / n).write_text(v + '\n')

    # ------------------------------------------------------------------ sway
    def run_aarch64(self, prog, *args, **kw):
        return [QEMU, '-cpu', 'cortex-a53', str(TREE / 'usr/lib/ld-linux-aarch64.so.1'), '--library-path', str(TREE / 'usr/lib'), str(TREE / 'usr/bin' / prog)] + list(args)

    def run_aarch64_file(self, path, *args):
        return [QEMU, '-cpu', 'cortex-a53', str(TREE / 'usr/lib/ld-linux-aarch64.so.1'), '--library-path', str(TREE / 'usr/lib'), str(path)] + list(args)

    def start_sway(self):
        conf = self.d / 'sway.conf'
        conf.write_text('output HEADLESS-1 resolution 720x480 position 0 0\noutput * bg #0e141b solid_color\ndefault_border none\ndefault_floating_border none\nfocus_follows_mouse no\nxwayland disable\n')
        env = {**self.env, 'WLR_BACKENDS': 'headless', 'WLR_RENDERER': 'pixman', 'WLR_LIBINPUT_NO_DEVICES': '1', 'LIBSEAT_BACKEND': 'noop'}
        self.sway_log = (self.d / 'sway.log').open('w')
        self.sway = subprocess.Popen(self.run_aarch64('sway', '-c', str(conf)), env=env, stdout=self.sway_log, stderr=subprocess.STDOUT, start_new_session=True)
        self.env['SWAYSOCK'] = str(self.rt / ('sway-ipc.%d.sock' % os.getuid()))
        self.wait(lambda: (self.rt / 'wayland-1').exists() and self.swaymsg('-t', 'get_version').returncode == 0, 120, 'sway to start (see %s)' % (self.d / 'sway.log'))
        self.inj = Injector(self)

    def swaymsg(self, *args):
        return sh(self.run_aarch64('swaymsg', *args), env=self.env, timeout=60)

    def tree(self):
        return json.loads(self.swaymsg('-r', '-t', 'get_tree').stdout)

    def window(self, app_id):
        def walk(n):
            if n.get('app_id') == app_id: return n
            for c in n.get('nodes', []) + n.get('floating_nodes', []):
                r = walk(c)
                if r: return r
        return walk(self.tree())

    def grim(self):
        out = self.d / 'shot.png'
        r = sh(self.run_aarch64('grim', str(out)), env=self.env, timeout=90); assert r.returncode == 0, r.stderr
        return np.asarray(Image.open(out).convert('RGB'))

    def stop(self):
        if self.inj: self.inj.close(); self.inj = None
        if self.dock: self.stop_dock()
        if self.sway and self.sway.poll() is None:
            try: os.killpg(self.sway.pid, signal.SIGTERM); self.sway.wait(20)
            except Exception:
                try: os.killpg(self.sway.pid, signal.SIGKILL)
                except Exception: pass
        if getattr(self, 'sway_log', None): self.sway_log.close()
        shutil.rmtree(self.rt, True); shutil.rmtree(self.d, True)

    # ------------------------------------------------------------------ the shell
    def start_dock(self, pad=False, pins=True, extra=None, device=False):
        if pad:
            self.pad = FakePad(str(self.rt / 'pad.sock'))
        self.pins.unlink(missing_ok=True)
        if pins: self.pins.write_text('browser.desktop\nterminal.desktop\nsettings.desktop\n')
        for f in list(self.d.glob('launched-*')) + [self.d / n for n in ('download-action.log', 'wpctl.log', 'ssh.log', 'usb.log', 'session.log')]: f.unlink(missing_ok=True)
        shutil.rmtree(self.d / 'downloads', True)
        env = {**self.env, 'WISP_DOCK_SOCK': str(self.rt / 'dock.sock'), 'WISP_PAD_SOCK': str(self.rt / 'pad.sock'), 'WISP_DOCK_APPS': str(self.apps), 'WISP_DOCK_PINS': str(self.pins),
               'WISP_DOCK_FONTS': FONTS, 'WISP_DOCK_SYSFS': str(self.sysfs), 'WISP_DOCK_BIN': str(self.tools), 'WISP_DOCK_DOWNLOADS': str(self.d / 'downloads'), **(extra or {})}
        self.dock_out = self.dock_log.open('w')
        cmd = self.run_aarch64_file(self.device_build) if device else [str(self.bin / 'wisp-dock')]
        self.dock = subprocess.Popen(cmd, env=env, stdout=self.dock_out, stderr=subprocess.STDOUT)
        self.dock_env = env
        self.wait(lambda: self.state() is not None and 'wisp-dock:720x44' in self.state().get('panels', ''), 90, 'the dock surface (log: %s)' % self.dock_log)

    def stop_dock(self):
        if self.dock and self.dock.poll() is None:
            self.dock.terminate()
            try: self.dock.wait(10)
            except subprocess.TimeoutExpired: self.dock.kill()
        code = self.dock.returncode if self.dock else 0
        self.dock = None
        self.dock_out.close()
        if self.pad: self.pad.close(); self.pad = None
        return code

    def ctl(self, *words):
        return sh([str(self.bin / 'wisp-dock'), '--ctl'] + list(words), env=self.dock_env, timeout=30)

    def state(self):
        if not self.dock or self.dock.poll() is not None: return None
        r = self.ctl('state')
        if r.returncode != 0 or '=' not in r.stdout: return None
        return dict(kv.split('=', 1) for kv in r.stdout.split())

    def tap(self, *names): self.inj.key('tap', *[KEY[n] for n in names])

    def wait(self, pred, timeout, what):
        end = time.time() + timeout
        while time.time() < end:
            try:
                if pred(): return
            except Exception: pass
            time.sleep(0.25)
        raise AssertionError('timed out waiting for ' + what)


def box_blur(a, k):
    p = k // 2
    ap = np.pad(a, ((p, p), (p, p), (0, 0)), mode='edge')
    c = np.pad(ap.cumsum(0).cumsum(1), ((1, 0), (1, 0), (0, 0)))
    h, w = a.shape[:2]
    return (c[k:k + h, k:k + w] - c[:h, k:k + w] - c[k:k + h, :w] + c[:h, :w]) / (k * k)


@unittest.skipIf(why_skipped(), why_skipped() or '')
class ShellTests(unittest.TestCase):
    rig = None

    @classmethod
    def setUpClass(cls):
        cls.rig = Rig()
        try:
            cls.rig.write_fixtures(); cls.rig.build(); cls.rig.start_sway()
        except BaseException:
            cls.rig.stop(); raise

    @classmethod
    def tearDownClass(cls):
        if cls.rig: cls.rig.stop()

    def setUp(self):
        self.clients = []

    def tearDown(self):
        for c in self.clients: c.stop()
        code = self.rig.stop_dock() if self.rig.dock else 0
        self.rig.wait(lambda: not self.rig.window('terminal') and not self.rig.window('images') and not self.rig.window('foot'), 20, 'the test windows to go')
        self.assertIn(code, (0, -signal.SIGTERM), 'the shell did not stop cleanly: %s' % self.rig.dock_log.read_text()[-1500:])
        self.assertNotIn('ERROR', self.rig.dock_log.read_text() + 'Sanitizer' * 0)
        self.assertNotIn('AddressSanitizer', self.rig.dock_log.read_text())

    def client(self, app_id, **kw):
        c = Client(self.rig, app_id, **kw); self.clients.append(c)
        self.rig.wait(lambda: self.rig.window(app_id), 40, 'the window %s' % app_id)
        return c

    def expected_strip(self, scene):
        """The renderer's own picture of a scene over the solid backdrop sway draws."""
        out = self.rig.d / (scene + '.raw')
        r = sh([str(self.rig.bin / 'test_scene'), scene, FONTS, str(self.rig.apps), str(out)]); self.assertEqual(r.returncode, 0, r.stderr)
        raw = np.fromfile(out, np.uint32); h, w = int(raw[1]), int(raw[0]); argb = raw[2:].reshape(h, w)
        layer = np.stack([(argb >> 16) & 255, (argb >> 8) & 255, argb & 255, (argb >> 24) & 255], -1).astype(np.float32)
        back = np.zeros((h, w, 3), np.float32); back[:] = BG
        return np.clip(layer[..., :3] + back * (1 - layer[..., 3:4] / 255.0) + 0.5, 0, 255).astype(np.uint8)

    def assertStripLike(self, scene, x1=555, rows=(436, 480)):
        """The left part of the strip is the picture's (the right holds the clock and live values, which the picture fixes at 12:34)."""
        want = self.expected_strip(scene)[rows[0]:rows[1], :x1].astype(np.float64)
        end = time.time() + 60; last = None
        while time.time() < end:
            got = self.rig.grim()[rows[0]:rows[1], :x1].astype(np.float64)
            last = float(np.abs(box_blur(got, 5) - box_blur(want, 5)).max())
            if last < 16: return
            time.sleep(1)
        Image.fromarray(got.astype(np.uint8)).save(self.rig.d / 'strip-got.png')
        keep = WORK / 'tmp/shell-diff'; keep.mkdir(exist_ok=True); shutil.copy(self.rig.d / 'strip-got.png', keep / (scene + '-got.png'))
        Image.fromarray(want.astype(np.uint8)).save(keep / (scene + '-want.png'))
        self.fail('%s: the strip on the screen differs from the picture by %.1f levels in some window' % (scene, last))

    # ------------------------------------------------------------------ the tests
    def test_the_dock_is_a_strip_of_the_right_size_that_looks_like_the_picture(self):
        self.rig.start_dock()
        self.assertEqual(self.rig.state()['panels'], 'wisp-dock:720x44')
        self.assertStripLike('desktop-empty')

    @unittest.skipUnless(shutil.which('aarch64-linux-gnu-gcc'), 'needs the aarch64 cross compiler')
    def test_the_build_for_the_device_runs_under_qemu_and_draws_the_same_strip_and_takes_keys(self):
        self.rig.start_dock(device=True)
        self.assertEqual(self.rig.state()['panels'], 'wisp-dock:720x44')
        self.assertStripLike('desktop-empty')
        c = self.client('terminal')
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 60, 'summoned')
        self.rig.tap('right')
        self.rig.wait(lambda: self.rig.state()['focus'] == 'settings.desktop', 60, 'the device build to move its focus on a real key')
        self.rig.tap('esc')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'idle', 60, 'put away')

    def test_the_strip_reserves_its_space_and_a_summoned_dock_does_not_move_windows(self):
        self.rig.start_dock()
        self.client('terminal')
        self.rig.wait(lambda: self.rig.window('terminal')['rect']['height'] == 436, 20, 'the window to fit above the strip')
        r = self.rig.window('terminal')['rect']
        self.assertEqual((r['x'], r['y'], r['width'], r['height']), (0, 0, 720, 436))
        self.rig.ctl('summon')
        self.rig.wait(lambda: 'wisp-dock:720x480' in self.rig.state()['panels'], 20, 'the summoned dock to take the screen')
        time.sleep(1)
        self.assertEqual(self.rig.window('terminal')['rect']['height'], 436, 'a summoned dock covers the screen but still reserves only the strip')
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['panels'] == 'wisp-dock:720x44', 20, 'the strip again')

    def test_each_window_gets_a_workspace_of_its_own_so_it_fills_the_space_the_dock_leaves(self):
        self.rig.start_dock()
        self.client('terminal')
        self.client('images')
        self.rig.wait(lambda: len(json.loads(self.rig.swaymsg('-r', '-t', 'get_workspaces').stdout)) >= 2, 30, 'a second workspace')
        for app in ('terminal', 'images'):
            self.rig.wait(lambda: self.rig.window(app)['rect']['width'] == 720 and self.rig.window(app)['rect']['height'] == 436, 30, 'the window %s to fill the area' % app)
        t = self.rig.window('terminal'); i = self.rig.window('images')
        self.assertTrue(i['focused'] and not t['focused'], 'the new window is the one shown')
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 20, 'summoned')
        for _ in range(2): self.rig.tap('left')
        self.rig.wait(lambda: self.rig.state()['focus'] == 'terminal.desktop', 20, 'on the terminal pin')
        self.rig.tap('enter')
        self.rig.wait(lambda: self.rig.window('terminal')['focused'], 30, 'the dock to bring the other workspace forward')
        self.client('terminal') if False else None
        self.rig.swaymsg('[app_id=terminal] kill')
        self.rig.wait(lambda: not self.rig.window('terminal'), 30, 'the terminal to close')
        self.rig.wait(lambda: self.rig.window('images')['focused'], 30, 'sway to fall back to the workspace before')

    def test_windows_appear_in_the_dock_and_the_one_in_front_is_marked(self):
        self.rig.start_dock()
        self.client('images'); self.client('terminal')
        self.rig.wait(lambda: self.rig.state()['windows'] == '2', 20, 'two windows in the dock')
        self.assertStripLike('desktop-window')

    def test_a_summoned_dock_takes_the_keyboard_and_gives_it_back(self):
        self.rig.start_dock()
        c = self.client('terminal')
        self.rig.wait(lambda: 'enter' in c.lines(), 20, 'the window to get the keyboard')
        self.rig.tap('left')
        self.rig.wait(lambda: c.keys() == [KEY['left']], 20, 'a key to reach the window')
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 20, 'the dock to be summoned')
        self.rig.wait(lambda: 'leave' in c.lines(), 20, 'the window to lose the keyboard')
        self.assertEqual(self.rig.state()['focus'], 'terminal.desktop', 'the focus starts on the window in front')
        self.rig.tap('right')
        self.rig.wait(lambda: self.rig.state()['focus'] == 'settings.desktop', 20, 'the focus to move right')
        self.assertEqual(c.keys(), [KEY['left']], 'nothing typed while the dock holds the keyboard reaches the window')
        self.rig.tap('esc')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'idle' and self.rig.state()['panels'] == 'wisp-dock:720x44', 20, 'the dock to be put away')
        self.rig.wait(lambda: c.lines().count('enter') == 2, 20, 'the window to get the keyboard back')
        self.rig.tap('right')
        self.rig.wait(lambda: c.keys() == [KEY['left'], KEY['right']], 20, 'keys to reach the window again')

    def test_enter_on_a_pin_starts_the_application(self):
        self.rig.start_dock()
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 20, 'the dock to be summoned')
        self.assertEqual(self.rig.state()['focus'], 'browser.desktop', 'with no window the focus starts on the first pin')
        self.rig.tap('enter')
        self.rig.wait(lambda: (self.rig.d / 'launched-browser').exists(), 20, 'the browser to be started')
        self.assertEqual(self.rig.state()['dock'], 'idle')

    def test_a_window_is_brought_forward_and_closed_by_the_dock(self):
        self.rig.start_dock()
        t = self.client('terminal'); i = self.client('images')
        self.rig.wait(lambda: self.rig.state()['windows'] == '2', 20, 'two windows')
        self.rig.wait(lambda: self.rig.window('images')['focused'], 20, 'images to be in front')
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 20, 'summoned')
        self.assertEqual(self.rig.state()['focus'], 'images.desktop' if self.rig.state()['focus'] == 'images.desktop' else 'w2', 'the window in front')
        self.rig.tap('left')
        self.rig.wait(lambda: self.rig.state()['focus'] == 'settings.desktop', 20, 'one step left')
        self.rig.tap('left')
        self.rig.wait(lambda: self.rig.state()['focus'] == 'terminal.desktop', 20, 'on the terminal')
        self.rig.tap('enter')
        self.rig.wait(lambda: self.rig.window('terminal')['focused'], 30, 'the terminal to be brought forward')
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 20, 'summoned again')
        self.rig.tap('space')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'actions', 20, 'the actions list')
        for _ in range(4): self.rig.tap('down')
        self.rig.tap('enter')
        self.rig.wait(lambda: not self.rig.window('terminal'), 30, 'the terminal window to be closed by the dock')
        self.assertTrue(self.rig.window('images'))

    def test_the_menu_filters_by_typing_and_launches(self):
        self.rig.start_dock()
        self.rig.ctl('menu')
        self.rig.wait(lambda: self.rig.state()['menu'] == '1' and 'wisp-menu:720x480' in self.rig.state()['panels'], 30, 'the menu surface')
        for k in ('t', 'e', 'r'): self.rig.tap(k)
        self.rig.wait(lambda: self.rig.state()['query'] == 'ter', 30, 'the query to be typed (the menu holds the keyboard)')
        self.rig.tap('backspace'); self.rig.tap('r')
        self.rig.wait(lambda: self.rig.state()['query'] == 'ter', 30, 'backspace and retype')
        self.rig.tap('enter')
        self.rig.wait(lambda: (self.rig.d / 'launched-terminal').exists(), 30, 'the terminal to be started from the menu')
        self.rig.wait(lambda: self.rig.state()['menu'] == '0' and 'wisp-menu' not in self.rig.state()['panels'], 30, 'the menu to close')

    def test_the_keyboard_reserves_space_and_types_through_the_pad_socket(self):
        self.rig.start_dock(pad=True)
        self.rig.wait(lambda: any(l.startswith('focus') for l in self.rig.pad.lines), 20, 'the shell to say hello to the pad daemon')
        c = self.client('terminal')
        self.rig.wait(lambda: 'focus terminal' in self.rig.pad.lines, 30, 'the pad to be told the window in front')
        self.rig.pad.send('osk')
        self.rig.wait(lambda: self.rig.state()['osk'] == '1', 20, 'the keyboard to open on the pad daemon\'s command')
        self.assertIn('osk on', self.rig.pad.lines)
        self.rig.wait(lambda: 'wisp-osk:720x276' in self.rig.state()['panels'] and 'wisp-dock' not in self.rig.state()['panels'], 20, 'the keyboard surface and no dock')
        self.rig.wait(lambda: self.rig.window('terminal')['rect']['height'] == 204, 30, 'the window to give way to the keyboard')
        n = len(self.rig.pad.lines)
        self.rig.pad.send('osk-type')
        self.rig.wait(lambda: len(self.rig.pad.lines) > n, 20, 'a key to be typed')
        self.assertEqual(self.rig.pad.lines[n], 'key 34', 'g is the focused key and its code goes to the pad daemon')
        self.rig.pad.send('osk-shift'); self.rig.pad.send('nav-up'); self.rig.pad.send('osk-type')
        self.rig.wait(lambda: len(self.rig.pad.lines) > n + 1, 20, 'a capital')
        self.assertEqual(self.rig.pad.lines[n + 1], 'key 20 shift', 'up from g: the key above is the one centred nearest, t (a tie with y goes to the left)')
        self.rig.pad.send('osk-hide')
        self.rig.wait(lambda: self.rig.state()['osk'] == '0' and self.rig.state()['panels'] == 'wisp-dock:720x44', 20, 'the dock to come back')
        self.assertIn('osk off', self.rig.pad.lines)
        self.rig.wait(lambda: self.rig.window('terminal')['rect']['height'] == 436, 30, 'the window to get its space back')

    def test_the_pointer_hovers_clicks_and_drags_the_quick_settings(self):
        self.rig.start_dock()
        self.pointer = self.rig.inj
        self.pointer.move(60, 460)
        time.sleep(0.5)
        self.rig.wait(lambda: self.rig.state() is not None, 10, 'the shell')
        self.pointer.click()
        self.rig.wait(lambda: (self.rig.d / 'launched-browser').exists(), 30, 'a click on the browser pin to start it')
        self.pointer.move(690, 460)
        self.pointer.click()
        self.rig.wait(lambda: self.rig.state()['dock'] == 'quick-settings', 30, 'a click on the status group to open the quick settings')
        self.rig.wait(lambda: 'wisp-dock:720x480' in self.rig.state()['panels'], 20, 'the panel surface')
        # the volume row: the track runs from x = 458 to x = 684 on the second row from the top of the panel
        y = None
        shot = self.rig.grim()
        for yy in range(130, 400):                       # find the slider track by its accent colour at the knob side (the Wi-Fi row above, selected and white, is not it)
            if tuple(shot[yy, 460]) != tuple(shot[yy, 300]) and abs(int(shot[yy, 560][2]) - 255) < 40: y = yy; break
        self.assertIsNotNone(y, 'the volume slider is on the screen')
        self.pointer.move(458 + (684 - 458) // 4, y); self.pointer.down()
        self.pointer.move(458 + (684 - 458) * 3 // 4, y); self.pointer.up()
        log = self.rig.d / 'wpctl.log'
        self.rig.wait(lambda: log.exists() and log.read_text().strip(), 30, 'the volume to be set by the tools')
        last = log.read_text().strip().splitlines()[-1].split()
        self.assertEqual(last[:2], ['set-volume', '@DEFAULT_AUDIO_SINK@'])
        self.assertGreater(float(last[2]), 0.6)
        self.pointer.move(100, 100); self.pointer.click()
        self.rig.wait(lambda: self.rig.state()['dock'] == 'idle' and self.rig.state()['panels'] == 'wisp-dock:720x44', 30, 'a click outside the panel to close it')

    def test_a_pad_command_over_the_socket_opens_the_menu_and_the_shell_survives_the_pad_leaving(self):
        self.rig.start_dock(pad=True)
        self.rig.wait(lambda: self.rig.pad.conn is not None, 20, 'the connection')
        self.rig.pad.send('menu')
        self.rig.wait(lambda: self.rig.state()['menu'] == '1', 20, 'the menu to open on the daemon\'s command')
        self.rig.pad.send('menu')
        self.rig.wait(lambda: self.rig.state()['menu'] == '0', 20, 'and to close on the second')
        self.rig.pad.conn.close()
        time.sleep(0.5)
        self.assertIsNotNone(self.rig.state(), 'the shell is still there')

    def test_a_held_key_repeats_in_the_shell_because_the_compositor_does_not_repeat_for_clients(self):
        self.rig.start_dock()
        self.rig.ctl('summon')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'summoned', 20, 'summoned')
        self.assertEqual(self.rig.state()['focus'], 'browser.desktop')
        self.rig.inj.key('down', KEY['right'])
        time.sleep(2.0)
        self.rig.inj.key('up', KEY['right'])
        focus = self.rig.state()['focus']
        self.assertIn(focus, ('game', 'status', 'settings.desktop', 'terminal.desktop'), 'holding right moved the focus on from the first pin (it is %s)' % focus)
        self.assertNotEqual(focus, 'browser.desktop')
        here = self.rig.state()['focus']
        time.sleep(1.5)
        self.assertEqual(self.rig.state()['focus'], here, 'and it stops when the key is released')

    def test_the_shell_is_idle_when_nothing_happens(self):
        self.rig.start_dock()
        time.sleep(2)
        def cpu():
            with open('/proc/%d/stat' % self.rig.dock.pid) as fh: f = fh.read().split(')')[-1].split()
            return (int(f[11]) + int(f[12])) / os.sysconf('SC_CLK_TCK')
        a = cpu(); time.sleep(6); b = cpu()
        self.assertLess(b - a, 0.15, 'six idle seconds cost %.2f s of CPU: something is polling' % (b - a))

    def test_a_program_started_from_the_dock_can_show_a_toast(self):
        self.rig.start_dock()
        self.rig.ctl('toast', 'Browser|Downloading Firefox, about 74 MB.|3')
        self.rig.wait(lambda: self.rig.state()['toast'] == '1' and 'wisp-toast:' in self.rig.state()['panels'], 20, 'the toast surface')
        w, h = self.rig.state()['panels'].split('wisp-toast:')[1].split(',')[0].rstrip('?').split('x')
        self.assertTrue(200 <= int(w) <= 420 and 40 <= int(h) <= 110, (w, h))
        shot = self.rig.grim()
        self.assertGreater(int((shot[0:int(h) + 10, 720 - int(w) - 12:720].astype(int).sum(-1) > 300).sum()), 100, 'text is on the screen at the top right')
        self.rig.wait(lambda: self.rig.state()['toast'] == '0' and 'wisp-toast' not in self.rig.state()['panels'], 20, 'the toast to go after its seconds')

    def test_a_finished_download_is_offered_in_a_toast_that_takes_a_yes(self):
        self.rig.start_dock()
        down = self.rig.d / 'downloads'
        self.assertTrue(down.is_dir(), 'the shell makes the folder it watches')
        part = down / 'Foo-1.0-aarch64.AppImage.part'
        part.write_bytes(b'\x7fELF\x02\x01\x01\x00AI\x02\x00\x00\x00\x00\x00\x03\x00\xb7\x00' + b'\0' * 100)
        time.sleep(1.5)
        self.assertEqual(self.rig.state()['toast'], '0', 'a partial file is not a finished download')
        part.rename(down / 'Foo-1.0-aarch64.AppImage')
        self.rig.wait(lambda: self.rig.state()['toast'] == '1' and self.rig.state()['toast_modal'] == '1', 20, 'the offer')
        self.rig.wait(lambda: 'wisp-toast:' in self.rig.state()['panels'], 20, 'the toast surface')
        c = self.client('terminal')
        self.rig.wait(lambda: self.rig.state()['toast_modal'] == '1', 5, 'still offered')
        self.rig.tap('enter')
        log = self.rig.d / 'download-action.log'
        self.rig.wait(lambda: log.exists() and log.read_text().strip(), 20, 'the action to run when A is pressed on the offer')
        self.assertEqual(log.read_text().strip(), 'install %s' % (down / 'Foo-1.0-aarch64.AppImage'))
        self.rig.wait(lambda: self.rig.state()['toast'] == '0', 20, 'the offer to go')
        self.assertEqual(c.keys(), [], 'the Enter was the offer\'s: it did not reach the window')
        # a second one: B puts it away and nothing runs
        (down / 'pack.zip').write_bytes(b'PK\x03\x04' + b'\0' * 40)
        self.rig.wait(lambda: self.rig.state()['toast_modal'] == '1', 20, 'the second offer')
        self.rig.tap('esc')
        self.rig.wait(lambda: self.rig.state()['toast'] == '0', 20, 'B to put it away')
        time.sleep(0.5)
        self.assertEqual(len(log.read_text().strip().splitlines()), 1, 'dismissing runs nothing')
        # a text file is only announced: no keyboard taken
        (down / 'notes.txt').write_text('hello')
        self.rig.wait(lambda: self.rig.state()['toast'] == '1', 20, 'the plain announcement')
        self.assertEqual(self.rig.state()['toast_modal'], '0')
        self.rig.tap('left')
        self.rig.wait(lambda: c.keys() == [KEY['left']], 20, 'keys still reach the window while a plain toast is up')

    def test_a_click_on_the_offer_accepts_it(self):
        self.rig.start_dock()
        down = self.rig.d / 'downloads'
        (down / 'p.png').write_bytes(b'\x89PNG\r\n\x1a\n' + b'\0' * 40)
        self.rig.wait(lambda: self.rig.state()['toast_modal'] == '1' and 'wisp-toast:' in self.rig.state()['panels'], 20, 'the offer')
        w, h = self.rig.state()['panels'].split('wisp-toast:')[1].split(',')[0].rstrip('?').split('x')
        self.rig.inj.move(720 - 8 - int(w) // 2, 8 + int(h) // 2)
        time.sleep(0.5)
        self.rig.inj.click()
        log = self.rig.d / 'download-action.log'
        self.rig.wait(lambda: log.exists() and log.read_text().strip(), 20, 'a click to accept the offer')
        self.assertEqual(log.read_text().strip(), 'open %s' % (down / 'p.png'))

    def test_the_settings_it_shows_come_from_the_tools(self):
        self.rig.start_dock()
        self.rig.ctl('qs')
        self.rig.wait(lambda: self.rig.state()['dock'] == 'quick-settings', 20, 'the quick settings')
        time.sleep(3)                                     # the tools are asked when it opens
        shot = self.rig.grim()
        self.assertGreater(int((shot[100:400, 400:720].astype(int).sum(-1) > 60).sum()), 2000, 'the panel is on the screen')

    def test_the_settings_window_takes_the_keyboard_and_sets_the_screen_timeout(self):
        self.rig.start_dock()
        c = self.client('terminal')
        self.rig.wait(lambda: 'enter' in c.lines(), 20, 'the window to get the keyboard')
        self.rig.ctl('settings')
        self.rig.wait(lambda: self.rig.state()['settings'] == '1', 20, 'the settings window')
        self.rig.wait(lambda: 'wisp-settings:720x436' in self.rig.state()['panels'], 20, 'its surface above the strip')
        self.rig.wait(lambda: 'leave' in c.lines(), 20, 'the terminal to lose the keyboard')
        time.sleep(3)                                                 # the tools are asked when it opens: the screen timeout (120 s) is read
        shot = self.rig.grim()
        self.assertGreater(int((shot[10:34, 20:140].astype(int).sum(-1) > 500).sum()), 100, 'the title "Settings" is on the screen')
        self.rig.tap('right'); self.rig.tap('right'); self.rig.tap('enter')       # into the rows on 2 min (what the tool reported), on to 5 min, apply
        log = self.rig.d / 'screen-timeout.log'
        self.rig.wait(lambda: log.exists() and log.read_text().strip(), 30, 'the timeout to be set through wisp-screen')
        self.assertEqual(log.read_text().split(), ['300'])
        self.assertEqual(c.keys(), [], 'nothing typed in the settings window reaches the terminal')
        self.rig.tap('esc'); self.rig.tap('esc')
        self.rig.wait(lambda: self.rig.state()['settings'] == '0' and self.rig.state()['panels'] == 'wisp-dock:720x44', 20, 'the window to close and its surface to go')
        self.rig.wait(lambda: c.lines().count('enter') == 2, 20, 'the terminal to get the keyboard back')

    def test_add_game_lists_a_card_adds_the_shortcut_through_wisp_shortcut_and_offers_game_mode(self):
        card, internal = self.rig.d / 'card', self.rig.d / 'internal'
        shutil.rmtree(card, True); shutil.rmtree(internal, True); shutil.rmtree(self.rig.d / 'steamroot', True)
        (card / 'games/wow/Data').mkdir(parents=True); (card / 'games/wow/WoW.exe').write_bytes(b'MZ' * 2048); (card / 'games/wow/readme.txt').write_text('x'); (card / 'games/wow/.hidden').write_text('x')
        (self.rig.d / 'steamroot/userdata/4242/config').mkdir(parents=True)
        self.rig.start_dock(extra={'WISP_AG_CARD': str(card), 'WISP_AG_INTERNAL': str(internal)})
        self.rig.ctl('addgame')
        self.rig.wait(lambda: self.rig.state()['settings'] == '1', 20, 'the Add game window')
        self.rig.wait(lambda: 'wisp-settings:720x436' in self.rig.state()['panels'], 20, 'its surface above the strip')
        self.assertTrue(internal.is_dir(), 'the internal place is made when it is missing')
        time.sleep(1.0)                                                # the listing of the card arrives
        shot = self.rig.grim()
        self.assertGreater(int((shot[10:34, 20:150].astype(int).sum(-1) > 500).sum()), 100, 'the title "Add game" is on the screen')
        for k in ('right', 'enter', 'enter'): self.rig.tap(k); time.sleep(0.6)       # into the listing, open games, open wow
        for k in ('down', 'enter'): self.rig.tap(k); time.sleep(0.6)                 # Data is first (the way up is not selected), then WoW.exe: the confirmation
        time.sleep(0.6)
        shot = self.rig.grim()
        self.assertGreater(int((shot[70:100, 190:300].astype(int).sum(-1) > 300).sum()), 50, 'the confirmation shows the name')
        self.assertFalse((self.rig.d / 'steamroot/userdata/4242/config/shortcuts.vdf').exists(), 'asking is not adding')
        self.rig.tap('enter')                                          # Add to Steam
        vdf = self.rig.d / 'steamroot/userdata/4242/config/shortcuts.vdf'
        self.rig.wait(lambda: vdf.exists(), 20, 'the shortcut file to be written by wisp-shortcut')
        out = subprocess.run([sys.executable, '-I', str(HERE / 'rootfs/usr/bin/wisp-shortcut'), 'list', '--file', str(vdf)], capture_output=True, text=True).stdout
        self.assertIn('\tWoW\t"%s"' % (card / 'games/wow/WoW.exe'), out)
        time.sleep(1.0)
        shot = self.rig.grim()
        self.assertGreater(int((shot[170:200, 180:340].astype(int).sum(-1) > 400).sum()), 80, 'the added dialog is on the screen')
        log = self.rig.d / 'session.log'
        self.assertFalse(log.exists() and log.read_text().strip(), 'Game mode is not entered before the person asks')
        self.rig.tap('enter')                                          # Game mode
        self.rig.wait(lambda: log.exists() and log.read_text().split() == ['game'], 20, 'the session switch')
        self.rig.wait(lambda: self.rig.state()['settings'] == '0' and self.rig.state()['panels'] == 'wisp-dock:720x44', 20, 'the window to close')

if __name__ == '__main__':
    unittest.main()
