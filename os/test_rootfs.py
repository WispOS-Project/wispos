"""Offline checks on the files baked into the WispOS rootfs and on the bootloader build script."""
import configparser, json, os, shutil, subprocess, tempfile, unittest, unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORK = Path(os.environ.get('WISP_WORK', '/mnt/hdd/wispos-staging/wispos-os-20261007'))


class RootfsTests(unittest.TestCase):
    def test_session_unit_is_the_only_session_and_keeps_trying(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None); unit.optionxform = str
        unit.read(HERE / 'rootfs/usr/lib/systemd/system/wisp-steam.service')
        self.assertEqual(unit['Install']['WantedBy'], 'wispos.target')
        self.assertEqual(unit['Service']['Restart'], 'always')
        self.assertNotIn('OnFailure', unit['Unit'])         # no fallback frontend by design
        self.assertEqual(unit['Service']['ExecStart'], '/usr/bin/wisp-steam-session')

    def test_steam_always_starts_on_a_fresh_xwayland(self):
        # Measured on the handheld (notes 4d): when Steam started within ~10 s of the previous one it reused that Steam's leftover Xwayland and its UI
        # stalled or stayed black on about half of the starts (16/32 and 20/40 in two series); with Xwayland restarted first, or a wait long enough for
        # Xwayland to exit by itself, 1/8, 0/8, 0/8 and 1/24 did. sway respawns Xwayland on demand, so the launcher ends the old one before each start.
        import re, tempfile, os, stat
        text = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        loop = text[text.index('while :'):]
        self.assertIn('fresh_xwayland', loop)
        self.assertLess(loop.index('fresh_xwayland'), loop.index('dbus-run-session'), 'the old Xwayland must go before each Steam start')
        body = re.search(r'# begin fresh_xwayland\n(.*?)# end fresh_xwayland', text, re.S)
        self.assertIsNotNone(body, 'fresh_xwayland must be delimited so it can be tested')
        self.assertNotIn('pkill', body.group(1)); self.assertNotIn('killall', body.group(1))        # by PID only, never by pattern
        # kill/sleep are shell builtins, so PATH stubs would be bypassed and the real kill could signal real PIDs: override them with shell functions.
        work = Path(tempfile.mkdtemp(prefix='wispos-xw-', dir=os.environ.get('TMPDIR')))
        state, log = work / 'pids', work / 'killed'
        stubs = (f'pidof() {{ [ -s {state} ] && cat {state} || return 1; }}\n'
                 f'kill() {{ echo "$@" >> {log}; [ -n "${{STUBBORN:-}}" ] || : > {state}; }}\n'
                 'sleep() { :; }\n')
        def run(extra):
            script = 'set -u\n' + stubs + body.group(1) + '\nfresh_xwayland\necho returned'
            return subprocess.run(['sh', '-c', script], env={'PATH': '/usr/bin:/bin', **extra}, capture_output=True, text=True, timeout=20)
        state.write_text('101 102\n')
        out = run({}); self.assertEqual(out.stdout.split(), ['returned']); self.assertEqual(log.read_text().split(), ['101', '102'])
        log.unlink(); state.write_text('')                      # nothing running: no kill, no hang
        out = run({}); self.assertEqual(out.stdout.split(), ['returned']); self.assertFalse(log.exists())
        state.write_text('103\n')                              # a process that refuses to die must not hang the launcher
        out = run({'STUBBORN': '1'}); self.assertEqual(out.stdout.split(), ['returned'])

    def test_scripts_are_valid_shell_and_never_kill_by_pattern(self):
        for rel, shell in (('rootfs/usr/bin/wisp-steam-session', 'bash'), ('rootfs/usr/bin/wisp-shm-clean', 'sh'),
                           ('boot/build-uboot.sh', 'bash'), ('boot/build-kernel.sh', 'bash'), ('build-rootfs.sh', 'bash')):
            path = HERE / rel
            subprocess.run([shell, '-n', str(path)], check=True)
            self.assertNotIn('pkill -f', path.read_text(), rel)

    def test_bootloader_build_is_pinned_and_reproducible_by_construction(self):
        text = (HERE / 'boot/build-uboot.sh').read_text()
        self.assertIn('ROCKNIX_COMMIT=c445081a59518f37d9776e5412dd7b14910696f7', text)
        self.assertIn('sha256sum', text)                       # every download verified
        self.assertIn('SOURCE_DATE_EPOCH', text)               # fixed timestamp
        self.assertIn('BUILD_MESSAGE_TIMESTAMP', text)         # Trusted Firmware banner pinned
        self.assertIn('/mnt/hdd/*', text)                      # work directory must be on the HDD
        for forbidden in ('apt-get', 'dpkg', 'sudo ', 'binfmt'):
            self.assertNotIn(forbidden, text)


import subprocess as _sp, sys as _sys, tempfile as _tf
_sys.path.insert(0, str(HERE))
import accounts


class BluetoothActivationTests(unittest.TestCase):
    def test_the_bluez_name_is_started_on_demand_so_the_docks_bluetoothctl_works_without_an_always_on_daemon(self):
        # found on the handheld 2026-10-12: nothing started bluetoothd, so `bluetoothctl show` (the quick-settings toggle) returned nothing
        f = HERE / 'rootfs/usr/share/dbus-1/system-services/org.bluez.service'; self.assertTrue(f.is_file())
        fields = dict(l.split('=', 1) for l in f.read_text().splitlines() if '=' in l)
        self.assertEqual((fields['Name'], fields['SystemdService']), ('org.bluez', 'bluetooth.service'))
        self.assertFalse((HERE / 'rootfs/usr/lib/systemd/system/wispos.target.wants/bluetooth.service').exists(), 'it must not be always on: it costs RAM')


class MemoryPolicyTests(unittest.TestCase):
    def test_the_zram_cap_is_640_mib(self):
        # Operator decision 2026-10-15: 640 MiB (was 450). Measured on the handheld the same day: at the 450 MiB cap an aged Steam session (a 453 MB download, an install script and a prefix creation in
        # one session) filled zram to its limit, zram rejected about 8,500 swap-outs a second (`failed_writes`), reclaim kept picking pages it could not store, the file cache was squeezed to ~100 MB and
        # the device crawled (docs/WISPOS-NOTES.md "Slowdown in the aged Steam session"); 560 MiB stopped the rejects. The disk size (2048 MiB) and the algorithm (lz4) are unchanged.
        text = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn("printf '671088640\\n' >/sys/block/zram0/mem_limit", text)
        self.assertEqual(671088640, 640 * 1024 * 1024)
        self.assertNotIn('471859200', text, 'the old 450 MiB value must not be left behind')

    def test_the_session_sets_the_zram_friendly_vm_policy(self):
        text = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn("printf '180\\n' >/proc/sys/vm/swappiness", text)
        self.assertIn("printf '0\\n' >/proc/sys/vm/page-cluster", text)                  # zram: one page per swap-in, not eight (measured need, docs/WISPOS-NOTES.md memory policy)


class AccountTests(unittest.TestCase):
    PASSWD = "root:x:0:0:Root User:/storage:/bin/sh\ndbus:x:81:81:System message bus:/:/bin/sh\nnobody:x:65534:65534:nobody:/:/bin/false\n"
    GROUP = "root:x:0:\nwheel:x:10:\nvideo:x:39:\naudio:x:63:\ninput:x:104:\nrender:x:105:\nusers:x:100:\nnetdev:x:497:\nsystemd-journal:x:190:\n"

    def test_wisp_password_hash_is_a_real_sha512_crypt_that_checks_out_independently(self):
        stored = accounts.sha512_crypt('wisp')
        self.assertTrue(stored.startswith('$6$' + accounts.SALT + '$'))
        # Independent verification: re-derive with the salt taken from the stored hash itself.
        salt = stored.split('$')[2]
        again = _sp.run(['openssl', 'passwd', '-6', '-salt', salt, '-stdin'], input='wisp\n', capture_output=True, text=True).stdout.strip()
        self.assertEqual(stored, again)
        wrong = _sp.run(['openssl', 'passwd', '-6', '-salt', salt, '-stdin'], input='wisp1\n', capture_output=True, text=True).stdout.strip()
        self.assertNotEqual(stored, wrong)

    def test_apply_adds_user_groups_and_a_real_shadow_without_touching_other_accounts(self):
        with _tf.TemporaryDirectory(prefix='wispos-accounts-') as root:
            etc = Path(root) / 'etc'; etc.mkdir()
            (etc / 'passwd').write_text(self.PASSWD); (etc / 'group').write_text(self.GROUP)
            (etc / 'shadow').symlink_to('/storage/.cache/shadow')            # as in ROCKNIX
            accounts.apply(root); accounts.apply(root)                         # idempotent
            passwd = (etc / 'passwd').read_text().splitlines()
            self.assertEqual([l for l in passwd if l.startswith('wisp:')], ['wisp:x:1000:1000:WispOS:/storage/home/wisp:/bin/sh'])
            self.assertIn('root:x:0:0:Root User:/storage:/bin/sh', passwd)
            groups = {l.split(':')[0]: l for l in (etc / 'group').read_text().splitlines()}
            for name in ('wheel', 'video', 'audio', 'input', 'render', 'netdev', 'users', 'systemd-journal'):
                self.assertIn('wisp', groups[name].split(':')[3].split(','), name)
            self.assertEqual(groups['wisp'], 'wisp:x:1000:')
            self.assertFalse((etc / 'shadow').is_symlink())
            shadow = {l.split(':')[0]: l.split(':')[1] for l in (etc / 'shadow').read_text().splitlines()}
            self.assertEqual(shadow['root'], '*')       # not '!': a locked root would refuse key logins too; self.assertEqual(shadow['dbus'], '*')
            self.assertEqual(shadow['wisp'], accounts.sha512_crypt('wisp'))
            self.assertEqual((etc / 'shadow').stat().st_mode & 0o777, 0o640)

    def test_ssh_is_off_by_default_persistently_switchable_and_allows_the_wisp_password_login(self):
        unit = (HERE / 'rootfs/usr/lib/systemd/system/sshd.service').read_text()
        self.assertIn('ConditionPathExists=/storage/.config/wispos/ssh-enabled', unit)       # no flag, no sshd (operator, 2026-10-11)
        self.assertIn('After=network.target wisp-firstboot.service', unit)                   # the first-boot seeding runs before the condition is evaluated
        self.assertIn('WantedBy=multi-user.target', unit)
        conf = (HERE / 'rootfs/etc/ssh/sshd_config.wisp').read_text()
        self.assertIn('PasswordAuthentication yes', conf)
        self.assertIn('PermitRootLogin prohibit-password', conf)               # root: keys only
        self.assertIn('d /storage/home/wisp 0700 wisp wisp', (HERE / 'rootfs/usr/lib/tmpfiles.d/wisp.conf').read_text())


class SshSwitchTests(unittest.TestCase):
    """wisp-ssh (the toggle the desktop-mode menu will call) and the `ssh` marker file on the boot partition."""
    SCRIPT = HERE / 'rootfs/usr/bin/wisp-ssh'

    def run_ssh(self, *args, fail_start=False):
        root = Path(_tf.mkdtemp(prefix='wispos-ssh-', dir=_os.environ.get('TMPDIR')))
        calls = root / 'calls'; fake = root / 'systemctl'
        fake.write_text('#!/bin/sh\necho "$@" >> %s\n%s' % (calls, 'exit 1\n' if fail_start else 'exit 0\n')); fake.chmod(0o755)
        flag = root / 'cfg/ssh-enabled'
        run = lambda *a: _sp.run(['sh', str(self.SCRIPT), *a], capture_output=True, text=True, env={**_os.environ, 'WISP_SSH_FLAG': str(flag), 'WISP_SYSTEMCTL': str(fake)})
        return root, flag, calls, run

    def test_on_remembers_and_starts_off_forgets_and_stops_toggle_flips_status_reports(self):
        root, flag, calls, run = self.run_ssh()
        r = run('status'); self.assertEqual((r.returncode, r.stdout.strip()), (3, 'ssh: off')); self.assertFalse(flag.exists())
        r = run('on'); self.assertEqual(r.returncode, 0, r.stderr); self.assertTrue(flag.exists()); self.assertIn('ssh: on', r.stdout); self.assertIn('start sshd.service', calls.read_text())
        self.assertEqual(run('status').returncode, 0)
        r = run('toggle'); self.assertFalse(flag.exists()); self.assertIn('stop sshd.service', calls.read_text()); self.assertIn('ssh: off', r.stdout)
        run('toggle'); self.assertTrue(flag.exists())                                        # and back on
        self.assertEqual(run('bogus').returncode, 2)

    def test_status_lists_the_addresses_but_not_loopback_with_the_plain_ip_output_both_ip_implementations_give(self):
        root, flag, calls, run = self.run_ssh()
        fake_ip = root / 'bin'; fake_ip.mkdir(); (fake_ip / 'ip').write_text('#!/bin/sh\n[ "$*" = "-4 addr show" ] || exit 1\ncat <<EOF\n1: lo: <LOOPBACK,UP> mtu 65536\n    inet 127.0.0.1/8 scope host lo\n2: wlan0: <UP> mtu 1500\n    inet 192.0.2.7/24 brd 192.0.2.255 scope global wlan0\nEOF\n'); (fake_ip / 'ip').chmod(0o755)
        env = {**_os.environ, 'WISP_SSH_FLAG': str(flag), 'WISP_SYSTEMCTL': str(root / 'systemctl'), 'PATH': '%s:%s' % (fake_ip, _os.environ['PATH'])}
        flag.parent.mkdir(parents=True); flag.write_text('')
        out = _sp.run(['sh', str(self.SCRIPT), 'status'], capture_output=True, text=True, env=env)
        self.assertEqual(out.returncode, 0); self.assertIn('address: 192.0.2.7', out.stdout); self.assertNotIn('127.0.0.1', out.stdout)

    def test_a_failed_start_is_an_error_not_a_silent_success(self):
        root, flag, calls, run = self.run_ssh(fail_start=True)
        r = run('on'); self.assertEqual(r.returncode, 1); self.assertIn('did not start', r.stderr)

    def test_the_ssh_file_on_the_boot_partition_seeds_the_setting_once_and_the_users_choice_then_persists(self):
        helper = FirstBootTests()
        root, out = helper.run_with({'ssh': ''})
        flag, seeded = root / 'storage/.config/wispos/ssh-enabled', root / 'storage/.config/wispos/ssh-seeded'
        self.assertTrue(flag.exists() and seeded.exists()); self.assertIn('SSH turned on', out.stdout)
        flag.unlink()                                                                         # the user turns it off with wisp-ssh off ...
        text = (root / 'run.sh').read_text()
        _sp.run(['sh', str(root / 'run.sh')], capture_output=True, text=True)                 # ... and the next boot, marker still on the card, must NOT turn it back on
        self.assertFalse(flag.exists())
        root, out = helper.run_with({}); self.assertFalse((root / 'storage/.config/wispos/ssh-enabled').exists())      # no marker, no SSH

    def test_development_images_carry_the_marker_and_release_images_do_not(self):
        text = (HERE / 'build-image.sh').read_text()
        dev_block = text[text.index('if [ "${WISP_DEV:-}" = 1 ]; then\n    printf \'DEVELOPMENT BUILD'):]
        self.assertIn('wispos/ssh"', dev_block.split('fi\n', 1)[0])                          # inside the WISP_DEV branch
        self.assertIn('SSH is OFF by default', text)


import os as _os
import check_rootfs, stage_rootfs


class FirstBootTests(unittest.TestCase):
    SCRIPT = HERE / 'rootfs/usr/bin/wisp-firstboot'

    def run_with(self, files):
        """Run the script against a fake /flash and /storage by rewriting its absolute paths."""
        root = Path(_tf.mkdtemp(prefix='wispos-firstboot-', dir=_os.environ.get('TMPDIR')))
        (root / 'flash/wispos').mkdir(parents=True); (root / 'storage/home/wisp').mkdir(parents=True)
        for name, text in files.items():
            (root / 'flash/wispos' / name).write_text(text)
        text = self.SCRIPT.read_text().replace('/flash', str(root / 'flash')).replace('/storage', str(root / 'storage'))
        text = text.replace('chown -R wisp:wisp', 'true')
        script = root / 'run.sh'; script.write_text(text)
        out = _sp.run(['sh', str(script)], capture_output=True, text=True)
        return root, out

    def test_wifi_conf_becomes_a_private_networkmanager_profile(self):
        root, out = self.run_with({'wifi.conf': 'ssid=My Net\r\npsk=pass phrase\r\n'})
        self.assertEqual(out.returncode, 0, out.stderr)
        files = list((root / 'storage/.config/NetworkManager/system-connections').glob('*.nmconnection'))
        self.assertEqual([f.name for f in files], ['wisp-My_Net.nmconnection'])
        text = files[0].read_text()
        self.assertIn('ssid=My Net\n', text); self.assertIn('psk=pass phrase\n', text); self.assertIn('key-mgmt=wpa-psk', text)
        self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_incomplete_wifi_conf_is_ignored_loudly_and_missing_files_are_fine(self):
        root, out = self.run_with({'wifi.conf': 'ssid=OnlyName\n'})
        self.assertEqual(out.returncode, 0); self.assertIn('ignored', out.stdout)
        self.assertFalse((root / 'storage/.config').exists())
        root, out = self.run_with({})
        self.assertEqual(out.returncode, 0)

    def test_authorized_keys_reach_root_and_wisp_with_strict_permissions(self):
        root, out = self.run_with({'authorized_keys': 'ssh-ed25519 AAAA test\n'})
        for home in ('storage', 'storage/home/wisp'):
            key = root / home / '.ssh/authorized_keys'
            self.assertEqual(key.read_text(), 'ssh-ed25519 AAAA test\n')
            self.assertEqual(key.stat().st_mode & 0o777, 0o600)
            self.assertEqual((root / home / '.ssh').stat().st_mode & 0o777, 0o700)


class KernelBuildScriptTests(unittest.TestCase):
    TEXT = (HERE / 'boot/build-kernel.sh').read_text()

    def test_sources_are_pinned_by_the_rocknix_recipe_and_hash_checked(self):
        self.assertIn('ROCKNIX_COMMIT=c445081a59518f37d9776e5412dd7b14910696f7', self.TEXT)
        self.assertEqual(self.TEXT.count('fetch "'), 4)          # kernel, firmware, regdb, joypad driver
        self.assertIn('hash mismatch', self.TEXT)
        self.assertIn('/mnt/hdd/*', self.TEXT)

    def test_patches_apply_in_rocknix_order_and_stop_on_failure(self):
        order = [self.TEXT.index(x) for x in ('patches/mainline/*.patch', '"$LK/patches/$KVER"/*.patch', 'devices/$DEV/patches/linux"/*.patch')]
        self.assertEqual(order, sorted(order))
        self.assertIn('set -euo pipefail', self.TEXT)

    def test_wispos_patches_come_after_rocknix_ones_and_stop_the_build_when_they_do_not_apply(self):
        self.assertLess(self.TEXT.index('devices/$DEV/patches/linux"/*.patch'), self.TEXT.index('"$HERE"/patches/*.patch'))
        self.assertIn('patch -d "$K" -p1 <"$p"', self.TEXT)          # under `set -e`: a failing patch ends the build
        self.assertTrue(sorted((HERE / 'boot/patches').glob('*.patch')))

    def test_the_host_role_gate_patch_says_what_it_is_and_applies_to_the_tree_it_was_made_for(self):
        patch = HERE / 'boot/patches/0001-phy-sun4i-usb-host-gate.patch'; text = patch.read_text()
        for needle in ('host_enable', 'READ_ONCE(data->host_enable)', 'External vbus detected', 'dev_groups = sun4i_usb_phy_groups', 'NOT yet exercised on the hardware'):
            self.assertIn(needle, text)
        tree = WORK / 'build/linux'                                    # the kernel tree of the last build, when there is one
        if not (tree / 'drivers/phy/allwinner/phy-sun4i-usb.c').exists(): self.skipTest('no built kernel tree on this host')
        def dry(*flags): return _sp.run(['patch', '--dry-run', '-p1', '-d', str(tree), *flags], input=text, capture_output=True, text=True).returncode == 0
        self.assertTrue(dry() or dry('-R'), 'the patch neither applies to nor is already applied in the kernel tree')

    def test_settings_that_make_the_build_match_the_reference(self):
        for needle in ('gzip -n -9', 'olddefconfig', 'DTC_FLAGS=-@', 'DEVICE=$DEV', 'KBUILD_BUILD_TIMESTAMP', 'CONFIG_EXTRA_FIRMWARE'):
            self.assertIn(needle, self.TEXT)


class ImageCredentialGuardTests(unittest.TestCase):
    def test_credentials_are_refused_without_the_dev_flag_and_never_default(self):
        text = (HERE / 'build-image.sh').read_text()
        self.assertIn('needs WISP_DEV=1', text)
        self.assertLess(text.index('needs WISP_DEV=1'), text.index('mkdir -p "$STAGE/boot/extlinux"'))   # refuses before building
        self.assertNotIn('.ssh/', text)                 # no builder-home key is ever read implicitly
        self.assertIn('DEV-BUILD.txt', text)
        out = _sp.run(['bash', str(HERE / 'build-image.sh'), '/mnt/hdd/wispos-staging/wispos-os-20261007'], capture_output=True, text=True,
                      env={**_os.environ, 'WISP_SSH_KEY': '/dev/null', 'WISP_DEV': ''})
        self.assertEqual(out.returncode, 2, out.stderr)
        self.assertIn('needs WISP_DEV=1', out.stderr)


class RootfsBuildScriptTests(unittest.TestCase):
    TEXT = (HERE / 'build-rootfs.sh').read_text()

    def test_the_fallback_preload_is_built_plain_armv8_and_checked_for_lse(self):
        for flag in ('-march=armv8-a', '-mno-outline-atomics', '-Wl,-z,now', '-Wl,-z,initfirst', 'lse-ldadd.c', 'contains LSE instructions'):
            self.assertIn(flag, self.TEXT)

    def test_the_pinned_userland_hash_is_checked_before_anything_is_extracted(self):
        self.assertLess(self.TEXT.index('does not match the lock file'), self.TEXT.index('unsquashfs'))
        self.assertIn('/mnt/hdd/*', self.TEXT)

    def test_session_uses_the_preload_that_ships_in_the_image(self):
        text = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn('LD_PRELOAD=/usr/lib/wispos/libwispos-ldadd.so', text)
        self.assertNotIn('$base/libwispos-ldadd', text)


class StageRootfsTests(unittest.TestCase):
    def tree(self):
        root = Path(_tf.mkdtemp(prefix='wispos-stage-'))
        for rel in ('usr/lib/libretro/core.so', 'usr/bin/retroarch', 'usr/bin/start_a.sh', 'usr/bin/vi', 'usr/lib/lib32keep'):
            (root / rel).parent.mkdir(parents=True, exist_ok=True); (root / rel).write_text('x')
        return root

    def manifest(self, text):
        path = Path(_tf.mkdtemp(prefix='wispos-manifest-')) / 'prune.txt'; path.write_text(text); return path

    def test_prune_removes_listed_paths_reports_stale_patterns_and_keeps_the_rest(self):
        root = self.tree()
        freed, unmatched = stage_rootfs.prune(root, self.manifest('# c\nusr/lib/libretro\nusr/bin/start_*.sh\nusr/bin/retroarch\nusr/bin/gone\n'))
        self.assertFalse((root / 'usr/lib/libretro').exists()); self.assertFalse((root / 'usr/bin/start_a.sh').exists())
        self.assertTrue((root / 'usr/bin/vi').exists()); self.assertTrue((root / 'usr/lib/lib32keep').exists())
        self.assertEqual(unmatched, ['usr/bin/gone']); self.assertGreater(freed, 0)

    def test_prune_refuses_patterns_that_could_wipe_a_directory_or_leave_the_tree(self):
        root = self.tree()
        for bad in ('usr/*', '/etc/passwd', 'usr/../etc', 'usr', 'usr/lib/*'):
            with self.assertRaises(SystemExit, msg=bad):
                stage_rootfs.prune(root, self.manifest(bad + '\n'))
        self.assertTrue((root / 'usr/bin/vi').exists())

    def test_the_real_manifest_never_removes_what_the_boot_and_session_need(self):
        lines = [l.strip() for l in (HERE / 'rootfs-prune.txt').read_text().splitlines() if l.strip() and not l.startswith('#')]
        for must_keep in check_rootfs.BOOT_CONTRACT:
            for line in lines:
                import fnmatch
                self.assertFalse(fnmatch.fnmatch(must_keep, line) or must_keep.startswith(line.rstrip('*') + '/'), f'{line} removes {must_keep}')

    def test_overlay_copies_modes_and_symlinks_and_replaces_links_safely(self):
        root, src = self.tree(), Path(_tf.mkdtemp(prefix='wispos-overlay-'))
        (src / 'usr/bin').mkdir(parents=True); (src / 'usr/bin/tool').write_text('#!/bin/sh\n'); (src / 'usr/bin/tool').chmod(0o755)
        (src / 'usr/lib/x.target.wants').mkdir(parents=True); (src / 'usr/lib/x.target.wants/a.service').symlink_to('../a.service')
        (root / 'usr/bin/tool').symlink_to('/storage/elsewhere')
        stage_rootfs.overlay(root, src)
        self.assertFalse((root / 'usr/bin/tool').is_symlink()); self.assertEqual((root / 'usr/bin/tool').stat().st_mode & 0o777, 0o755)
        self.assertEqual(_os.readlink(root / 'usr/lib/x.target.wants/a.service'), '../a.service')
        self.assertFalse(Path('/storage/elsewhere').exists())

    def test_the_usb_host_bench_record_is_baked_only_when_the_build_names_it(self):
        root = self.tree()
        with unittest.mock.patch.dict(_os.environ, {'WISP_USB_HOST_BENCH': ''}): stage_rootfs.install_usb_host_bench(root)
        self.assertFalse((root / 'usr/share/wispos/usb-host-bench').exists())
        self.assertTrue((root / 'usr/share/wispos/usb-host-disabled').is_file())                         # no record: host mode is switched off in the image
        with unittest.mock.patch.dict(_os.environ, {'WISP_USB_HOST_BENCH': ' T1-T4 passed 2026-10-20 '}): stage_rootfs.install_usb_host_bench(root)
        self.assertEqual((root / 'usr/share/wispos/usb-host-bench').read_text(), 'T1-T4 passed 2026-10-20\n')
        self.assertFalse((root / 'usr/share/wispos/usb-host-disabled').exists())                         # a record: it is on

    def test_overlay_never_copies_host_python_bytecode(self):
        root, src = self.tree(), Path(_tf.mkdtemp(prefix='wispos-overlay-'))
        (src / 'usr/bin/__pycache__').mkdir(parents=True); (src / 'usr/bin/__pycache__/tool.cpython-312.pyc').write_bytes(b'\0')
        (src / 'usr/bin/tool').write_text('#!/bin/sh\n'); (src / 'usr/bin/stray.pyc').write_bytes(b'\0')
        stage_rootfs.overlay(root, src)
        self.assertTrue((root / 'usr/bin/tool').is_file())
        self.assertFalse((root / 'usr/bin/__pycache__').exists()); self.assertFalse((root / 'usr/bin/stray.pyc').exists())

    def test_sshd_settings_go_first_and_os_release_is_rebranded(self):
        root = self.tree(); (root / 'etc/ssh').mkdir(parents=True)
        (root / 'etc/ssh/sshd_config').write_text('PasswordAuthentication no\nPort 22\n')
        (root / 'etc/ssh/sshd_config.wisp').write_text('PasswordAuthentication yes\n')
        stage_rootfs.merge_sshd(root)
        self.assertEqual((root / 'etc/ssh/sshd_config').read_text().splitlines()[0], 'PasswordAuthentication yes')
        self.assertFalse((root / 'etc/ssh/sshd_config.wisp').exists())
        (root / 'etc/os-release').write_text('OS_NAME="ROCKNIX"\nOS_VERSION="1"\nHW_DEVICE="H700"\nHOME_URL="https://x"\nGIT_ORGANIZATION="ROCKNIX"\nGIT_REPO="distribution"\n'
                                             'BUILD_ID="c445081a59518f37d9776e5412dd7b14910696f7"\n')
        lock_dir = Path(tempfile.mkdtemp(prefix='wispos-lock-', dir=os.environ.get('TMPDIR'))); self.addCleanup(shutil.rmtree, lock_dir, True)
        def with_lock(content, env=None):
            lock = lock_dir / 'lockfile.json'; lock.write_text(content)
            (root / 'etc/os-release').write_text('OS_NAME="ROCKNIX"\nOS_VERSION="1"\nHW_DEVICE="H700"\nHOME_URL="https://x"\nGIT_ORGANIZATION="ROCKNIX"\nGIT_REPO="distribution"\n'
                                                 'BUILD_ID="c445081a59518f37d9776e5412dd7b14910696f7"\n')
            with unittest.mock.patch.object(stage_rootfs, 'LOCKFILE', lock), unittest.mock.patch.dict(os.environ, {'SOURCE_DATE_EPOCH': '1790879479', **(env or {})}, clear=False):   # 2026-10-01
                if not env or 'WISP_BUILD_ID' not in env: os.environ.pop('WISP_BUILD_ID', None)
                stage_rootfs.rebrand(root)
            return (root / 'etc/os-release').read_text()
        text = with_lock('{"version": "0.3.4"}')                         # no build id in the lockfile: the pinned build date, sequence 0
        self.assertIn('OS_NAME="WispOS"', text); self.assertIn('HW_DEVICE="H700"', text); self.assertNotIn('ROCKNIX', text); self.assertNotIn('GIT_REPO', text)
        self.assertRegex(text, r'OS_VERSION="\d{8}"')                # ROCKNIX's tools parse this shape
        self.assertIn('WISPOS_VERSION="0.3.4"', text); self.assertIn('VERSION_ID="0.3.4"', text); self.assertIn('PRETTY_NAME="WispOS 0.3.4"', text)       # the version comes from the lockfile
        self.assertIn('NAME="WispOS"', text); self.assertIn('ID="wispos"', text)
        self.assertIn('BUILD_ID="20261001.0"', text); self.assertNotIn('c445081a', text)                       # one build id, ours; ROCKNIX's git hash is gone
        self.assertEqual(text.count('BUILD_ID='), 1)
        self.assertIn('BUILD_ID="20261012.3"', with_lock('{"version": "0.3.4", "build_id": "20261012.3"}'))   # the release candidate's build id, from the lockfile
        self.assertIn('BUILD_ID="20261020.1"', with_lock('{"version": "0.3.4", "build_id": "20261012.3"}', {'WISP_BUILD_ID': '20261020.1'}))       # the release process may still override it
        with unittest.mock.patch.dict(os.environ, {'WISP_BUILD_ID': 'tuesday'}):
            with self.assertRaises(SystemExit): stage_rootfs.build_id()                                          # a build id that cannot compare like a version is refused
        for bad in ('{}', '{"version": "latest"}', '{"version": "0.1"}', '{"version": "0.1.0", "build_id": "tuesday"}', '{"version": "0.1.0", "build_id": 5}'):
            with self.assertRaises(SystemExit, msg=bad): with_lock(bad)                                          # a malformed lockfile stops the build
        real = stage_rootfs.lockfile()                                    # and the lockfile in the repository is a valid one
        self.assertRegex(real['version'], r'^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$'); self.assertRegex(real.get('build_id') or '20261001.0', r'^\d{8}\.\d+$')

    def test_the_setup_unit_starts_the_session_itself_and_sway_always_has_a_config(self):
        # Seen on the handheld: wisp-setup failed once (sway had crashed on its first start) and restarted; wisp-steam, ordered after
        # it, was evaluated while it was failing, skipped for want of READY, and never retried, so Steam did not start after setup.
        unit = (HERE / 'rootfs/usr/lib/systemd/system/wisp-setup.service').read_text()
        self.assertIn('ExecStartPost=-/usr/bin/systemctl --no-block start wisp-steam.service', unit)
        # sway.service starts before ROCKNIX's autostart writes the sway config, and ROCKNIX ships no default for sway-config to copy,
        # so the first start after a flash ran with no config and crashed (status 139). A default is staged from the kiosk config.
        import stage_rootfs, tempfile, os
        tree = Path(tempfile.mkdtemp(prefix='wispos-sway-', dir=os.environ.get('TMPDIR')))
        (tree / 'usr/share/sway').mkdir(parents=True); (tree / 'usr/share/sway/config.kiosk').write_text('seat * hide_cursor 1000\n')
        stage_rootfs.install_sway_default_config(tree)
        self.assertEqual((tree / 'usr/share/sway/config').read_text(), 'seat * hide_cursor 1000\n')
        self.assertIn("'usr/share/sway/config'", (HERE / 'check_rootfs.py').read_text())


    def test_setup_screens_run_at_native_resolution_and_the_default_display_is_restored_before_steam_starts(self):
        # Seen on the handheld: at half resolution the first-run screen (a WispOS app that draws at the size sway gives it) was soft and barely legible.
        unit = (HERE / 'rootfs/usr/lib/systemd/system/wisp-setup.service').read_text().splitlines()
        lines = [l for l in unit if l.startswith('Exec')]
        self.assertEqual([l.split('=', 1)[0] for l in lines], ['ExecStartPre', 'ExecStartPre', 'ExecStartPre', 'ExecStartPre', 'ExecStart', 'ExecStartPost', 'ExecStartPost', 'ExecStartPost'])
        self.assertIn('wisp-splash-say --ready', lines[1])                                                              # the boot splash ends once the compositor accepts clients
        self.assertIn('wisp-display native', lines[2]); self.assertIn('wisp-screen inhibit on', lines[3])               # setup shows progress: the screen stays on
        self.assertIn('wisp-screen inhibit off', lines[5]); self.assertIn('wisp-display default', lines[6]); self.assertIn('start wisp-steam.service', lines[7])
        # the helper itself, with a fake swaymsg: `native` is scale 1, `default` is read from the kiosk config, anything else is refused
        import os, stat, subprocess, tempfile
        d = Path(tempfile.mkdtemp(prefix='wispos-display-', dir=os.environ.get('TMPDIR'))); (d / 'swaymsg').write_text('#!/bin/sh\necho "$@" >> "%s/calls"\n' % d); (d / 'swaymsg').chmod(0o755)
        (d / 'sock').write_text('')                                    # the helper waits for SWAYSOCK to exist as a socket: give it a real one
        import socket; sk = socket.socket(socket.AF_UNIX); os.unlink(d / 'sock'); sk.bind(str(d / 'sock'))
        env = {**os.environ, 'PATH': f'{d}:{os.environ["PATH"]}', 'SWAYSOCK': str(d / 'sock'), 'WISP_KIOSK_CONFIG': str(HERE / 'rootfs/usr/share/sway/config.kiosk')}
        helper = str(HERE / 'rootfs/usr/bin/wisp-display')
        for arg in ('native', 'default'): subprocess.run(['sh', helper, arg], env=env, check=True)
        self.assertEqual((d / 'calls').read_text().splitlines(), ['output DSI-1 scale 1', 'output DSI-1 scale 1'])
        self.assertEqual(subprocess.run(['sh', helper, 'bogus'], env=env, capture_output=True).returncode, 2)
        sk.close()


    def test_the_screen_blanks_when_idle_and_wakes_on_a_button_press(self):
        # Operator request 2026-10-09: turn the screen off when it is not in use (backlight wear, battery). Measured on the handheld: sway `output DSI-1 power off`
        # sets bl_power=4 and DPMS Off, and `grim` cannot capture while it is off (so scripts wake it first).
        import importlib.machinery, importlib.util, os, subprocess, tempfile, time
        path = str(HERE / 'rootfs/usr/bin/wisp-screen-idle')
        spec = importlib.util.spec_from_loader('wisp_screen_idle', importlib.machinery.SourceFileLoader('wisp_screen_idle', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual(m.decide(True, 1000.0, 880.0, 120, False), 'off')          # idle for exactly the timeout
        self.assertIsNone(m.decide(True, 1000.0, 881.0, 120, False))                # one second short
        self.assertIsNone(m.decide(True, 1000.0, 0.0, 0, False))                    # timeout 0 = never blank
        self.assertIsNone(m.decide(True, 1000.0, 0.0, 120, True))                   # inhibited (first-run setup shows progress)
        self.assertIsNone(m.decide(False, 1000.0, 0.0, 120, False))                 # already off: nothing to do
        d = Path(tempfile.mkdtemp(prefix='wispos-screen-', dir=os.environ.get('TMPDIR')))
        m.TIMEOUT_FILE = str(d / 'timeout'); self.assertEqual(m.read_timeout(), 120)                                   # default when the file is missing
        (d / 'timeout').write_text('45\n'); self.assertEqual(m.read_timeout(), 45)
        (d / 'timeout').write_text('0'); self.assertEqual(m.read_timeout(), 0)
        (d / 'timeout').write_text('garbage'); self.assertEqual(m.read_timeout(), 120)
        # the command, with a fake swaymsg and fake /run and backlight files
        (d / 'swaymsg').write_text('#!/bin/sh\necho "$@" >> "%s/calls"\n' % d); (d / 'swaymsg').chmod(0o755); (d / 'bl_power').write_text('4\n')
        env = {**os.environ, 'PATH': f'{d}:{os.environ["PATH"]}', 'WISP_SCREEN_RUN': str(d), 'WISP_BACKLIGHT': str(d / 'bl_power'), 'SWAYSOCK': '/nonexistent'}
        run = lambda *a: subprocess.run(['sh', str(HERE / 'rootfs/usr/bin/wisp-screen'), *a], env=env, capture_output=True, text=True)
        self.assertEqual(run('status').stdout.strip(), 'off'); (d / 'bl_power').write_text('0\n'); self.assertEqual(run('status').stdout.strip(), 'on')
        run('wake'); self.assertTrue((d / 'wisp-screen-wake').exists()); run('off'); run('on')
        self.assertEqual((d / 'calls').read_text().splitlines(), ['output DSI-1 power on', 'output DSI-1 power off', 'output DSI-1 power on'])
        run('inhibit', 'on'); self.assertTrue((d / 'wisp-screen-inhibit').exists()); run('inhibit', 'off'); self.assertFalse((d / 'wisp-screen-inhibit').exists())
        self.assertEqual(run('inhibit', 'maybe').returncode, 2); self.assertEqual(run().returncode, 2)
        unit = (HERE / 'rootfs/usr/lib/systemd/system/wisp-screen-idle.service').read_text()
        self.assertIn('ExecStart=/usr/bin/wisp-screen-idle', unit); self.assertEqual(os.readlink(HERE / 'rootfs/usr/lib/systemd/system/wispos.target.wants/wisp-screen-idle.service'), '../wisp-screen-idle.service')


    def _tf2_card_world(self, card=True, card_sectors=250_000_000, mounts=''):
        """A fake sysfs/dev/mounts tree with the boot card on controller 4020000.mmc and (optionally) a card on 4022000.mmc, fake partitioning/mkfs/mount tools that only
        record their arguments, and the env that points tf2-card at them. Returns (env, dir)."""
        import os, tempfile
        d = Path(tempfile.mkdtemp(prefix='wispos-tf2-', dir=os.environ.get('TMPDIR'))); (d / 'bin').mkdir(); (d / 'dev').mkdir(); (d / 'sys/block').mkdir(parents=True)
        for tool in ('parted', 'partprobe', 'mke2fs', 'mount', 'umount', 'blkid'):
            (d / 'bin' / tool).write_text('#!/bin/sh\necho "%s $@" >> "%s/calls"\n' % (tool, d)); (d / 'bin' / tool).chmod(0o755)
        cards = [('mmcblk0', '4020000.mmc', 'mmc0', 122_000_000)] + ([('mmcblk1', '4022000.mmc', 'mmc1', card_sectors)] if card else [])
        for name, host, mmc, sectors in cards:
            real = d / 'sys/devices/platform/soc' / host / 'mmc_host' / mmc / (mmc + ':0001') / 'block' / name; real.mkdir(parents=True); (real / 'size').write_text('%d\n' % sectors)
            os.symlink(os.path.relpath(real, d / 'sys/block'), d / 'sys/block' / name)
            for node in (name, name + 'p1'): (d / 'dev' / node).write_text('')
        (d / 'mounts').write_text(mounts.replace('@DEV@', str(d / 'dev')))
        env = {**os.environ, 'PATH': f'{d}/bin:{os.environ["PATH"]}', 'WISP_SYS': str(d / 'sys'), 'WISP_DEV': str(d / 'dev'), 'WISP_MOUNTS': str(d / 'mounts'),
               'WISP_TF2_MOUNT': str(d / 'tf2'), 'WISP_LOG_DIR': str(d / 'logs')}
        return env, d

    def _tf2_shells(self):
        import shutil
        shells = [['sh']]                                      # the device runs BusyBox ash; the host's sh is dash: the script must behave the same under both
        if shutil.which('busybox'): shells.append(['busybox', 'sh'])      # NOTE: the host's busybox sh runs ITS OWN mke2fs/mount/umount/partprobe applets before $PATH, so the fake tools
        return shells                                          # cannot stand in there: scenarios that reach the tools run under `sh` only, refusals and parsing under both

    def test_the_screen_timeout_can_be_read_and_set_through_wisp_screen(self):
        # Issue #6: the 120 s blank timeout was only changeable by writing /storage/wispos/config/screen-timeout by hand. `wisp-screen timeout [SECS|off|status]` is the one entry point
        # for scripts and the UI; the file stays the single authority (wisp-screen-idle re-reads it every second, so a change is live) and the value 0 (never blank) is how blanking is turned
        # off: the idle service must NOT be masked or stopped, because it is the only writer of /run/wisp-last-input, which wisp-compat-gate reads.
        import importlib.machinery, importlib.util, os, subprocess, tempfile
        idle = str(HERE / 'rootfs/usr/bin/wisp-screen-idle')
        spec = importlib.util.spec_from_loader('wisp_screen_idle3', importlib.machinery.SourceFileLoader('wisp_screen_idle3', idle))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        for text, want in (('off', 0), ('0', 0), ('45', 45), (' 300 ', 300), ('600\n', 600), ('86400', 86400), ('OFF', 0)):
            self.assertEqual(m.parse_timeout(text), want, text)
        for bad in ('', 'abc', '-1', '1.5', '86401', '1e3', '12 13', 'never'):
            self.assertIsNone(m.parse_timeout(bad), bad)
        d = Path(tempfile.mkdtemp(prefix='wispos-screen-timeout-', dir=os.environ.get('TMPDIR')))
        # the command, through a wrapper that runs the real idle script against a temporary timeout file, and a fake systemctl that records any use
        f = d / 'config' / 'screen-timeout'
        (d / 'idle').write_text('#!/bin/sh\nexec python3 "%s" "$@"\n' % idle); (d / 'idle').chmod(0o755)
        (d / 'systemctl').write_text('#!/bin/sh\necho "$@" >> "%s/systemctl.calls"\n' % d); (d / 'systemctl').chmod(0o755)
        env = {**os.environ, 'PATH': f'{d}:{os.environ["PATH"]}', 'WISP_SCREEN_IDLE': str(d / 'idle'), 'WISP_SCREEN_TIMEOUT_FILE': str(f), 'WISP_SCREEN_RUN': str(d), 'SWAYSOCK': '/nonexistent'}
        run = lambda *a: subprocess.run(['sh', str(HERE / 'rootfs/usr/bin/wisp-screen'), 'timeout', *a], env=env, capture_output=True, text=True)
        r = run('status'); self.assertEqual((r.returncode, r.stdout.strip()), (0, '120'), r.stderr)                 # the default when no file exists
        self.assertEqual(run().stdout.strip(), '120')                                                              # bare `timeout` is status
        r = run('30'); self.assertEqual((r.returncode, r.stdout.strip()), (0, '30'), r.stderr)
        self.assertEqual(f.read_text(), '30\n'); self.assertEqual(run('status').stdout.strip(), '30')               # the file is created (with its directory) and read back
        r = run('off'); self.assertEqual((r.returncode, r.stdout.strip()), (0, 'off')); self.assertEqual(f.read_text(), '0\n'); self.assertEqual(run('status').stdout.strip(), 'off')
        self.assertEqual(run('0').stdout.strip(), 'off')
        run('300'); self.assertEqual(m.parse_timeout(f.read_text()), 300)
        before = f.read_text()
        for bad in ('abc', '-5', '99999999', '1.5'):
            r = run(bad); self.assertEqual(r.returncode, 2, bad); self.assertEqual(f.read_text(), before, 'a rejected value leaves the file alone: ' + bad); self.assertIn('usage', r.stderr)
        self.assertEqual([p.name for p in f.parent.iterdir()], ['screen-timeout'], 'no temporary file is left behind')
        self.assertFalse((d / 'systemctl.calls').exists(), 'setting the timeout never touches the idle service')
        self.assertIn('timeout', subprocess.run(['sh', str(HERE / 'rootfs/usr/bin/wisp-screen')], env=env, capture_output=True, text=True).stderr)   # the usage line names the verb

    def test_tf2_card_formats_only_the_card_in_the_tf2_slot_and_refuses_everything_else(self):
        # Operator 2026-10-10: wire up formatting and using the 128 GB card in the TF2 slot. The OS lives on the boot card (4020000.mmc): the tool must pick its target by the
        # controller, ignore any other device a caller names, and never unmount or partition anything the OS runs from.
        import subprocess
        tool = str(HERE / 'rootfs/usr/lib/wispos/tf2-card'); d_args = lambda d: str(d / 'dev')
        def run(shell, env, *args): return subprocess.run([*shell, tool, *args], env=env, capture_output=True, text=True)
        for shell in self._tf2_shells():
            with self.subTest(shell=shell[-1]):
                env, d = self._tf2_card_world(card=False)                                   # no card in TF2
                self.assertEqual(run(shell, env, 'device').returncode, 3)
                r = run(shell, env, 'format'); self.assertEqual(r.returncode, 1); self.assertIn('no card in the TF2 slot', r.stderr); self.assertFalse((d / 'calls').exists())

                env, d = self._tf2_card_world()                                             # card in TF2: the whole card, only that device, label as asked
                self.assertEqual(run(shell, env, 'device').stdout.strip(), 'mmcblk1')
                if shell == ['sh']:
                    r = run(shell, env, 'format', '--label', 'GAMES'); self.assertEqual(r.returncode, 0, r.stderr)
                    calls = (d / 'calls').read_text().splitlines()
                    self.assertEqual(calls[0], 'parted -s %s/mmcblk1 mklabel gpt mkpart primary ext4 1MiB 100%%' % d_args(d))
                    self.assertEqual(calls[-1], 'mke2fs -q -F -t ext4 -m 0 -L GAMES %s/mmcblk1p1' % d_args(d))
                    self.assertFalse([c for c in calls if 'mmcblk0' in c], calls)

                for target in ('/dev/mmcblk0', '/dev/mmcblk0p2', '/dev/sda', 'mmcblk0'):    # a caller-supplied device that is not the TF2 card is refused, nothing runs
                    env, d = self._tf2_card_world(); r = run(shell, env, 'format', '--device', target.replace('/dev', d_args(d)))
                    self.assertEqual(r.returncode, 1, target); self.assertIn('refusing', r.stderr); self.assertFalse((d / 'calls').exists(), target)
                if shell == ['sh']:
                    env, d = self._tf2_card_world(); self.assertEqual(run(shell, env, 'format', '--device', '%s/mmcblk1p1' % d_args(d)).returncode, 0)   # its own partition is fine

                env, d = self._tf2_card_world(mounts='@DEV@/mmcblk1p1 /storage ext4 rw 0 0\n')   # the OS running from the TF2 card: refuse, run nothing
                r = run(shell, env, 'format'); self.assertEqual(r.returncode, 1); self.assertIn('refusing', r.stderr); self.assertFalse((d / 'calls').exists())
                if shell == ['sh']:
                    env, d = self._tf2_card_world(); (d / 'mounts').write_text('%s/mmcblk1p1 %s ext4 rw 0 0\n' % (d / 'dev', d / 'tf2'))   # mounted where this tool mounts it: unmounted first
                    self.assertEqual(run(shell, env, 'format').returncode, 0); self.assertEqual((d / 'calls').read_text().splitlines()[0], 'umount %s' % (d / 'tf2'))
                    env, d = self._tf2_card_world(mounts='@DEV@/mmcblk1p2 /storage/games-external ext4 rw 0 0\n@DEV@/mmcblk1p2 /storage/roms ext4 rw 0 0\n')   # ROCKNIX's automount of an external card
                    self.assertEqual(run(shell, env, 'format').returncode, 0)
                    self.assertEqual((d / 'calls').read_text().splitlines()[:2], ['umount /storage/games-external', 'umount /storage/roms'])

                for bad in ('a b', 'x' * 17, '', 'a;b', '-F'):                              # labels: letters, digits . _ - only, at most 16
                    env, d = self._tf2_card_world(); r = run(shell, env, 'format', '--label', bad)
                    if bad == '-F':                                                         # a leading dash is a legal character; mke2fs gets it after -L as its value
                        if shell == ['sh']: self.assertEqual(r.returncode, 0)
                    else: self.assertEqual(r.returncode, 1, repr(bad)); self.assertFalse((d / 'calls').exists(), repr(bad))
                env, d = self._tf2_card_world(card_sectors=1_000_000); r = run(shell, env, 'format')   # ~488 MiB: not a card to format
                self.assertEqual(r.returncode, 1); self.assertIn('only', r.stderr); self.assertFalse((d / 'calls').exists())

                if shell == ['sh']:
                    env, d = self._tf2_card_world(); r = run(shell, env, 'mount'); self.assertEqual(r.returncode, 0, r.stderr)    # mount: only the TF2 partition, at the tool's mount point
                    self.assertEqual((d / 'calls').read_text().splitlines()[-1], 'mount -t ext4 -o noatime %s/mmcblk1p1 %s' % (d_args(d), d / 'tf2'))

    def test_steam_format_helpers_log_the_call_and_run_the_same_tf2_only_formatter(self):
        # Steam runs steamos-format-sdcard / steamos-format-device with arguments we have not seen yet. Both helpers are one line that hands over to tf2-card, which logs the call
        # first, uses only --label and --device, ignores the rest, and formats + mounts the TF2 card (or refuses a device that is not it).
        import os, subprocess
        for name in ('steamos-format-sdcard', 'steamos-format-device'):
            path = HERE / 'rootfs/usr/bin/steamos-polkit-helpers' / name; self.assertTrue(os.access(path, os.X_OK), name)
            self.assertIn('exec /usr/lib/wispos/tf2-card steam-format', path.read_text())
        tool = str(HERE / 'rootfs/usr/lib/wispos/tf2-card')
        for shell in self._tf2_shells():
            with self.subTest(shell=shell[-1]):
                env, d = self._tf2_card_world(); dev = str(d / 'dev')
                r = subprocess.run([*shell, tool, 'steam-format', 'steamos-format-sdcard', '--device', dev + '/mmcblk0', '--label', 'x', '--future-flag'], env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 1); self.assertFalse((d / 'calls').exists())                      # the boot card named by the caller: refused, nothing ran
                self.assertIn('argv (5): --device %s/mmcblk0 --label x --future-flag' % dev, (d / 'logs/steamos-format-sdcard.log').read_text())
                if shell != ['sh']: continue
                r = subprocess.run([*shell, tool, 'steam-format', 'steamos-format-device', '--label', 'GAMES', '--skip-validation'], env=env, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr); calls = (d / 'calls').read_text().splitlines()
                self.assertIn('mke2fs -q -F -t ext4 -m 0 -L GAMES %s/mmcblk1p1' % dev, calls); self.assertEqual(calls[-1], 'mount -t ext4 -o noatime %s/mmcblk1p1 %s' % (dev, d / 'tf2'))
                self.assertTrue((d / 'logs/steamos-format-device.log').exists())
                r = subprocess.run([*shell, tool, 'steam-format', 'steamos-format-sdcard'], env=env, capture_output=True, text=True)     # no arguments at all: default label, no crash
                self.assertEqual(r.returncode, 0, r.stderr)
        # the trim helper stays a placeholder: it logs the call and fails, and runs no formatter
        import tempfile
        t = Path(tempfile.mkdtemp(prefix='wispos-trim-', dir=os.environ.get('TMPDIR')))
        r = subprocess.run(['sh', str(HERE / 'rootfs/usr/bin/steamos-polkit-helpers/steamos-trim-devices'), '--x'], env={**os.environ, 'WISP_LOG_DIR': str(t)}, capture_output=True, text=True)
        self.assertEqual(r.returncode, 1); self.assertIn('argv (1): --x', (t / 'steamos-trim-devices.log').read_text())


    def test_the_tf2_card_is_mounted_by_our_udev_rule_and_not_by_rocknixs_automount(self):
        # The TF2 card must come back at /storage/wispos/tf2 after every boot or re-insert (Steam's library folder lives there). ROCKNIX's 99-automount.rules would mount it at
        # /storage/games-external instead, so the image prunes it; our rule and the two units do the mount. The controller name lives in the tool and in the rule: they must agree.
        import re
        rule = (HERE / 'rootfs/usr/lib/udev/rules.d/98-wisp-tf2.rules').read_text(); tool = (HERE / 'rootfs/usr/lib/wispos/tf2-card').read_text()
        host = re.search(r'TF2_HOST=\$\{WISP_TF2_HOST:-([0-9a-f]+\.mmc)\}', tool).group(1)
        self.assertIn('DEVPATH!="*/%s/*"' % host, rule); self.assertEqual(re.findall(r'\b[0-9a-f]{7}\.mmc\b', rule), [host])
        self.assertIn('ENV{ID_FS_TYPE}=="ext4"', rule); self.assertIn('restart wisp-tf2-mount.service', rule); self.assertIn('restart wisp-tf2-umount.service', rule)
        for unit, verb in (('wisp-tf2-mount', 'mount'), ('wisp-tf2-umount', 'umount')):
            text = (HERE / 'rootfs/usr/lib/systemd/system' / (unit + '.service')).read_text()
            self.assertIn('ExecStart=/usr/lib/wispos/tf2-card %s\n' % verb, text); self.assertIn('Type=oneshot', text)
        pruned = [l.strip() for l in (HERE / 'rootfs-prune.txt').read_text().splitlines() if l.strip() and not l.startswith('#')]
        self.assertIn('usr/lib/udev/rules.d/99-automount.rules', pruned)
        self.assertNotIn('usr/lib/udev/rules.d/98-wisp-tf2.rules', pruned)

    def test_tf2_card_unmount_is_clean_while_the_card_is_there_and_lazy_after_it_was_pulled(self):
        import subprocess
        tool = str(HERE / 'rootfs/usr/lib/wispos/tf2-card')
        for card, expected in ((True, 'umount'), (False, 'umount -l')):
            env, d = self._tf2_card_world(card=card)
            (d / 'mounts').write_text('%s/mmcblk1p1 %s ext4 rw 0 0\n' % (d / 'dev', d / 'tf2'))
            r = subprocess.run(['sh', tool, 'umount'], env=env, capture_output=True, text=True); self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual((d / 'calls').read_text().strip(), '%s %s' % (expected, d / 'tf2'))
        env, d = self._tf2_card_world(); r = subprocess.run(['sh', tool, 'umount'], env=env, capture_output=True, text=True)      # not mounted: nothing runs
        self.assertEqual(r.returncode, 0); self.assertFalse((d / 'calls').exists())


    def _gate(self):
        import importlib.machinery, importlib.util
        path = str(HERE / 'rootfs/usr/bin/wisp-compat-gate')
        spec = importlib.util.spec_from_loader('wisp_compat_gate', importlib.machinery.SourceFileLoader('wisp_compat_gate', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m); return m

    def test_compat_gate_decision_table_follows_the_locked_spec(self):
        # docs/WISPOS-COMPAT-LAYER-SPEC.md section 5.2 (operator 2026-10-10): no game, 5 min quiet, no Steam download, battery start 50% / pause 25%, thermal pause 70 / resume 60.
        g = self._gate()
        ok = dict(game=False, idle_s=600, steam_busy=False, has_battery=True, on_ac=True, battery_pct=80, temp_c=45.0)
        def d(phase='idle', **kw): return g.decide({**ok, **kw}, phase)
        self.assertEqual(d(), (True, 'all conditions met'))
        self.assertFalse(d(game=True)[0]); self.assertIn('game', d(game=True)[1])
        self.assertFalse(d(idle_s=299)[0]); self.assertTrue(d(idle_s=300)[0])                                    # exactly 5 minutes of quiet is enough
        self.assertFalse(d(steam_busy=True)[0])
        for phase in ('idle', 'running', 'paused'): self.assertTrue(d(phase, on_ac=True, battery_pct=3)[0], phase)            # plugged in: no battery rule at all
        self.assertFalse(d('idle', on_ac=False, battery_pct=49)[0]); self.assertTrue(d('idle', on_ac=False, battery_pct=50)[0])    # a build STARTS on battery at 50%
        self.assertTrue(d('running', on_ac=False, battery_pct=26)[0]); self.assertFalse(d('running', on_ac=False, battery_pct=25)[0])   # and pauses at 25%
        self.assertTrue(d('paused', on_ac=False, battery_pct=26)[0]); self.assertFalse(d('paused', on_ac=False, battery_pct=25)[0])   # resumes above 25%, not 50%
        self.assertFalse(d(on_ac=False, battery_pct=None)[0]); self.assertIn('unreadable', d(on_ac=False, battery_pct=None)[1])        # fail closed
        self.assertTrue(d(has_battery=False, on_ac=False, battery_pct=None)[0])                                  # no battery at all (a dev board): no battery rule
        self.assertTrue(d('running', temp_c=69.9)[0]); self.assertFalse(d('running', temp_c=70.0)[0])             # a running build pauses at 70 C
        self.assertTrue(d('paused', temp_c=60.0)[0]); self.assertFalse(d('paused', temp_c=60.1)[0])              # and resumes at 60 C
        self.assertFalse(d('idle', temp_c=65.0)[0]); self.assertTrue(d('running', temp_c=65.0)[0])               # hysteresis: 65 C keeps a running build going but does not start one
        self.assertFalse(d(temp_c=None)[0]); self.assertIn('unreadable', d(temp_c=None)[1])
        self.assertEqual([g.next_phase(p, a) for p, a in (('idle', True), ('idle', False), ('running', False), ('paused', False), ('paused', True))], ['running', 'idle', 'paused', 'paused', 'running'])

    def test_compat_gate_readers_use_the_signals_the_device_really_has(self):
        import os, tempfile
        g = self._gate(); d = Path(tempfile.mkdtemp(prefix='wispos-gate-', dir=os.environ.get('TMPDIR')))
        # processes: only `reaper ... SteamLaunch` is a game (Steam's own helpers and a program merely mentioning it are not)
        proc = d / 'proc'
        for pid, argv in (('10', 'steam'), ('11', '/usr/bin/python3\0wisp-compat-gate'), ('12', 'grep\0SteamLaunch')):
            (proc / pid).mkdir(parents=True); (proc / pid / 'cmdline').write_bytes(argv.encode())
        self.assertFalse(g.game_running(str(proc)))
        (proc / '13').mkdir(); (proc / '13' / 'cmdline').write_bytes(b'/home/x/ubuntu12_32/reaper\0SteamLaunch\0AppId=217200\0--\0proton')
        self.assertTrue(g.game_running(str(proc)))
        # Steam: StateFlags 4 = installed, 1026 = install in progress (both read on the device), 2 = update waiting but not running
        lib = d / 'steamapps'; lib.mkdir()
        def acf(name, flags): (lib / name).write_text('"AppState"\n{\n\t"appid"\t\t"1"\n\t"StateFlags"\t\t"%d"\n}\n' % flags)
        acf('appmanifest_1.acf', 4); acf('appmanifest_2.acf', 2); self.assertFalse(g.steam_busy([str(lib)]))
        acf('appmanifest_3.acf', 1026); self.assertTrue(g.steam_busy([str(lib)])); self.assertFalse(g.steam_busy([str(d / 'nowhere')]))
        vdf = d / 'libraryfolders.vdf'; vdf.write_text('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"/a/steam"\n\t}\n\t"1"\n\t{\n\t\t"path"\t\t"/b/tf2"\n\t}\n}\n')
        self.assertEqual(g.steamapps_dirs(str(vdf)), ['/a/steam/steamapps', '/b/tf2/steamapps']); self.assertEqual(g.steamapps_dirs(str(d / 'none')), [])
        # power: battery capacity and ONLINE of the other supplies (never `status`); thermal: the hottest zone
        sysr = d / 'sys'
        def sup(name, **files):
            (sysr / 'class/power_supply' / name).mkdir(parents=True)
            for k, v in files.items(): (sysr / 'class/power_supply' / name / k).write_text(str(v) + '\n')
        sup('axp-battery', type='Battery', capacity=42, status='Not charging'); sup('axp-usb', type='USB', online=0)
        self.assertEqual(g.power(str(sysr)), (True, False, 42))
        (sysr / 'class/power_supply/axp-usb/online').write_text('1\n'); self.assertEqual(g.power(str(sysr)), (True, True, 42))
        self.assertEqual(g.power(str(d / 'empty')), (False, False, None))
        self.assertIsNone(g.max_temp_c(str(sysr)))
        for n, t in (('0', 41500), ('1', 63250)): (sysr / 'class/thermal' / ('thermal_zone' + n)).mkdir(parents=True); (sysr / 'class/thermal' / ('thermal_zone' + n) / 'temp').write_text('%d\n' % t)
        self.assertEqual(g.max_temp_c(str(sysr)), 63.25)
        # idle: the marker's age; with no marker, the uptime (nobody has touched the device since boot)
        up = d / 'uptime'; up.write_text('1234.5 999.0\n'); marker = d / 'last-input'
        self.assertEqual(g.idle_seconds(now=5000.0, marker=str(marker), uptime_file=str(up)), 1234.5)
        marker.write_text(''); os.utime(marker, (4000.0, 4000.0)); self.assertEqual(g.idle_seconds(now=5000.0, marker=str(marker), uptime_file=str(up)), 1000.0)
        # the command line: JSON on stdout, exit 0 when allowed and 1 when not
        import subprocess, json
        env = {**os.environ, 'WISP_GATE_RUN': str(d)}
        r = subprocess.run(['python3', str(HERE / 'rootfs/usr/bin/wisp-compat-gate'), '--phase', 'paused'], env=env, capture_output=True, text=True)
        out = json.loads(r.stdout); self.assertIn(r.returncode, (0, 1)); self.assertEqual(r.returncode == 0, out['allowed']); self.assertEqual(sorted(out['state']), sorted(['game', 'idle_s', 'steam_busy', 'has_battery', 'on_ac', 'battery_pct', 'temp_c']))

    def test_screen_idle_marks_every_input_for_the_compat_gate(self):
        import importlib.machinery, importlib.util, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-screen-idle')
        spec = importlib.util.spec_from_loader('wisp_screen_idle2', importlib.machinery.SourceFileLoader('wisp_screen_idle2', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        d = Path(tempfile.mkdtemp(prefix='wispos-touch-', dir=os.environ.get('TMPDIR'))); marker = d / 'wisp-last-input'
        m.touch(str(marker)); self.assertTrue(marker.exists()); os.utime(marker, (1000.0, 1000.0)); m.touch(str(marker)); self.assertGreater(marker.stat().st_mtime, 1000.0)
        m.touch(str(d / 'no/such/dir/marker'))                                      # an unwritable marker must not crash the watcher
        self.assertIn("touch(RUN + '/wisp-last-input')", Path(path).read_text())    # and main() really calls it on a press
        gate = (HERE / 'rootfs/usr/bin/wisp-compat-gate').read_text(); self.assertIn("'/wisp-last-input'", gate)    # the reader and the writer name the same file


    def _fake_fex_deb(self, tweak=None):
        """A tiny .deb shaped like fex-emu-wine (same member paths, small contents) plus a lock that pins it. `tweak(lock, files)` can break something. Returns (deb, lock_path, dir)."""
        import hashlib, json, os, subprocess, tempfile
        d = Path(tempfile.mkdtemp(prefix='wispos-fexdeb-', dir=os.environ.get('TMPDIR'))); root = d / 'pkg'
        files = {'usr/lib/wine/aarch64-windows/libwow64fex.dll': b'W' * 300, 'usr/lib/wine/aarch64-windows/libarm64ecfex.dll': b'A' * 400,
                 'usr/lib/wine/aarch64-unix/libwow64fex.so': b'w' * 50, 'usr/lib/wine/aarch64-unix/libarm64ecfex.so': b'a' * 60, 'usr/share/doc/fex-emu-wine/copyright': b'MIT\n'}
        for name, blob in files.items(): (root / name).parent.mkdir(parents=True, exist_ok=True); (root / name).write_bytes(blob)
        (root / 'DEBIAN').mkdir(); (root / 'DEBIAN/control').write_text('Package: fex-emu-wine\nVersion: 1-1~n\nArchitecture: arm64\nMaintainer: t <t@example.org>\nDescription: test\n')
        deb = d / 'p.deb'; subprocess.run(['dpkg-deb', '--build', '-Zxz', str(root), str(deb)], check=True, capture_output=True)
        sha = lambda b: hashlib.sha256(b).hexdigest()
        lock = {'schema': 1, 'package': 'fex-emu-wine', 'version': '1-1~n', 'architecture': 'arm64', 'sha256': sha(deb.read_bytes()), 'tool': 'ToolX',
                'license_member': 'usr/share/doc/fex-emu-wine/copyright', 'license_sha256': sha(files['usr/share/doc/fex-emu-wine/copyright']),
                'files': [{'member': m, 'sha256': sha(files[m]), 'tool_path': 'files/lib/wine/' + m.split('/lib/wine/')[1]} for m in list(files)[:4]]}
        if tweak: tweak(lock, files, deb)
        lock_path = d / 'lock.json'; lock_path.write_text(json.dumps(lock)); return deb, lock_path, d

    def test_official_fex_package_is_unpacked_only_as_pinned_and_refused_otherwise(self):
        import hashlib, json, os, stage_rootfs, tempfile
        deb, lock_path, d = self._fake_fex_deb(); tree = d / 'tree'
        stage_rootfs.install_official_fex(tree, deb, lock_path)
        out = tree / 'usr/share/wispos/fex-official'
        self.assertEqual(sorted(p.name for p in out.iterdir()), ['copyright', 'libarm64ecfex.dll', 'libarm64ecfex.so', 'libwow64fex.dll', 'libwow64fex.so', 'official-fex.lock.json'])
        self.assertEqual((out / 'libwow64fex.dll').read_bytes(), b'W' * 300)
        def wrong_pkg(lock, files, deb): lock['sha256'] = '0' * 64
        def wrong_member(lock, files, deb): lock['files'][2]['sha256'] = '1' * 64
        def wrong_version(lock, files, deb): lock['version'] = '2-1~n'
        def missing_member(lock, files, deb): lock['files'].append({'member': 'usr/lib/wine/aarch64-unix/absent.so', 'sha256': '2' * 64, 'tool_path': 'files/lib/wine/aarch64-unix/absent.so'})
        for tweak, text in ((wrong_pkg, 'does not match'), (wrong_member, 'does not match'), (wrong_version, 'lock says'), (missing_member, 'lacks')):
            deb, lock_path, d = self._fake_fex_deb(tweak)
            with self.assertRaises(SystemExit) as ctx: stage_rootfs.install_official_fex(d / 'tree2', deb, lock_path)
            self.assertIn(text, str(ctx.exception), tweak.__name__)

    def test_official_fex_lock_pins_the_tested_package_and_the_tool_it_targets(self):
        import json
        lock = json.loads((HERE.parent / 'steam/official-fex.lock.json').read_text())
        tool = json.loads((HERE.parent / 'steam/compat-tool.lock.json').read_text())
        self.assertEqual(lock['tool'], tool['name']); self.assertEqual((lock['package'], lock['version']), ('fex-emu-wine', '2610-1~n'))
        self.assertTrue(lock['url'].startswith('https://ppa.launchpadcontent.net/fex-emu/fex/')); self.assertEqual(len(lock['sha256']), 64)
        self.assertEqual(sorted(f['tool_path'] for f in lock['files']), ['files/lib/wine/aarch64-unix/libarm64ecfex.so', 'files/lib/wine/aarch64-unix/libwow64fex.so',
                         'files/lib/wine/aarch64-windows/libarm64ecfex.dll', 'files/lib/wine/aarch64-windows/libwow64fex.dll'])
        self.assertEqual({f['member'].rsplit('/', 1)[1]: f['sha256'][:12] for f in lock['files']}, {'libwow64fex.dll': '835d356dd8ad', 'libarm64ecfex.dll': '7f2d54ae1a09',
                         'libwow64fex.so': '75677d6edf7e', 'libarm64ecfex.so': 'fe7d3857f429'})     # the hashes the device test (Quake III 26.4 fps) ran with
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertLess(session.index('wisp-fex-official'), session.index('wisp-proton-patch'))        # applied before every Steam start

    def test_wisp_fex_official_installs_keeps_originals_once_and_is_idempotent(self):
        import importlib.machinery, importlib.util, json, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-fex-official')
        spec = importlib.util.spec_from_loader('wisp_fex_official', importlib.machinery.SourceFileLoader('wisp_fex_official', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        import hashlib
        d = Path(tempfile.mkdtemp(prefix='wispos-fexov-', dir=os.environ.get('TMPDIR'))); src, tools = d / 'src', d / 'tools'; src.mkdir(); tools.mkdir()
        blobs = {'libwow64fex.dll': b'NEW-W', 'libarm64ecfex.dll': b'NEW-A', 'libwow64fex.so': b'NEW-w', 'libarm64ecfex.so': b'NEW-a'}
        sub = {'libwow64fex.dll': 'aarch64-windows', 'libarm64ecfex.dll': 'aarch64-windows', 'libwow64fex.so': 'aarch64-unix', 'libarm64ecfex.so': 'aarch64-unix'}
        for n, b in blobs.items(): (src / n).write_bytes(b)
        lock = {'package': 'fex-emu-wine', 'version': '9', 'sha256': 'f' * 64, 'tool': 'ToolX',
                'files': [{'member': 'usr/lib/wine/%s/%s' % (sub[n], n), 'sha256': hashlib.sha256(b).hexdigest(), 'tool_path': 'files/lib/wine/%s/%s' % (sub[n], n)} for n, b in blobs.items()]}
        (src / 'official-fex.lock.json').write_text(json.dumps(lock))
        self.assertEqual(m.apply(src, tools), (True, ['ToolX is not installed yet: nothing to do']))                      # first run still downloading the tool
        win = tools / 'ToolX/files/lib/wine/aarch64-windows'; win.mkdir(parents=True)
        (win / 'libwow64fex.dll').write_bytes(b'OLD-W'); (win / 'libarm64ecfex.dll').write_bytes(b'OLD-A'); os.chmod(win / 'libwow64fex.dll', 0o444)
        ok, msgs = m.apply(src, tools); self.assertTrue(ok); self.assertEqual(len(msgs), 4)
        for n, b in blobs.items(): self.assertEqual((tools / 'ToolX/files/lib/wine' / sub[n] / n).read_bytes(), b, n)
        self.assertEqual((tools / 'ToolX/.wisp-orig/files/lib/wine/aarch64-windows/libwow64fex.dll').read_bytes(), b'OLD-W')     # the original is kept
        self.assertFalse((tools / 'ToolX/.wisp-orig/files/lib/wine/aarch64-unix/libwow64fex.so').exists())                         # a file that did not exist has no original
        self.assertEqual(json.loads((tools / 'ToolX/.wisp-fex-official.json').read_text())['version'], '9')
        self.assertEqual(m.apply(src, tools), (True, ['ToolX: official FEX modules 9 already in place']))                         # idempotent
        (win / 'libwow64fex.dll').write_bytes(b'REPLACED-BY-A-NEW-ARCHIVE'); ok, msgs = m.apply(src, tools)                         # a new tool archive puts the old file back
        self.assertEqual((ok, len(msgs)), (True, 1)); self.assertEqual((win / 'libwow64fex.dll').read_bytes(), b'NEW-W')
        self.assertEqual((tools / 'ToolX/.wisp-orig/files/lib/wine/aarch64-windows/libwow64fex.dll').read_bytes(), b'OLD-W')       # the FIRST original stays
        (src / 'libarm64ecfex.so').write_bytes(b'CORRUPT'); (win / 'libwow64fex.dll').write_bytes(b'X')                              # damaged image: refuse, change nothing
        ok, msgs = m.apply(src, tools); self.assertFalse(ok); self.assertIn('image is damaged', msgs[0]); self.assertEqual((win / 'libwow64fex.dll').read_bytes(), b'X')
        (src / 'libarm64ecfex.so').write_bytes(b'NEW-a'); lock['files'][0]['tool_path'] = '../escape'; (src / 'official-fex.lock.json').write_text(json.dumps(lock))
        ok, msgs = m.apply(src, tools); self.assertFalse(ok); self.assertIn('unsafe path', msgs[0])


    def test_wisp_wow64_valve_requests_the_app_once_and_copies_only_a_tested_file(self):
        import hashlib, importlib.machinery, importlib.util, json, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-wow64-valve')
        spec = importlib.util.spec_from_loader('wisp_wow64_valve', importlib.machinery.SourceFileLoader('wisp_wow64_valve', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        d = Path(tempfile.mkdtemp(prefix='wispos-wow64-', dir=os.environ.get('TMPDIR'))); good = b'VALVE-GOOD-WOW64'
        lock = {'appid': 4628740, 'app_name': 'Proton 11.0 (ARM64)', 'app_dir': 'Proton 11.0 (ARM64)', 'source': 'files/lib/wine/aarch64-windows/wow64.dll', 'tool': 'ToolX',
                'tool_path': 'files/lib/wine/aarch64-windows/wow64.dll', 'known_good': [{'sha256': hashlib.sha256(good).hexdigest()}]}
        lib, tools, state = d / 'lib/steamapps', d / 'tools', d / 'state'
        acf = lib / 'appmanifest_4628740.acf'
        # 1. nothing installed, never requested: a minimal "update required" manifest is written ONCE, so Steam downloads it after sign-in without a dialog
        ok, msgs = m.run(lock, [lib], tools, state); self.assertTrue(ok); self.assertIn('asked Steam', msgs[0])
        text = acf.read_text(); self.assertIn('"appid"\t\t"4628740"', text); self.assertIn('"StateFlags"\t\t"2"', text); self.assertIn('"installdir"\t\t"Proton 11.0 (ARM64)"', text)
        # the user (or Steam) removes it again: we do not fight that, the request was made once
        acf.unlink(); ok, msgs = m.run(lock, [lib], tools, state); self.assertTrue(ok); self.assertFalse(acf.exists()); self.assertIn('not asking again', msgs[0])
        # 2. downloading (1026 = what a real install read) -> wait; the tool is not there yet -> nothing to do
        def manifest(flags): acf.write_text('"AppState"\n{\n\t"appid"\t\t"4628740"\n\t"StateFlags"\t\t"%d"\n}\n' % flags)
        manifest(1026); ok, msgs = m.run(lock, [lib], tools, state); self.assertTrue(ok); self.assertIn('waiting', msgs[0])
        src = lib / 'common/Proton 11.0 (ARM64)/files/lib/wine/aarch64-windows/wow64.dll'; src.parent.mkdir(parents=True); src.write_bytes(good)
        manifest(4); ok, msgs = m.run(lock, [lib], tools, state); self.assertTrue(ok); self.assertIn('nothing to do', msgs[0])
        # 3. installed + tested hash + tool present: copied atomically, original kept once; idempotent
        dest = tools / 'ToolX/files/lib/wine/aarch64-windows/wow64.dll'; dest.parent.mkdir(parents=True); dest.write_bytes(b'COMMUNITY'); os.chmod(dest, 0o444)
        ok, msgs = m.run(lock, [lib], tools, state); self.assertTrue(ok); self.assertEqual(dest.read_bytes(), good)
        self.assertEqual((tools / 'ToolX/.wisp-orig/files/lib/wine/aarch64-windows/wow64.dll').read_bytes(), b'COMMUNITY')
        self.assertIn('already in the tool', m.run(lock, [lib], tools, state)[1][0])
        # 4. Valve updates the app to a build nobody tested: NOT used (the tool keeps what it has), and the message says how to fix it
        src.write_bytes(b'VALVE-NEWER-BUILD'); ok, msgs = m.run(lock, [lib], tools, state); self.assertTrue(ok); self.assertIn('not a tested build', msgs[0]); self.assertEqual(dest.read_bytes(), good)
        # 5. a bad lock path is refused
        src.write_bytes(good); bad = dict(lock, tool_path='../x'); ok, msgs = m.run(bad, [lib], tools, state); self.assertFalse(ok); self.assertIn('unsafe path', msgs[0])
        # the real lock: pinned to the build that was tested on the device, the same tool the other overlay targets, and applied before every Steam start
        real = json.loads((HERE.parent / 'steam/valve-wow64.lock.json').read_text())
        self.assertEqual(real['known_good'][0]['sha256'][:12], 'b361749969b2'); self.assertEqual(real['tool'], json.loads((HERE.parent / 'steam/official-fex.lock.json').read_text())['tool'])
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertLess(session.index('wisp-fex-official'), session.index('wisp-wow64-valve')); self.assertLess(session.index('wisp-wow64-valve'), session.index('wisp-proton-patch'))


    def test_wow64_copy_runs_when_steam_rewrites_the_valve_proton_manifest_not_only_at_steam_start(self):
        # First flash (device, 2026-10-11): Steam installed Valve's Proton app by itself right after sign-in, minutes AFTER wisp-steam-session had run wisp-wow64-valve for that start, so the
        # copy needs its own trigger: a path unit on the app's manifest. Steam rewrites it often while downloading, so the service must not hit systemd's start limit.
        import json, os
        sysd = HERE / 'rootfs/usr/lib/systemd/system'; lock = json.loads((HERE.parent / 'steam/valve-wow64.lock.json').read_text())
        path_unit = (sysd / 'wisp-wow64-valve.path').read_text(); service = (sysd / 'wisp-wow64-valve.service').read_text()
        self.assertIn('PathChanged=/storage/wispos/runtime/steam/steamapps/appmanifest_%d.acf' % lock['appid'], path_unit)
        self.assertIn('Unit=wisp-wow64-valve.service', path_unit); self.assertIn('ExecStart=/usr/bin/wisp-wow64-valve\n', service); self.assertIn('StartLimitIntervalSec=0', service)
        self.assertEqual(os.readlink(sysd / 'wispos.target.wants/wisp-wow64-valve.path'), '../wisp-wow64-valve.path')


    def test_steam_starts_in_the_steamos_gamepad_ui_mode(self):
        # Operator decision 2026-10-14: -steamos3 AND -steamdeck, otherwise Steam believes it is in plain Big Picture mode.
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        line = [l for l in session.splitlines() if 'steamrtarm64/steam" -gamepadui' in l]
        self.assertEqual(len(line), 1); self.assertIn('-gamepadui -steamos3 -steamdeck', line[0])

    def test_the_client_channel_is_left_to_steams_deck_mode(self):
        # Found 2026-10-14 in the QEMU session of the candidate: in Deck mode Steam wants the `steamdeck_stable` client channel and rewrites package/beta to it. The wrapper forced
        # `publicbeta` (a file write at every start, and `-clientbeta publicbeta`), so on every COLD start Steam looked its installed version up under one channel's manifest name and
        # downloaded under the other's ("installed version 0") and re-downloaded and re-extracted its whole package (7 min in QEMU, ~10 min on the device) at every power-on.
        # Same VM, same image: forced channel = an extraction at every cold start; no override = "Download skipped ... installed version 1791918634", no extraction.
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        code = [l for l in session.splitlines() if not l.lstrip().startswith('#')]
        self.assertEqual([l for l in code if 'package/beta' in l], [], 'the wrapper must not write Steam\'s beta (channel) file')
        self.assertEqual([l for l in code if '-clientbeta' in l], [], 'the wrapper must not override the channel for the session')

    def test_steams_ui_is_told_it_runs_as_the_deck_session(self):
        # Found on the handheld 2026-10-14/15 (docs/WISPOS-NOTES.md, "Power-menu/gamescope mechanism"): the Steam UI's power menu offers "Switch to Desktop" only when its
        # IN_GAMESCOPE flag is set, and that flag is `Plat_IsGamescope()` = a non-empty GAMESCOPE_WAYLAND_DISPLAY in the client's environment (libtier0_s.so). Without it the
        # desktop mode (journey 5) cannot be reached from the Steam UI at all; with it (a volatile drop-in on the device) the entry appeared and the switch worked.
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        code = [l for l in session.splitlines() if not l.lstrip().startswith('#')]
        lines = [l for l in code if 'GAMESCOPE_WAYLAND_DISPLAY' in l]
        self.assertEqual(len(lines), 1, 'exported exactly once, by the session wrapper')
        self.assertRegex(lines[0], r'^export GAMESCOPE_WAYLAND_DISPLAY=\S+$', 'a non-empty value is all Steam checks')

    def test_the_default_display_is_the_panels_full_resolution(self):
        # Operator decision 2026-10-14 (release): no half-resolution mode; the panel runs at its own 720x480 (scale 1). Half resolution had been the default since 2026-10-09.
        kiosk = (HERE / 'rootfs/usr/share/sway/config.kiosk').read_text().splitlines()
        self.assertEqual([l for l in kiosk if l.startswith('output DSI-1 scale ')], ['output DSI-1 scale 1'])
        self.assertEqual([l for l in kiosk if l.startswith('output DSI-1 scale_filter')], ['output DSI-1 scale_filter linear'])
        # ROCKNIX's 111-sway-init appends `output DSI-1 ...` lines after copying this file; none of those may set a scale (the base config is the only place)
        self.assertEqual(len([l for l in kiosk if 'scale' in l and l.startswith('output')]), 2)


    def test_games_run_with_wined3d_and_wine_debug_logging_off_by_default(self):
        # Measured on the handheld: with Wine tracing on, BIT.TRIP RUNNER took many minutes to load; with WINEDEBUG=-all about 2. No Vulkan driver exists for the GPU,
        # so DXVK cannot start ("Failed to create Vulkan instance"): WineD3D must be the default. Both are exported before Steam starts so every game inherits them.
        text = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        line = [l for l in text.splitlines() if l.startswith('export PROTON_USE_WINED3D=1')]
        self.assertEqual(line, ['export PROTON_USE_WINED3D=1 WINEDEBUG=-all'])
        self.assertLess(text.index(line[0]), text.index('steamrtarm64/steam" -gamepadui'))      # exported before Steam is started
        self.assertNotIn('PROTON_LOG', text.replace('Never set PROTON_LOG', ''))                # no Proton logging default anywhere in the launcher


    def test_games_start_fullscreen_without_a_title_bar(self):
        # Seen on the handheld: without gamescope BIT.TRIP RUNNER (X11 class steam_app_63710) ran as a floating 720x405 window with a 25 px title bar on the
        # 720x480 panel. ROCKNIX's 111-sway-init copies usr/share/sway/config.kiosk over the user config on every boot, so the rule lives there.
        kiosk = (HERE / 'rootfs/usr/share/sway/config.kiosk').read_text()
        for base in ('seat * hide_cursor 1000', 'default_border none', 'exec_always mako'): self.assertIn(base, kiosk)     # ROCKNIX's own lines are kept
        rule = [l for l in kiosk.splitlines() if l.startswith('for_window') and 'steam_app_' in l]
        self.assertEqual(len(rule), 1); self.assertIn('fullscreen enable', rule[0]); self.assertIn('border none', rule[0])
        import re
        self.assertTrue(re.search(rule[0].split('"')[1], 'steam_app_63710')); self.assertFalse(re.search(rule[0].split('"')[1], 'steam'))   # Steam's own window is not a game


    def test_every_library_the_updated_steam_client_needs_is_baked_and_in_the_contract(self):
        # Seen on the handheld: after Steam updated itself, steamui.so needed libva ("Failed to load steamui.so", exit 255 in a loop).
        # The client's own dependencies, found by scanning the UPDATED client on the device, are these sonames; each must come from a
        # pinned package in the lock and be required by check_rootfs so a build without it fails.
        import json, stage_rootfs
        lock = {x['package'] for x in json.loads((HERE.parent / 'steam/native-libs.lock.json').read_text())['packages']}
        self.assertTrue(set(stage_rootfs.STEAM_LIB_PACKAGES) <= lock)
        contract = (HERE / 'check_rootfs.py').read_text()
        for soname in ('libibus-1.0.so.5', 'libva.so.2', 'libvdpau.so.1', 'libssh2.so.1', 'libbrotlidec.so.1', 'libbrotlicommon.so.1', 'libasyncns.so.0'):
            self.assertIn(f"'usr/lib/wispos/lib/{soname}'", contract, soname)
        self.assertIn('/usr/lib/wispos/lib:', (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text())

    def test_wifi_is_never_switched_off_at_boot(self):
        # Seen on the handheld: ROCKNIX's autostart 080-network runs `wifictl disable` whenever the setting wifi.enabled is 0, and
        # ROCKNIX's default system.cfg says 0. About 18 s into every boot it deauthenticated the radio and set the rfkill soft block, which
        # systemd-rfkill then saved and restored. Wi-Fi only survived while the first-run screen re-enabled it; after setup every reboot
        # left the device off the network. WispOS has no airplane-mode setting, so its 080-network only ever unblocks the radio.
        import subprocess, tempfile, os, stat
        script = HERE / 'rootfs/usr/lib/autostart/common/080-network'
        self.assertTrue(script.is_file(), 'the ROCKNIX 080-network must be overridden by the WispOS one')
        self.assertTrue(script.stat().st_mode & stat.S_IXUSR)
        text = script.read_text()
        for forbidden in ('wifictl disable', 'get_setting', 'wifi.enabled'):
            self.assertNotIn(forbidden, text)
        bindir = Path(tempfile.mkdtemp(prefix='wispos-rfkill-', dir=os.environ.get('TMPDIR')))
        log = bindir / 'calls'
        (bindir / 'rfkill').write_text(f'#!/bin/sh\necho "$@" >> {log}\n'); (bindir / 'rfkill').chmod(0o755)
        for name in ('wifictl', 'nmcli'):
            (bindir / name).write_text(f'#!/bin/sh\necho "{name} $@" >> {log}.bad\n'); (bindir / name).chmod(0o755)
        subprocess.run(['sh', str(script)], check=True, env={'PATH': f'{bindir}:/usr/bin:/bin'}, timeout=20)
        self.assertEqual(log.read_text().split(), ['unblock', 'wifi'])
        self.assertFalse((bindir / 'calls.bad').exists())


class CheckRootfsTests(unittest.TestCase):
    def test_unit_check_finds_missing_programs_follows_links_and_skips_runtime_paths(self):
        root = Path(_tf.mkdtemp(prefix='wispos-check-'))
        (root / 'usr/bin').mkdir(parents=True); (root / 'usr/bin/ok').write_text('x'); (root / 'bin').symlink_to('/usr/bin')
        units = root / 'usr/lib/systemd/system'; units.mkdir(parents=True)
        (units / 'a.service').write_text('[Service]\nExecStartPre=-/bin/ok\nExecStart=/usr/bin/missing\nExecStop=/storage/wispos/x\n')
        found = check_rootfs.unit_report(root)
        self.assertEqual(found, {'usr/lib/systemd/system/a.service': ['ExecStart=/usr/bin/missing']})

    def test_dangling_enablement_link_and_default_target_are_reported(self):
        root = Path(_tf.mkdtemp(prefix='wispos-check-')); units = root / 'usr/lib/systemd/system'
        (units / 'w.target.wants').mkdir(parents=True); (units / 'w.target.wants/gone.service').symlink_to('../gone.service')
        self.assertEqual(check_rootfs.wants_report(root), ['usr/lib/systemd/system/w.target.wants/gone.service'])
        self.assertIn('default.target -> wispos.target', check_rootfs.contract_report(root))

    def test_host_bytecode_in_our_directories_is_a_finding_but_the_interpreters_own_is_not(self):
        root = Path(_tf.mkdtemp(prefix='wispos-check-'))
        (root / 'usr/lib/python3.14/__pycache__').mkdir(parents=True)                 # the image's own Python: legitimate
        self.assertFalse([m for m in check_rootfs.contract_report(root) if 'bytecode' in m])
        (root / 'usr/bin/__pycache__').mkdir(parents=True)
        self.assertIn('MUST NOT CONTAIN usr/bin/__pycache__ (host Python bytecode)', check_rootfs.contract_report(root))

    def test_an_always_on_root_shell_is_a_finding_and_the_prune_list_removes_it(self):
        root = Path(_tf.mkdtemp(prefix='wispos-check-')); wants = root / 'usr/lib/systemd/system/sysinit.target.wants'; wants.mkdir(parents=True)
        (wants / 'debug-shell.service').symlink_to('../debug-shell.service')          # a dangling link still counts: it is the ENABLEMENT that is the problem
        self.assertIn('MUST NOT CONTAIN usr/lib/systemd/system/sysinit.target.wants/debug-shell.service', check_rootfs.contract_report(root))
        (wants / 'debug-shell.service').unlink()
        self.assertFalse([m for m in check_rootfs.contract_report(root) if 'debug-shell' in m])
        prune = [l.strip() for l in (HERE / 'rootfs-prune.txt').read_text().splitlines() if l.strip() and not l.startswith('#')]
        self.assertIn('usr/lib/systemd/system/sysinit.target.wants/debug-shell.service', prune)

    def test_baked_steam_libraries_resolve_each_other_but_nothing_else_may_use_that_directory(self):
        # libbrotlidec needs libbrotlicommon, which sits beside it in usr/lib/wispos/lib, the directory the Steam session puts on
        # LD_LIBRARY_PATH. The closure check must accept that, and only for libraries inside that directory.
        import shutil, os
        root = Path(_tf.mkdtemp(prefix='wispos-baked-', dir=os.environ.get('TMPDIR'))); src = root / 'src'; src.mkdir()
        (src / 'common.c').write_text('int common(void){return 1;}\n'); (src / 'dec.c').write_text('int common(void); int dec(void){return common();}\n')
        (src / 'user.c').write_text('int common(void); int user(void){return common();}\n')
        cc = ['aarch64-linux-gnu-gcc', '-shared', '-fPIC', '-Wl,--no-as-needed']
        for d in ('usr/lib/wispos/lib', 'usr/lib/elsewhere'): (root / d).mkdir(parents=True)
        subprocess.run(cc + ['-Wl,-soname,libcommon.so.1', '-o', str(root / 'usr/lib/wispos/lib/libcommon.so.1'), str(src / 'common.c')], check=True)
        subprocess.run(cc + [str(src / 'dec.c'), '-o', str(root / 'usr/lib/wispos/lib/libdec.so.1'), '-Wl,-soname,libdec.so.1', '-L' + str(root / 'usr/lib/wispos/lib'), '-l:libcommon.so.1'], check=True)
        subprocess.run(cc + [str(src / 'user.c'), '-o', str(root / 'usr/lib/elsewhere/libuser.so.1'), '-Wl,-soname,libuser.so.1', '-L' + str(root / 'usr/lib/wispos/lib'), '-l:libcommon.so.1'], check=True)
        for f in list((root / 'usr/lib/wispos/lib').glob('*.so.1')) + [root / 'usr/lib/elsewhere/libuser.so.1']:
            self.assertGreater(f.stat().st_size, 64)
        report = check_rootfs.elf_report(root)
        self.assertNotIn('libcommon.so.1', report.get('usr/lib/wispos/lib/libdec.so.1', []))      # a sibling in the baked directory resolves
        self.assertIn('libcommon.so.1', report.get('usr/lib/elsewhere/libuser.so.1', []))         # any other directory may not use it

    def test_real_checked_in_overlay_is_self_consistent(self):
        wants = HERE / 'rootfs/usr/lib/systemd/system/wispos.target.wants/wisp-steam.service'
        self.assertTrue(wants.exists())                                   # link resolves to the unit
        target = configparser.ConfigParser(strict=False, interpolation=None); target.optionxform = str
        target.read(HERE / 'rootfs/usr/lib/systemd/system/wispos.target')
        self.assertEqual(target['Install']['Alias'].split(), ['default.target', 'rocknix.target'])

    def test_steams_ui_scale_defaults_to_the_operators_choice_without_overriding_the_users(self):
        # Operator decision 2026-10-15: the SteamOS UI scale default is "150 %"; he chose the 9-notch position (his own 2026-10-09 hand pick, ScaleFactor 0.842373907566070557 = 1.45 x Steam's
        # auto value 0.5809 on this 720x480 panel; Steam's control shows no percentage). Verified on the handheld 2026-10-15 (notes "UI scale seed"): Steam stores a manual scale as a per-display
        # block under InstallConfigStore/UI/display named "<display>|||<mode>" holding ScaleFactor, and mirrors it into display/Current; a seeded block alone started Steam normally and was applied.
        import importlib.machinery, importlib.util, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-seed')
        spec = importlib.util.spec_from_loader('wisp_steam_seed2', importlib.machinery.SourceFileLoader('wisp_steam_seed2', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual(m.UI_SCALE, '0.842373907566070557'); self.assertEqual(m.UI_SCALE_DISPLAY, 'External: DSI-1 3"|||Fullscreen-720x480')
        block = '\t\t\t"External: DSI-1 3\\"|||Fullscreen-720x480"\n\t\t\t{\n\t\t\t\t"ScaleFactor"\t\t"0.842373907566070557"\n\t\t\t}\n'
        def vdf(ui=''):
            return '"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"Other"\t\t"1"\n\t\t\t}\n\t\t}\n\t}\n' + ui + '}\n'
        current = ('\t"UI"\n\t{\n\t\t"display"\n\t\t{\n\t\t\t"Current"\n\t\t\t{\n\t\t\t\t"MinScaleFactor"\t\t"0.29"\n\t\t\t\t"name"\t\t"External: DSI-1 3\\"|||Fullscreen-720x480"\n'
                   '\t\t\t\t"AutoScaleFactor"\t\t"0.5809"\n\t\t\t\t"ScaleFactor"\t\t"0.5809"\n\t\t\t}\n\t\t}\n\t}\n')
        # no UI section at all (a fresh config): the whole UI/display/<block> is added, braces balance, and a second run changes nothing
        text, status = m.seed_ui_scale(vdf())
        self.assertEqual(status, 'seeded'); self.assertIn(block, text); self.assertEqual(text.count('{'), text.count('}')); self.assertEqual(m.seed_ui_scale(text), (text, 'present'))
        self.assertIn('"Other"\t\t"1"', text)
        # Steam's own Current block (auto) is left exactly as it is and does not count as a choice: the block is added beside it
        text, status = m.seed_ui_scale(vdf(current))
        self.assertEqual(status, 'seeded'); self.assertIn(current.split('\t\t\t"Current"')[1].split('\n\t\t}')[0], text); self.assertIn(block, text)
        self.assertEqual(text.count('{'), text.count('}')); self.assertEqual(m.seed_ui_scale(text)[1], 'present')
        # a display block of the user's own (any name, any value) is never replaced or added to
        own = vdf('\t"UI"\n\t{\n\t\t"display"\n\t\t{\n\t\t\t"Internal: DSI-1 3\\""\n\t\t\t{\n\t\t\t\t"ScaleFactor"\t\t"1.1"\n\t\t\t}\n\t\t}\n\t}\n')
        self.assertEqual(m.seed_ui_scale(own), (own, 'present'))
        # a UI section without a display block gets one; junk is left alone
        text, status = m.seed_ui_scale(vdf('\t"UI"\n\t{\n\t\t"Other"\t\t"x"\n\t}\n'))
        self.assertEqual(status, 'seeded'); self.assertIn(block, text); self.assertEqual(text.count('{'), text.count('}'))
        junk = '"Something"\n{\n}\n'
        self.assertEqual(m.seed_ui_scale(junk), (junk, 'unrecognised'))
        # the command seeds both the tool and the scale on a first start, and leaves a user's scale alone afterwards
        d = Path(tempfile.mkdtemp(prefix='wispos-seed-ui-', dir=os.environ.get('TMPDIR'))); f = d / 'config' / 'config.vdf'
        self.assertEqual(m.main([str(f)]), 0); first = f.read_text()
        self.assertIn(block, first); self.assertIn('proton-stable', first); self.assertEqual(first.count('{'), first.count('}'))
        edited = first.replace('"0.842373907566070557"', '"0.9"'); f.write_text(edited)
        self.assertEqual(m.main([str(f)]), 0); self.assertEqual(f.read_text(), edited)

    def test_steam_defaults_to_the_arm64_proton_without_overriding_the_users_choice(self):
        # Worms Armageddon install, 2026-10-09: with no global default tool Steam queued the x86 Proton Experimental + FEX stack (about 2.2 GB) that cannot run here.
        import importlib.machinery, importlib.util, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-seed')
        spec = importlib.util.spec_from_loader('wisp_steam_seed', importlib.machinery.SourceFileLoader('wisp_steam_seed', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        T = '\t'
        def vdf(*mapping):
            body = ''.join(mapping)
            return ('"InstallConfigStore"\n{\n\t"Software"\n\t{\n\t\t"Valve"\n\t\t{\n\t\t\t"Steam"\n\t\t\t{\n\t\t\t\t"Other"\t\t"1"\n' + body + '\t\t\t}\n\t\t}\n\t}\n}\n')
        mapping63710 = '\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"63710"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton-stable"\n\t\t\t\t\t}\n\t\t\t\t}\n'
        # no file yet: a minimal one with the entry, which parses back to the right nesting
        text, status = m.seed('')
        self.assertEqual(status, 'seeded'); self.assertIn('"0"', text); self.assertIn('"name"\t\t"proton-stable"', text)
        self.assertEqual(m.seed(text)[1], 'present')                                      # idempotent
        # a mapping block with other apps gets the global entry added, and keeps the others and everything else
        text, status = m.seed(vdf(mapping63710))
        self.assertEqual(status, 'seeded'); self.assertIn('"63710"', text); self.assertIn('"Other"\t\t"1"', text)
        self.assertEqual(text.count('"CompatToolMapping"'), 1); self.assertEqual(m.seed(text)[1], 'present')
        self.assertEqual(text.count('{'), text.count('}'))
        # no mapping block at all: it is added inside Steam's block
        text, status = m.seed(vdf())
        self.assertEqual(status, 'seeded'); self.assertEqual(text.count('{'), text.count('}')); self.assertEqual(m.seed(text)[1], 'present')
        # the user's own default (any tool) is never replaced; an entry for an app called "0..." deeper in the file does not count
        own = vdf('\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"0"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton-experimental"\n\t\t\t\t\t}\n\t\t\t\t}\n')
        self.assertEqual(m.seed(own), (own, 'present'))
        nested = vdf('\t\t\t\t"CompatToolMapping"\n\t\t\t\t{\n\t\t\t\t\t"63710"\n\t\t\t\t\t{\n\t\t\t\t\t\t"0"\t\t"x"\n\t\t\t\t\t}\n\t\t\t\t}\n')
        self.assertEqual(m.seed(nested)[1], 'seeded')
        # a file that is not what we expect is left exactly alone
        junk = '"Something"\n{\n}\n'
        self.assertEqual(m.seed(junk), (junk, 'unrecognised'))
        # the command: writes atomically, keeps the mode, leaves the junk alone with exit 1
        d = Path(tempfile.mkdtemp(prefix='wispos-seed-', dir=os.environ.get('TMPDIR')))
        f = d / 'config' / 'config.vdf'
        self.assertEqual(m.main([str(f)]), 0); self.assertIn('proton-stable', f.read_text())
        f.write_text(junk); self.assertEqual(m.main([str(f)]), 1); self.assertEqual(f.read_text(), junk)
        # the custom tool (installed under compatibilitytools.d, which Steam never validates or updates) is preferred once it exists
        custom = 'Proton11-ARM64-A57-v80'
        text, status = m.seed('', custom); self.assertEqual(status, 'seeded'); self.assertIn('"name"\t\t"%s"' % custom, text)
        stable = m.seed(vdf(mapping63710), 'proton-stable')[0]
        up, status = m.seed(stable, custom)                                               # our own earlier seed (proton-stable, priority 75) is moved over
        self.assertEqual(status, 'upgraded'); self.assertIn('"name"\t\t"%s"' % custom, up); self.assertNotIn('"0"\n\t\t\t\t\t{\n\t\t\t\t\t\t"name"\t\t"proton-stable"', up)
        self.assertEqual(up.count('{'), up.count('}')); self.assertIn('"63710"', up); self.assertEqual(m.seed(up, custom)[1], 'present')   # idempotent
        usermade = own.replace('proton-experimental', 'proton-stable')                  # the user chose proton-stable themselves: no priority 75 -> never touched
        self.assertEqual(m.seed(usermade, custom), (usermade, 'present'))
        self.assertEqual(m.seed(own, custom), (own, 'present'))                           # any other choice stays too
        # 2026-10-14: in SteamOS mode (-steamos3) Steam records a per-game mapping at priority 85 to Valve's proton-stable, which beats a default of priority 75: every game then ran on Valve's stock
        # Proton, which spins in wineboot on this CPU. Our default must outrank 85 and stay below 250 (a person's own explicit choice), and earlier priority-75 seeds are upgraded in place.
        self.assertIn('"priority"\t\t"200"', text); self.assertGreater(int(m.SEED_PRIORITY), 85); self.assertLess(int(m.SEED_PRIORITY), 250)
        for legacy_name in ('proton-stable', custom):
            legacy = m.seed('', legacy_name)[0].replace('"priority"\t\t"200"', '"priority"\t\t"75"')
            fixed, status = m.seed(legacy, custom)
            self.assertEqual(status, 'upgraded', legacy_name); self.assertIn('"name"\t\t"%s"' % custom, fixed); self.assertIn('"priority"\t\t"200"', fixed); self.assertNotIn('"75"', fixed)
            self.assertEqual(m.seed(fixed, custom), (fixed, 'present'))                    # idempotent
        explicit = m.seed('', custom)[0].replace('"priority"\t\t"200"', '"priority"\t\t"250"')
        self.assertEqual(m.seed(explicit, custom), (explicit, 'present'))                  # the user's own explicit choice of the same tool is never touched
        other = m.seed('', 'proton-experimental')[0]
        self.assertEqual(m.seed(other, custom), (other, 'present'))                        # another tool at our priority is not ours
        root = Path(tempfile.mkdtemp(prefix='wispos-steamroot-', dir=os.environ.get('TMPDIR')))
        self.assertEqual(m.wanted_tool(str(root)), 'proton-stable')                      # not installed yet
        (root / 'compatibilitytools.d' / custom).mkdir(parents=True); (root / 'compatibilitytools.d' / custom / 'compatibilitytool.vdf').write_text('x')
        self.assertEqual(m.wanted_tool(str(root)), custom)
        g = d / 'config2' / 'config.vdf'; g.parent.mkdir(); g.write_text(stable)
        self.assertEqual(m.main([str(g), str(root)]), 0); self.assertIn(custom, g.read_text())
        # the session runs it before every Steam start, and it ships in the image's checked file list
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn('wisp-steam-seed', session); self.assertLess(session.index('wisp-steam-seed'), session.index('steamrtarm64/steam" -gamepadui'))

    def test_games_launched_by_steam_get_the_fallback_ahead_of_the_overlay_in_every_process(self):
        # 2026-10-09: Steam-launched games never started on the handheld. Steam preloads "<fallback>:<overlay>:~/.steam/bin32/gameoverlayrenderer.so:<overlay>" into the
        # launch shell and the pressure-vessel container; libraries initialise in reverse order, so the overlay's constructors (LSE atomics, SIGILL on the Cortex-A53)
        # ran before the fallback's handler existed ("sh" core dump, exit -1; in the container "killed by signal 4", silent exit 0). Verified on the device: with a COPY of
        # the fallback at the bin32 path the game starts. A link would not do: a second path to the same file is loaded once, at its first position.
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn('"$HOME/.steam/bin32/gameoverlayrenderer.so"', session)
        self.assertIn('cp /usr/lib/wispos/libwispos-ldadd.so "$HOME/.steam/bin32/gameoverlayrenderer.so"', session)
        self.assertNotIn('ln -sf /usr/lib/wispos/libwispos-ldadd.so', session)
        self.assertNotRegex(session, r'ln -s[fn]* .*bin32/gameoverlayrenderer')
        self.assertLess(session.index('bin32/gameoverlayrenderer.so'), session.index('steamrtarm64/steam" -gamepadui'))      # in place before Steam starts

    def test_native_linux_games_get_the_guest_root_and_graphics_provider_only_when_it_exists(self):
        # 2026-10-10: Valve's FEX tool (native x86 Linux games) took its root from STEAM_COMPAT_GRAPHICS_PROVIDER; unset, FEX had no x86 filesystem and every launch ended
        # silently with no window (Half-Life, Blue Shift, Day of Defeat all ran once it was set).
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn('guest_root=$base/guestos/ubuntu-24.04', session)
        self.assertIn('if [ -f "$guest_root/graphics_provider.json" ]; then', session)
        self.assertIn('export STEAM_COMPAT_GRAPHICS_PROVIDER="$guest_root/graphics_provider.json"', session)
        self.assertIn('ThunksDB_GL:1', session)
        self.assertLess(session.index('STEAM_COMPAT_GRAPHICS_PROVIDER'), session.index('steamrtarm64/steam" -gamepadui'))      # exported before Steam starts
        self.assertNotIn('STEAM_COMPAT_GRAPHICS_PROVIDER=/usr/share/guestos', session)

    def test_the_install_script_evaluation_is_not_repeated_once_it_has_succeeded(self):
        # 2026-10-09: Steam runs `proton run iscriptevaluator.exe legacycompat\\evaluatorscript_<id>.vdf` before every launch; under FEX on the A53 one pass takes ~100 s.
        # The patch skips a repeat of the SAME script (same hash + Proton version) after an exit 0, and never after a failure or a changed script.
        import importlib.machinery, importlib.util, os, subprocess, sys, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-proton-patch')
        spec = importlib.util.spec_from_loader('wisp_proton_patch', importlib.machinery.SourceFileLoader('wisp_proton_patch', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        skeleton = (
            'import os, sys\n'
            'class Proton:\n'
            '    def __init__(self, d): pass\n'
            '    def cleanup_legacy_dist(self): pass\n'
            '    def do_steampipe_fixups(self): pass\n'
            'class Session:\n'
            '    def run(self):\n'
            '        open(os.environ["RAN_LOG"], "a").write("ran\\n"); open(os.environ["ENV_LOG"], "a").write(os.environ.get("PAN_MESA_DEBUG", "<unset>") + "\\n"); return int(os.environ.get("RC", "0"))\n'
            'def log(m): pass\n'
            'if __name__ == "__main__":\n'
            '    if "STEAM_COMPAT_DATA_PATH" not in os.environ:\n'
            '        log("No compat data path?")\n'
            '        sys.exit(1)\n'
            '\n'
            '    g_proton = Proton(os.path.dirname(sys.argv[0]))\n'
            '    g_session = Session()\n'
            '    rc = 0\n'
            '    if sys.argv[1] == "run":\n'
            '        rc = g_session.run()\n'
            '    elif sys.argv[1] == "waitforexitandrun":\n'
            '        rc = g_session.run()\n'
            '    sys.exit(rc)\n')
        text, status = m.patched(skeleton)
        self.assertEqual(status, 'patched'); compile(text, 'proton', 'exec')
        self.assertEqual(m.patched(text), (text, 'present'))                                  # idempotent
        self.assertEqual(m.patched('print(1)\n'), ('print(1)\n', 'unrecognised'))             # an unexpected file is left alone
        d = Path(tempfile.mkdtemp(prefix='wispos-isc-', dir=os.environ.get('TMPDIR')))
        (d / 'tool').mkdir(); (d / 'steam' / 'legacycompat').mkdir(parents=True); (d / 'data').mkdir()
        (d / 'tool' / 'proton').write_text(text); (d / 'tool' / 'version').write_text('11.0-test\n')
        script = d / 'steam' / 'legacycompat' / 'evaluatorscript_1.vdf'; script.write_text('"evaluatorscript" { "appid" "1" }\n')
        env = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(d / 'data'), STEAM_COMPAT_CLIENT_INSTALL_PATH=str(d / 'steam'), RAN_LOG=str(d / 'ran.log'), ENV_LOG=str(d / 'env.log'))
        env.pop('PAN_MESA_DEBUG', None)
        def go(*args, rc='0'):
            r = subprocess.run([sys.executable, '-I', str(d / 'tool' / 'proton')] + list(args), env=dict(env, RC=rc), capture_output=True, text=True)
            return r.returncode, (d / 'ran.log').read_text().count('ran') if (d / 'ran.log').exists() else 0, r.stderr
        ev = ['run', '/x/legacycompat/iscriptevaluator.exe', 'legacycompat\\evaluatorscript_1.vdf']
        self.assertEqual(go(*ev, rc='3')[:2], (3, 1))                                         # a failed pass runs and leaves no marker
        self.assertEqual(go(*ev)[:2], (0, 2))                                                 # so it runs again, now succeeds and leaves one
        rc, ran, err = go(*ev)
        self.assertEqual((rc, ran), (0, 2)); self.assertIn('skipped', err)                    # an identical script after success: skipped, nothing ran
        script.write_text('"evaluatorscript" { "appid" "1" "changed" "1" }\n')
        self.assertEqual(go(*ev)[:2], (0, 3))                                                 # a changed script runs again
        (d / 'tool' / 'version').write_text('11.0-newer\n')
        self.assertEqual(go(*ev)[:2], (0, 4))                                                 # a new Proton version runs again
        self.assertEqual(go('run', '/x/game.exe')[:2], (0, 5))                                # an ordinary game is never skipped
        self.assertEqual(go('run', '/x/game.exe')[:2], (0, 6))
        self.assertEqual(go('waitforexitandrun', '/x/game.exe')[:2], (0, 7))
        # the panfrost workaround (Mesa 26.2.2 overruns a GPU buffer in a tiled texture upload; measured 2026-10-10): every game process gets PAN_MESA_DEBUG=linear
        envlog = lambda: (d / 'env.log').read_text().split()
        self.assertEqual(set(envlog()), {'linear'})                                           # every run above (run and waitforexitandrun, evaluator and game) saw it
        (d / 'env.log').unlink()
        def go_env(value, verb='waitforexitandrun'):
            (d / 'env.log').unlink(missing_ok=True)
            r = subprocess.run([sys.executable, '-I', str(d / 'tool' / 'proton'), verb, '/x/game.exe'], env=dict(env, PAN_MESA_DEBUG=value), capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr); return envlog()
        self.assertEqual(go_env('perf'), ['perf,linear'])                                     # added to flags the user set, not replacing them
        self.assertEqual(go_env('trace,linear,sync'), ['trace,linear,sync'])                  # never duplicated
        self.assertEqual(go_env('', 'run'), ['linear'])                                       # an empty value does not leave a leading comma
        old_text = text.replace(m.ENV, '')                                                    # a launcher patched by the version before this piece existed
        self.assertNotIn(m.ENV_MARK, old_text); self.assertIn(m.MARK, old_text)
        upgraded, status = m.patched(old_text)
        self.assertEqual(status, 'patched'); self.assertIn(m.ENV_MARK, upgraded); self.assertEqual(upgraded.count(m.MARK), text.count(m.MARK))   # gets only the missing piece
        self.assertEqual(m.patched(upgraded), (upgraded, 'present')); compile(upgraded, 'proton', 'exec')
        root = d / 'steamroot'
        for sub in ('compatibilitytools.d/Proton11-ARM64-A57-v80', 'steamapps/common/Proton 11.0 (ARM64)', 'steamapps/common/Other'):
            (root / sub).mkdir(parents=True); (root / sub / 'proton').write_text('x')
        self.assertEqual([Path(f).parent.name for f in m.default_files(str(root))], ['Proton11-ARM64-A57-v80', 'Proton 11.0 (ARM64)'])
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn('wisp-proton-patch', session); self.assertLess(session.index('wisp-proton-patch'), session.index('steamrtarm64/steam" -gamepadui'))

    def test_the_launcher_puts_the_panel_at_full_resolution_before_the_game_starts_through_sways_socket(self):
        # 2026-10-12, handheld: the default display is half resolution (output scale 2), a game started at that scale read a 360x240 mode list and got a 360x228 window; every earlier
        # working run had a script switch the scale BEFORE the game. 2026-10-13: the first version called /usr/bin/wisp-display from the patched `proton` launcher, which runs INSIDE
        # Steam's pressure-vessel container where neither that tool nor swaymsg exists: it never ran, silently. The launcher now sends the command to sway's IPC socket itself, at the
        # start of `waitforexitandrun` (the game), never for `run` (the install-script evaluation and other helpers). Nothing restores the default here: wisp-game-display does.
        # The program is run for real, against a fake sway socket, so the ORDER and the wire format are observed.
        import importlib.machinery, importlib.util, os, socket, struct, sys, tempfile, threading
        path = str(HERE / 'rootfs/usr/bin/wisp-proton-patch')
        spec = importlib.util.spec_from_loader('wisp_proton_patch5', importlib.machinery.SourceFileLoader('wisp_proton_patch5', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        skeleton = (
            'import os, sys\n'
            'class Proton:\n'
            '    def __init__(self, d): pass\n'
            '    def cleanup_legacy_dist(self): pass\n'
            '    def do_steampipe_fixups(self): pass\n'
            'class Session:\n'
            '    def run(self):\n'
            '        open(os.environ["EVENTS"], "a").write("game\\n"); return int(os.environ.get("RC", "0"))\n'
            'def log(m): pass\n'
            'if __name__ == "__main__":\n'
            '    if "STEAM_COMPAT_DATA_PATH" not in os.environ:\n'
            '        log("No compat data path?")\n'
            '        sys.exit(1)\n'
            '\n'
            '    g_proton = Proton(os.path.dirname(sys.argv[0]))\n'
            '    g_session = Session()\n'
            '    rc = 0\n'
            '    if sys.argv[1] == "run":\n'
            '        rc = g_session.run()\n'
            '    elif sys.argv[1] == "waitforexitandrun":\n'
            '        #wait for wineserver to shut down\n'
            '        #then run\n'
            '        rc = g_session.run()\n'
            '    sys.exit(rc)\n')
        text, status = m.patched(skeleton)
        self.assertEqual(status, 'patched'); compile(text, 'proton', 'exec')
        self.assertEqual(m.patched(text), (text, 'present'))
        d = Path(tempfile.mkdtemp(prefix='wispos-gd-', dir=os.environ.get('TMPDIR')))
        (d / 'tool').mkdir(); (d / 'data').mkdir(); (d / 'tool' / 'proton').write_text(text); (d / 'tool' / 'version').write_text('11.0-test\n')
        events = d / 'events'
        # a fake sway: accepts connections on a unix socket, speaks the i3/sway IPC framing (6-byte magic, native-endian length and type, payload), logs what it was asked BEFORE it replies
        sock_path = str(d / 'sway-ipc.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); server.bind(sock_path); server.listen(5); server.settimeout(0.2)
        state = {'stop': False, 'mute': False}
        def serve():
            while not state['stop']:
                try: conn, _ = server.accept()
                except (socket.timeout, OSError): continue
                with conn:
                    head = b''
                    while len(head) < 14:
                        chunk = conn.recv(14 - len(head))
                        if not chunk: break
                        head += chunk
                    if len(head) < 14: continue
                    magic, (length, kind) = head[:6], struct.unpack('=II', head[6:14])
                    payload = b''
                    while len(payload) < length: payload += conn.recv(length - len(payload))
                    with open(events, 'a') as f: f.write('sway:%s:%d:%s\n' % (magic.decode(), kind, payload.decode().replace(' ', '_')))
                    if not state['mute']:
                        reply = b'[{"success":true}]'; conn.sendall(b'i3-ipc' + struct.pack('=II', len(reply), kind) + reply)
                    else: threading.Event().wait(2)
        thread = threading.Thread(target=serve, daemon=True); thread.start()
        def stop():
            state['stop'] = True; thread.join(2); server.close()
        self.addCleanup(stop)
        env = dict(os.environ, STEAM_COMPAT_DATA_PATH=str(d / 'data'), EVENTS=str(events), WISP_SWAYSOCK=sock_path)
        def go(*args, rc='0', extra=None):
            events.unlink(missing_ok=True)
            r = subprocess.run([sys.executable, '-I', str(d / 'tool' / 'proton')] + list(args), env=dict(env, RC=rc, **(extra or {})), capture_output=True, text=True)
            return r.returncode, events.read_text().split() if events.exists() else [], r.stderr
        rc, seen, err = go('waitforexitandrun', '/x/game.exe')
        self.assertEqual((rc, seen), (0, ['sway:i3-ipc:0:output_DSI-1_scale_1', 'game']), err)      # RUN_COMMAND (type 0) answered BEFORE the game runs
        self.assertEqual(go('waitforexitandrun', '/x/game.exe', rc='3')[:2], (3, ['sway:i3-ipc:0:output_DSI-1_scale_1', 'game']))   # the game's exit status is kept
        self.assertEqual(go('run', '/x/iscriptevaluator.exe')[:2], (0, ['game']))                  # the evaluation and other helper runs never touch the display
        self.assertEqual(go('run', '/x/game.exe')[:2], (0, ['game']))
        self.assertEqual(go('waitforexitandrun', '/x/game.exe', extra={'WISP_SWAYSOCK': str(d / 'missing.sock')})[:2], (0, ['game']))    # no socket: the game still starts
        stale = d / 'stale.sock'; dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM); dead.bind(str(stale)); dead.close()
        self.assertEqual(go('waitforexitandrun', '/x/game.exe', extra={'WISP_SWAYSOCK': str(stale)})[:2], (0, ['game']))                # a socket nobody listens on: the same
        state['mute'] = True                                                                       # a sway that takes the command and never answers: bounded by the timeout, the game starts
        rc, seen, _ = go('waitforexitandrun', '/x/game.exe', extra={'WISP_SWAYSOCK_TIMEOUT': '0.5'})
        self.assertEqual((rc, seen), (0, ['sway:i3-ipc:0:output_DSI-1_scale_1', 'game']))
        # a launcher patched by the version before this piece existed gets only the new piece at the next start
        old = text.replace(m.DISPLAY_START, '')
        self.assertNotIn(m.DISPLAY_MARK, old); self.assertIn(m.MARK, old); self.assertIn(m.ENV_MARK, old)
        self.assertEqual(m.patched(old), (text, 'patched'))
        # a launcher without the game's start verb is not understood and is left alone (the caller writes nothing for 'unrecognised')
        self.assertEqual(m.patched(skeleton.replace('waitforexitandrun', 'other'))[1], 'unrecognised')
        # the session shares exactly the one socket with the container, or the piece above has nothing to talk to
        self.assertIn('"/run/0-runtime-dir/sway-ipc.0.sock"', text)                              # the default socket is the one the session shares (below)
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertRegex(session, r'export PRESSURE_VESSEL_FILESYSTEMS_RW=[^\n]*/run/0-runtime-dir/sway-ipc\.0\.sock')

    def test_the_standard_spelling_of_the_english_locale_exists_so_pressure_vessel_does_not_regenerate_it(self):
        # 2026-10-09: ROCKNIX installs the locale as usr/config/locale/en_US.UTF-8; userconfig-setup rsyncs it (-a keeps links) into the tmpfs /storage/.config/locale that usr/share/locale
        # and usr/lib/locale point to. glibc and pressure-vessel look for the normalised
        # directory name en_US.utf8; without it pv-locale-gen reported "Missing locale" and compiled the locale (28-35 s on the A53) in the container at every
        # container start (two per Steam game launch). With a link under the standard name it needs nothing (0 s, measured on the device).
        import os
        link = HERE / 'rootfs/usr/config/locale/en_US.utf8'
        self.assertTrue(link.is_symlink()); self.assertEqual(os.readlink(link), 'en_US.UTF-8')

    def test_a_black_screen_stall_restarts_the_steam_session_but_a_slow_start_or_a_running_game_does_not(self):
        # 2026-10-09: Steam's window was mapped in Xwayland but sway never got a view, the renderer spun at 90-100%, and the screen stayed black until a manual restart
        # (3 of 4 starts that evening). The watchdog restarts the service (never a reboot), but only for a confirmed stall and within a restart budget.
        import importlib.machinery, importlib.util, os, stat, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-watch')
        spec = importlib.util.spec_from_loader('wisp_steam_watch', importlib.machinery.SourceFileLoader('wisp_steam_watch', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        up, helpers = m.STALL_AFTER + 1, 7
        self.assertEqual(m.decide(False, up, helpers, False, 3, 0), (None, 0))                # the session is not running: nothing to restart
        self.assertEqual(m.decide(True, m.STALL_AFTER - 1, helpers, False, 3, 0), (None, 0))  # a normal start still has minutes to show its window
        self.assertEqual(m.decide(True, up, 2, False, 3, 0), (None, 0))                       # the web helpers are not even up: not this failure
        self.assertEqual(m.decide(True, up, helpers, True, 3, 0), (None, 0))                  # the window exists (also while a game owns the screen): healthy
        misses = 0
        for i in range(m.CONFIRM - 1):                                                        # the stall must be seen several checks in a row
            action, misses = m.decide(True, up, helpers, False, misses, 0); self.assertEqual((action, misses), (None, i + 1))
        self.assertEqual(m.decide(True, up, helpers, False, misses, 0), ('restart', 0))
        self.assertEqual(m.decide(True, up, helpers, False, m.CONFIRM, m.MAX_RESTARTS)[0], None)   # the restart budget is spent: stop, never loop
        # the sway query, with a fake swaymsg on the PATH: a window of class steam anywhere in the tree counts; a failing sway never counts as a stall
        d = Path(tempfile.mkdtemp(prefix='wispos-watch-', dir=os.environ.get('TMPDIR')))
        def fake(text, rc=0):
            f = d / 'swaymsg'; f.write_text('#!/bin/sh\ncat <<\'EOF\'\n%s\nEOF\nexit %d\n' % (text, rc)); f.chmod(0o755)
        old_path = os.environ['PATH']; os.environ['PATH'] = str(d) + os.pathsep + old_path
        try:
            tree = lambda cls: '{"nodes":[{"nodes":[{"nodes":[],"floating_nodes":[],"window_properties":{"class":"%s"}}]}]}' % cls
            fake(tree('steam')); self.assertTrue(m.has_steam_window())
            fake(tree('steam_app_217200')); self.assertFalse(m.has_steam_window())             # only a game window: Steam's own window is missing
            fake('{"nodes":[]}'); self.assertFalse(m.has_steam_window())
            fake('', rc=1); self.assertTrue(m.has_steam_window())
        finally: os.environ['PATH'] = old_path
        unit = (HERE / 'rootfs/usr/lib/systemd/system/wisp-steam-watch.service').read_text()
        self.assertIn('ExecStart=/usr/bin/wisp-steam-watch', unit); self.assertIn('Restart=always', unit)
        self.assertEqual(os.readlink(HERE / 'rootfs/usr/lib/systemd/system/wispos.target.wants/wisp-steam-watch.service'), '../wisp-steam-watch.service')

    def test_a_steam_client_left_stopped_by_its_crash_handler_is_restarted_even_with_no_web_helpers_alive(self):
        # 2026-10-13, handheld: after two Steam watchdog asserts (main loop stalled > 15 s, UI thread hung) the client process sat in state T (all 17 threads) for 14 minutes, its web
        # helper a zombie, the screen black. The window rule restarts only with 3 live helpers, so nothing recovered it. This rule stands alone: no uptime, no helpers, no sway.
        import importlib.machinery, importlib.util, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-watch')
        spec = importlib.util.spec_from_loader('wisp_steam_watch4', importlib.machinery.SourceFileLoader('wisp_steam_watch4', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        self.assertEqual(m.decide_stopped(False, True, 5, 0), (None, 0))                       # the session is not running: nothing to restart
        self.assertEqual(m.decide_stopped(True, False, 5, 0), (None, 0))                       # running: the count starts again
        action, n = m.decide_stopped(True, True, 0, 0)
        self.assertEqual((action, n), (None, 1))                                               # one sighting is not enough (a debugger, a slow /proc read)
        self.assertEqual(m.decide_stopped(True, True, m.STOPPED_CONFIRM - 1, 0), ('restart-stopped', 0))
        self.assertEqual(m.decide_stopped(True, True, m.STOPPED_CONFIRM, m.MAX_RESTARTS)[0], None)   # the restart budget is spent: stop, never loop
        # through step(): a session 20 s old, ZERO helpers, sway never asked, and a state saved by the older code (no 'stopped' key)
        def ask(): self.fail('the stopped rule must not need sway')
        state = {'misses': 0, 'seen_at': None}
        actions = []
        for t in range(m.STOPPED_CONFIRM):
            action, state = m.step(state, True, 20, 0, ask, 0, float(t), stopped=True); actions.append(action)
        self.assertEqual(actions, [None] * (m.STOPPED_CONFIRM - 1) + ['restart-stopped'])
        action, state = m.step(state, True, 20, 0, ask, 0, 99.0, stopped=False)                # running again: no action, the count is zero
        self.assertEqual((action, state['stopped']), (None, 0))
        self.assertEqual(m.step({'misses': 0, 'seen_at': None, 'stopped': 1}, True, 20, 0, ask, 0, 1.0, stopped=False)[1]['stopped'], 0)   # a run of sightings must be unbroken
        # the black-screen window rule is unchanged: a client that is not stopped still goes through it
        action, _ = m.step({'misses': m.CONFIRM - 1, 'seen_at': None}, True, m.STALL_AFTER + 1, 7, lambda: False, 0, 1.0, stopped=False)
        self.assertEqual(action, 'restart')
        # client_stopped reads a /proc-shaped tree: T counts; S, R, Z, a tracing stop 't' and a missing process do not; a name with spaces and parentheses does not confuse it
        root = Path(tempfile.mkdtemp(prefix='wispos-proc-', dir=os.environ.get('TMPDIR')))
        for pid, comm, state_ch in ((10, 'steam', 'T'), (11, 'steam', 'S'), (12, 'steam', 'R'), (13, 'steam', 'Z'), (14, 'steam', 't'), (15, 'we) T (ird', 'S'), (16, 'we) S (ird', 'T')):
            (root / str(pid)).mkdir(); (root / str(pid) / 'stat').write_text('%d (%s) %s 1 0 0\n' % (pid, comm, state_ch))
        self.assertTrue(m.client_stopped(str(root), [10]))
        for pid in (11, 12, 13, 14, 15, 999): self.assertFalse(m.client_stopped(str(root), [pid]), pid)
        self.assertTrue(m.client_stopped(str(root), [11, 999, 16]))                            # any one of several; the odd name is parsed after its LAST ')'
        self.assertFalse(m.client_stopped(str(root), []))
        # the newest assert dumps are named in the log before a restart
        dumps = Path(tempfile.mkdtemp(prefix='wispos-dumps-', dir=os.environ.get('TMPDIR')))
        self.assertEqual(m.recent_dumps(str(dumps)), [])
        for n in ('assert_20261013003920_22.dmp', 'assert_20261013004220_3.dmp', 'assert_20261013005329_5.dmp', 'assert_20261013005348_5.dmp', 'root_log.txt', 'assert_notes.txt'): (dumps / n).write_text('x')   # a non-dump named like one is not listed
        self.assertEqual(m.recent_dumps(str(dumps)), ['assert_20261013004220_3.dmp', 'assert_20261013005329_5.dmp', 'assert_20261013005348_5.dmp'])
        self.assertEqual(m.recent_dumps(str(dumps / 'nothing-here')), [])

    def test_the_watchdog_asks_sway_only_when_the_answer_matters_because_sway_keeps_every_reply(self):
        # Measured 2026-10-09 on the handheld: sway 1.11 retains ~8x the size of every IPC reply (get_tree of a 5 KB tree: +42 kB per call, linear over 800 calls). The
        # watchdog used to ask every 15 s from the moment Steam started, which is 5760 questions a day; sway's heap was 280 MB (all cold, in zram) after 19 hours.
        import importlib.machinery, importlib.util
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-watch')
        spec = importlib.util.spec_from_loader('wisp_steam_watch2', importlib.machinery.SourceFileLoader('wisp_steam_watch2', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        def hour(window_ok_until=10 ** 9, session_age=0):
            """Run the check loop for an hour of a session that is `session_age` seconds old; the window is in sway until `window_ok_until` seconds into the hour."""
            asked, actions, state = [], [], {'misses': 0, 'seen_at': None}
            for t in range(0, 3600, m.CHECK_EVERY):
                action, state = m.step(state, True, session_age + t, 7, lambda: (asked.append(t), t < window_ok_until)[1], 0, float(t))
                if action: actions.append((t, action))
            return asked, actions
        asked, actions = hour(session_age=0)                                           # a healthy session from its very first second
        self.assertEqual(actions, []); self.assertTrue(all(t >= m.STALL_AFTER for t in asked), 'no question before the stall rule can fire')
        self.assertLessEqual(len(asked), 6, asked)                                    # a handful of questions an hour (absolute: it was 240), not derived from the constant
        asked, actions = hour(window_ok_until=0, session_age=0)                        # the black-screen stall: never a window
        self.assertEqual(actions[0][1], 'restart'); self.assertGreaterEqual(actions[0][0], m.STALL_AFTER)
        self.assertLessEqual(actions[0][0], m.STALL_AFTER + m.CONFIRM * m.CHECK_EVERY, 'a stall is still confirmed within CONFIRM checks of the stall rule')
        asked, actions = hour(window_ok_until=1000, session_age=m.STALL_AFTER)         # the window disappears mid-session: found within one healthy interval + the confirmation
        self.assertEqual(actions[0][1], 'restart'); self.assertLessEqual(actions[0][0], 1000 + m.HEALTHY_EVERY + m.CONFIRM * m.CHECK_EVERY + m.CHECK_EVERY)
        self.assertEqual(m.step({'misses': 0, 'seen_at': 5.0}, False, 0, 0, lambda: self.fail('asked sway with no session'), 0, 99.0)[0], None)   # no session: no question
        _, state = m.step({'misses': 0, 'seen_at': 5.0}, True, 10, 7, lambda: self.fail('asked sway while the session is young'), 0, 99.0)
        self.assertIsNone(state['seen_at'], 'a restarted session has not been seen yet')

    def test_a_new_crash_dump_makes_the_watchdog_check_the_window_at_once_and_a_missing_window_is_then_confirmed_within_a_minute(self):
        # 2026-10-14: a web helper crashed (crash_*.dmp), Steam's window left sway's tree and nothing brought it back; the healthy-interval check would have taken up to 15 minutes to notice.
        import importlib.machinery, importlib.util, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-watch')
        spec = importlib.util.spec_from_loader('wisp_steam_watch6', importlib.machinery.SourceFileLoader('wisp_steam_watch6', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        up = m.STALL_AFTER + 600; healthy = {'misses': 0, 'seen_at': 1000.0}                       # the window was seen a minute ago: no routine question is due
        asked = []
        action, state = m.step(healthy, True, up, 8, lambda: (asked.append(1), True)[1], 0, 1060.0)
        self.assertEqual((action, asked), (None, []), 'without a crash the healthy window is not asked about')
        action, state = m.step(healthy, True, up, 8, lambda: (asked.append(1), True)[1], 0, 1060.0, poke=True)
        self.assertEqual((action, asked), (None, [1]), 'a crash dump makes the check happen now; the window is there: nothing happens')
        state, rounds, now = dict(healthy), 0, 1060.0
        while True:                                                                              # window gone after the crash
            rounds += 1; now += m.CHECK_EVERY
            action, state = m.step(state, True, up, 8, lambda: False, 0, now, poke=(rounds == 1))
            if action or rounds > 20: break
        self.assertEqual(action, 'restart'); self.assertLessEqual(rounds * m.CHECK_EVERY, 90, 'confirmed within about a minute, not a healthy interval')
        self.assertEqual(m.step(healthy, True, m.STALL_AFTER - 1, 8, lambda: self.fail('asked a young session'), 0, 1060.0, poke=True)[0], None)
        self.assertEqual(m.step(healthy, True, up, 2, lambda: self.fail('asked with too few helpers'), 0, 1060.0, poke=True)[0], None)
        d = Path(tempfile.mkdtemp(prefix='wispos-dumps-', dir=os.environ.get('TMPDIR'))); self.addCleanup(shutil.rmtree, d, True)
        self.assertIsNone(m.newest_crash(str(d)))
        for n in ('assert_20261014001833_5.dmp', 'crash_20261013223306_48.dmp', 'crash_20261014030000_48.dmp', 'root_log.txt'): (d / n).write_text('x')
        self.assertEqual(m.newest_crash(str(d)), 'crash_20261014030000_48.dmp', 'only crash dumps count, the newest wins; assert dumps do not')
        self.assertIsNone(m.newest_crash(str(d / 'missing')))

    def test_the_watchdog_leaves_evidence_in_the_log_before_it_restarts_steam(self):
        # A stalled UI used to be restarted without a trace of why. Now the busiest web-helper threads (state, CPU, kernel wait, futex address and value) and the GPU
        # page-fault count are logged first. thread_table reads a /proc-shaped tree, so it is tested on a fake one.
        import importlib.machinery, importlib.util, os, tempfile
        path = str(HERE / 'rootfs/usr/bin/wisp-steam-watch')
        spec = importlib.util.spec_from_loader('wisp_steam_watch3', importlib.machinery.SourceFileLoader('wisp_steam_watch3', path))
        m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
        root = Path(tempfile.mkdtemp(prefix='wispos-proc-', dir=os.environ.get('TMPDIR')))
        def thread(pid, tid, comm, state, utime, stime, wchan, syscall):
            d = root / str(pid) / 'task' / str(tid); d.mkdir(parents=True)
            (d / 'comm').write_text(comm + '\n')
            # /proc/PID/task/TID/stat: pid (comm) state ppid ... utime is field 14 and stime 15 counting the pid as 1, i.e. 12 fields after the state
            (d / 'stat').write_text('%d (%s) %s %s\n' % (tid, comm, state, ' '.join(['0'] * 10 + [str(utime), str(stime)] + ['0'] * 30)))
            (d / 'wchan').write_text(wchan); (d / 'syscall').write_text(syscall + '\n')
        thread(100, 100, 'steamwebhelper', 'S', 5, 3, '__futex_wait', '98 0xffff6c900438 0x9 0x2 0x0 0x0 0xffffffff 0x1 0x2')
        thread(100, 101, 'CompositorTileW', 'R', 9000, 1000, '0', 'running')
        thread(101, 201, 'steamwebhelper', 'S', 1, 1, 'do_sys_poll', '7 0x1 0x2 0x3 0x4 0x5 0x6 0x7 0x8')
        lines = m.thread_table(str(root), [100, 101, 999], limit=2)                                 # 999 does not exist: skipped, not an error
        self.assertEqual(len(lines), 2)
        self.assertIn('CompositorTileW', lines[0]); self.assertIn('cpu=10000', lines[0]); self.assertIn('syscall=running', lines[0])     # busiest first
        self.assertIn('steamwebhelper', lines[1]); self.assertIn('wchan=__futex_wait', lines[1]); self.assertIn('syscall=98 0xffff6c900438 0x9 0x2', lines[1])   # a futex wait shows its address and value
        self.assertEqual(m.thread_table(str(root / 'nope'), [100]), [])                             # no /proc tree: nothing, no exception
        # evidence() wires pidof and dmesg in (fake programs on the PATH)
        d = root / 'bin'; d.mkdir()
        for name, out in (('pidof', '100 101'), ('dmesg', '[1.0] panfrost 1800000.gpu: Unhandled Page fault in AS0\n[2.0] other\n[3.0] panfrost 1800000.gpu: Unhandled Page fault in AS1')):
            f = d / name; f.write_text("#!/bin/sh\ncat <<'EOF'\n%s\nEOF\n" % out); f.chmod(0o755)
        old_path = os.environ['PATH']; os.environ['PATH'] = str(d) + os.pathsep + old_path
        try: ev = m.evidence(str(root))
        finally: os.environ['PATH'] = old_path
        self.assertEqual(ev[0], 'GPU page faults this boot: 2'); self.assertGreaterEqual(len(ev), 3)
        text = (HERE / 'rootfs/usr/bin/wisp-steam-watch').read_text()
        self.assertLess(text.index("for line in evidence()"), text.index("run('systemctl', 'restart'"))          # logged BEFORE the restart is issued

    def test_pressure_vessel_does_not_compile_the_english_locale_at_every_container_start(self):
        # 2026-10-09 on the handheld: launch to game window 195 s with the per-container locale compile, 129 s without (the compile ran in both phases of a launch).
        session = (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text()
        self.assertIn('export PRESSURE_VESSEL_GENERATE_LOCALES=0', session)
        self.assertLess(session.index('PRESSURE_VESSEL_GENERATE_LOCALES=0'), session.index('steamrtarm64/steam" -gamepadui'))
        self.assertNotIn('PRESSURE_VESSEL_COPY_RUNTIME', session)                         # measured: no effect (133 s vs 129 s); not shipped


class SplashWiringTests(unittest.TestCase):
    """The boot splash's hooks in the image (docs/WISPOS-SPLASH-DESIGN.md): the stage helper, the units that call it, the wrapper and the boot arguments."""
    SAY = HERE / 'rootfs/usr/bin/wisp-splash-say'

    def run_say(self, fifo, *args, timeout=5):
        import os
        return subprocess.run(['sh', str(self.SAY), *args], capture_output=True, text=True, timeout=timeout, env={**os.environ, 'WISP_SPLASH_FIFO': str(fifo)})

    def test_say_writes_the_splash_protocol_lines(self):
        import os, tempfile
        d = Path(tempfile.mkdtemp(prefix='wispos-say-', dir=os.environ.get('TMPDIR'))); fifo = d / 'f'; os.mkfifo(fifo)
        rd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)                                  # the splash's own reader
        try:
            for args, line in ((('Starting the display',), b'status Starting the display\n'), (('--ready',), b'ready\n'), (('--fail', 'Display failed'), b'fail Display failed\n')):
                self.assertEqual(self.run_say(fifo, *args).returncode, 0)
                self.assertEqual(os.read(rd, 4096), line)
        finally:
            os.close(rd)

    def test_say_reaches_both_screens_ready_only_the_framebuffer_and_done_only_the_wayland_one(self):
        import os, tempfile
        d = Path(tempfile.mkdtemp(prefix='wispos-say-', dir=os.environ.get('TMPDIR'))); fb, wl = d / 'fb', d / 'wl'; os.mkfifo(fb); os.mkfifo(wl)
        rf, rw = os.open(fb, os.O_RDWR | os.O_NONBLOCK), os.open(wl, os.O_RDWR | os.O_NONBLOCK)
        def say(*args): return subprocess.run(['sh', str(self.SAY), *args], capture_output=True, text=True, timeout=5, env={**os.environ, 'WISP_SPLASH_FIFO': str(fb), 'WISP_BOOTSCREEN_FIFO': str(wl)})
        def drain(fd):
            try: return os.read(fd, 4096)
            except BlockingIOError: return b''
        try:
            self.assertEqual(say('Starting Steam').returncode, 0); self.assertEqual((drain(rf), drain(rw)), (b'status Starting Steam\n',) * 2)
            self.assertEqual(say('--fail', 'No display').returncode, 0); self.assertEqual((drain(rf), drain(rw)), (b'fail No display\n',) * 2)
            self.assertEqual(say('--ready').returncode, 0); self.assertEqual((drain(rf), drain(rw)), (b'ready\n', b''))             # the framebuffer splash only: `ready` would end the Wayland screen
            self.assertEqual(say('--done').returncode, 0); self.assertEqual((drain(rf), drain(rw)), (b'', b'done\n'))
        finally:
            os.close(rf); os.close(rw)

    def test_say_never_blocks_or_fails(self):
        import os, tempfile
        d = Path(tempfile.mkdtemp(prefix='wispos-say-', dir=os.environ.get('TMPDIR')))
        self.assertEqual(self.run_say(d / 'absent', 'x').returncode, 0)               # no splash at all (dev image with wisp.splash=off, or after `ready`)
        fifo = d / 'noreader'; os.mkfifo(fifo)
        self.assertEqual(self.run_say(fifo, 'x', timeout=5).returncode, 0)             # a FIFO nobody reads must not hang the unit that calls this

    def test_units_feed_the_splash_and_end_it_when_the_compositor_accepts_clients(self):
        drop = (HERE / 'rootfs/usr/lib/systemd/system/sway.service.d/10-wisp-splash.conf').read_text()
        self.assertIn('ExecStartPre=-/usr/bin/wisp-splash-say "Starting the display"', drop)     # the leading - : never fails the compositor
        setup = (HERE / 'rootfs/usr/lib/systemd/system/wisp-setup.service').read_text()
        self.assertLess(setup.index('until [ -S /var/run/0-runtime-dir/wayland-1 ]'), setup.index('wisp-splash-say --ready'))
        self.assertLess(setup.index('wisp-splash-say --ready'), setup.index('ExecStart=/usr/bin/wisp-setup'))
        self.assertIn('ExecStartPre=-/usr/bin/wisp-splash-say --ready', setup)

    def test_wrapper_reveals_the_log_when_nothing_ends_the_splash(self):
        text = (HERE / 'splash/rocknix-splash.sh').read_text()
        self.assertIn('--reveal-after 90', text)
        self.assertIn('wisp.splash=off', text)

    def boot_args(self, dev):
        import os, re
        text = (HERE / 'build-image.sh').read_text()
        lines = '\n'.join(l for l in text.splitlines() if l.startswith('BOOT_ARGS=') or l.startswith('if [ "${WISP_DEV:-}" = 1 ]; then BOOT_ARGS'))
        out = subprocess.run(['bash', '-c', lines + '\necho "$BOOT_ARGS"'], capture_output=True, text=True, env={**os.environ, 'WISP_DEV': '1' if dev else ''})
        self.assertEqual(out.returncode, 0, out.stderr)
        return out.stdout.split()

    def test_boot_arguments_hide_the_text_and_keep_the_console(self):
        for dev in (False, True):
            a = self.boot_args(dev)
            for needed in ('boot=LABEL=ROCKNIX', 'disk=LABEL=STORAGE', 'console=ttyS0,115200', 'console=tty0', 'logo.nologo', 'vt.global_cursor_default=0'):
                self.assertIn(needed, a, ('dev' if dev else 'release', needed))
        rel, dev = self.boot_args(False), self.boot_args(True)
        self.assertIn('quiet', rel); self.assertIn('systemd.show_status=true', rel)     # clean panel; systemd lines still reach the log shown with SELECT
        self.assertNotIn('quiet', dev); self.assertIn('wisp.dev', dev); self.assertNotIn('wisp.dev', rel)
        self.assertNotIn('wisp.splash=off', rel + dev)                                  # the splash is ON in both; off is an edit of extlinux.conf on any PC


class BootScreenWiringTests(unittest.TestCase):
    """The Wayland phase of the boot screen in the image (os/splash/wisp-bootscreen.c, docs/WISPOS-SPLASH-DESIGN.md): built from the shared drawing code, staged, checked, started once the
    compositor is up and before the first screens, and unable to hold the boot up or to stay when it should not."""
    def unit(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None); unit.optionxform = str
        unit.read(HERE / 'rootfs/usr/lib/systemd/system/wisp-bootscreen.service'); return unit

    def test_the_unit_starts_after_the_compositor_and_before_the_first_screens_without_being_required_by_them(self):
        u = self.unit()
        self.assertEqual(u['Unit']['After'], 'sway.service'); self.assertEqual(u['Unit']['Before'], 'wisp-setup.service wisp-steam.service')
        self.assertEqual(u['Install']['WantedBy'], 'wispos.target')
        link = HERE / 'rootfs/usr/lib/systemd/system/wispos.target.wants/wisp-bootscreen.service'
        self.assertTrue(link.is_symlink()); self.assertEqual(os.readlink(link), '../wisp-bootscreen.service')
        for other in ('wisp-setup.service', 'wisp-steam.service'):                                 # ordered after it, never dependent on it: if it cannot run, the boot goes on
            text = (HERE / 'rootfs/usr/lib/systemd/system' / other).read_text()
            self.assertNotRegex(text, r'(?m)^(Requires|BindsTo|Requisite)=.*wisp-bootscreen')

    def test_it_reports_ready_with_its_first_frame_and_cannot_hold_the_boot_up_for_long(self):
        u = self.unit()
        self.assertEqual(u['Service']['Type'], 'notify'); self.assertEqual(u['Service']['NotifyAccess'], 'main')
        self.assertLessEqual(int(u['Service']['TimeoutStartSec']), 60); self.assertEqual(u['Service']['Restart'], 'no')       # it is a boot decoration: it is never restarted over the real UI
        self.assertIn('/usr/bin/wisp-bootscreen', u['Service']['ExecStart'])

    def test_the_session_units_feed_it_a_stage_line_and_the_old_ready_stays_as_a_fallback_for_the_splash(self):
        steam = (HERE / 'rootfs/usr/lib/systemd/system/wisp-steam.service').read_text()
        self.assertIn('ExecStartPre=-/usr/bin/wisp-splash-say "Starting Steam"', steam)                  # the leading -: never fails the session
        self.assertIn('ExecStartPre=-/usr/bin/wisp-splash-say --ready', (HERE / 'rootfs/usr/lib/systemd/system/wisp-setup.service').read_text())

    def test_the_build_compiles_it_with_the_shared_drawing_code_and_the_images_own_fonts_and_stages_and_checks_it(self):
        build = (HERE / 'build-rootfs.sh').read_text()
        line = [l for l in build.splitlines() if 'wisp-bootscreen.c' in l and 'aarch64-linux-gnu-gcc' in l][0]
        self.assertIn('-Werror', line); self.assertIn('-I"$HERE/splash"', line); self.assertIn('wlr-layer-shell', build)
        gen = [l for l in build.splitlines() if 'gen_assets.py' in l and 'bootscreen' in l][0]
        self.assertIn('$T/tree/usr/share/fonts/liberation', gen)                                          # the typeface of the image, as the initramfs splash takes it
        self.assertIn('WISP_BOOTSCREEN_BIN', build); self.assertIn('usr/bin/wisp-bootscreen', (HERE / 'stage_rootfs.py').read_text())
        contract = (HERE / 'check_rootfs.py').read_text()
        for needed in ('usr/bin/wisp-bootscreen', 'usr/lib/systemd/system/wisp-bootscreen.service'): self.assertIn(needed, contract)

    def test_the_splash_and_the_boot_screen_share_one_drawing_header(self):
        scene = (HERE / 'splash/scene.h').read_text()
        for fn in ('draw_screen', 'spinner', 'hint', 'badge', 'draw_logview_lines'): self.assertIn(fn, scene)
        for prog in ('splash/wisp-splash.c', 'splash/wisp-bootscreen.c'):
            text = (HERE / prog).read_text(); self.assertIn('#include "scene.h"', text)
            for gone in ('static void rrect(', 'static void spinner(', 'static void draw_screen('): self.assertNotIn(gone, text)       # no private copy of the drawing


class SteamosManagerWiringTests(unittest.TestCase):
    """The daemon behind Steam's SteamOS calls (os/steamos/): the bus names, activation files, unit, policy and build steps must all agree (measured bus layout: docs/WISPOS-NOTES.md)."""
    C = (HERE / 'steamos/wisp-steamos-manager.c').read_text()

    def define(self, name):
        import re
        return re.search(r'#define %s "([^"]+)"' % name, self.C).group(1)

    def keyfile(self, rel):
        import configparser
        cp = configparser.ConfigParser(interpolation=None); cp.optionxform = str; cp.read(HERE / rel); return cp['D-BUS Service']

    def test_system_bus_activation_names_the_unit_and_the_unit_runs_the_atomupd_role(self):
        svc = self.keyfile('rootfs/usr/share/dbus-1/system-services/com.steampowered.Atomupd1.service')
        self.assertEqual(svc['Name'], self.define('ATOMUPD_NAME')); self.assertEqual(svc['User'], 'root')
        unit = configparser.ConfigParser(strict=False, interpolation=None); unit.optionxform = str
        unit.read(HERE / 'rootfs/usr/lib/systemd/system' / svc['SystemdService'])
        self.assertEqual(unit['Service']['ExecStart'], '/usr/bin/wisp-steamos-manager --atomupd system')
        self.assertEqual(unit['Service']['Type'], 'notify'); self.assertIn('sd_notify(0, "READY=1")', self.C)       # the unit is told when the names are taken

    def test_session_bus_activation_runs_the_manager_role_inside_steams_session(self):
        svc = self.keyfile('rootfs/usr/share/dbus-1/services/com.steampowered.SteamOSManager1.service')
        self.assertEqual(svc['Name'], self.define('MANAGER_NAME'))
        self.assertEqual(svc['Exec'], '/usr/bin/wisp-steamos-manager --manager session')
        self.assertIn('dbus-run-session', (HERE / 'rootfs/usr/bin/wisp-steam-session').read_text())        # the session bus Steam asks is the one it is started under

    def test_the_update_service_is_started_by_the_bus_only(self):
        root = HERE / 'rootfs/usr/lib/systemd/system'
        self.assertNotIn('[Install]', (root / 'wisp-steamos-manager.service').read_text())
        enabled = [str(l) for w in root.glob('*.wants') for l in w.iterdir() if l.name == 'wisp-steamos-manager.service']
        self.assertEqual(enabled, [])

    def test_only_root_may_own_the_update_name(self):
        import xml.etree.ElementTree as ET
        policies = ET.parse(HERE / 'rootfs/usr/share/dbus-1/system.d/com.steampowered.Atomupd1.conf').getroot().findall('policy')
        by = {(p.get('user') or p.get('context')): [(a.tag, dict(a.attrib)) for a in p] for p in policies}
        self.assertIn(('allow', {'own': 'com.steampowered.Atomupd1'}), by['root'])
        self.assertFalse([1 for tag, a in by['default'] if 'own' in a], 'the default policy must not let anyone own the name')

    def test_the_daemon_ends_with_its_bus_and_the_methods_that_change_things_need_root(self):
        self.assertIn('sd_bus_set_exit_on_disconnect', self.C)              # a session-activated process must not outlive Steam's session bus
        self.assertNotIn('SD_BUS_VTABLE_UNPRIVILEGED', self.C)              # sd-bus then refuses every non-root caller of a method or a writable property

    def test_the_build_compiles_it_strictly_against_the_images_libsystemd_and_installs_it(self):
        build = (HERE / 'build-rootfs.sh').read_text()
        self.assertIn('steamos/fetch-sdbus-headers.sh', build)
        line = [l for l in build.splitlines() if 'wisp-steamos-manager.c' in l and 'aarch64-linux-gnu-gcc' in l][0]
        self.assertIn('-Werror', line); self.assertIn('aarch64-linux-gnu-gcc', line)
        self.assertIn('libsystemd.so.0', build)
        self.assertIn('WISP_STEAMOS_MANAGER_BIN', build)
        self.assertIn("usr/bin/wisp-steamos-manager", (HERE / 'stage_rootfs.py').read_text())
        contract = (HERE / 'check_rootfs.py').read_text()
        for needed in ('usr/bin/wisp-steamos-manager', 'com.steampowered.Atomupd1.service', 'com.steampowered.SteamOSManager1.service', 'wisp-steamos-manager.service'): self.assertIn(needed, contract)

    def test_the_helper_ships_where_the_daemon_looks_for_it_and_the_update_source_is_all_or_nothing(self):
        import re
        default = re.search(r'"(/usr/lib/wispos/wisp-update)"', self.C).group(1)                     # the daemon's built-in helper path
        stage = (HERE / 'stage_rootfs.py').read_text()
        self.assertIn("lib / 'wisp-update'", stage); self.assertIn("usr/lib/wispos", stage); self.assertEqual(default, '/usr/lib/wispos/wisp-update')
        self.assertIn("'usr/lib/wispos/wisp-update'", (HERE / 'check_rootfs.py').read_text())
        import tempfile, importlib, os as _os
        sr = importlib.import_module('stage_rootfs')
        tree = Path(tempfile.mkdtemp(prefix='wispos-sm-', dir=_os.environ.get('TMPDIR'))); (tree / 'usr/bin').mkdir(parents=True)
        binary = tree / 'daemon'; binary.write_bytes(b'ELF')
        for env in ({'WISP_UPDATE_MANIFEST_URL': 'https://example.invalid/m.json'}, {'WISP_UPDATE_PUBKEY_FILE': str(binary)}):
            with unittest.mock.patch.dict(_os.environ, env, clear=False):
                _os.environ.pop('WISP_UPDATE_PUBKEY_FILE' if 'WISP_UPDATE_MANIFEST_URL' in env else 'WISP_UPDATE_MANIFEST_URL', None)
                with self.assertRaises(SystemExit): sr.install_steamos_manager(tree, binary)
        with unittest.mock.patch.dict(_os.environ, {'WISP_UPDATE_MANIFEST_URL': 'http://insecure.invalid/m.json', 'WISP_UPDATE_PUBKEY_FILE': str(binary)}):
            with self.assertRaises(SystemExit): sr.install_steamos_manager(tree, binary)                      # plain http is refused at build time too
        for k in ('WISP_UPDATE_MANIFEST_URL', 'WISP_UPDATE_PUBKEY_FILE'): _os.environ.pop(k, None)
        with unittest.mock.patch.object(sr, 'UPDATE_SOURCE_FILE', tree / 'absent.json'):
            sr.install_steamos_manager(tree, binary)
        self.assertTrue((tree / 'usr/lib/wispos/wisp-update').stat().st_mode & 0o111); self.assertFalse((tree / 'usr/share/wispos/update.conf').exists())       # no source = no config file
        with unittest.mock.patch.dict(_os.environ, {'WISP_UPDATE_MANIFEST_URL': 'https://example.invalid/m.json', 'WISP_UPDATE_PUBKEY_FILE': str(binary)}):
            sr.install_steamos_manager(tree, binary)
        self.assertEqual(json.loads((tree / 'usr/share/wispos/update.conf').read_text()), {'manifest_url': 'https://example.invalid/m.json'})

    def test_the_committed_source_files_decide_the_default_and_the_key_alone_bakes_nothing(self):
        import tempfile, importlib, os as _os
        sr = importlib.import_module('stage_rootfs')
        d = Path(tempfile.mkdtemp(prefix='wispos-src-', dir=_os.environ.get('TMPDIR'))); key, src = d / 'k.pub', d / 'src.json'
        with unittest.mock.patch.dict(_os.environ, {}, clear=False):
            for k in ('WISP_UPDATE_MANIFEST_URL', 'WISP_UPDATE_PUBKEY_FILE'): _os.environ.pop(k, None)
            self.assertEqual(sr.update_source(src, key), (None, None))                                        # neither file
            key.write_text('k'); self.assertEqual(sr.update_source(src, key), (None, None))                     # the key alone: no source, no guess
            src.write_text(json.dumps({'manifest_url': 'https://github.com/o/r/releases/latest/download/manifest.json'}))
            self.assertEqual(sr.update_source(src, key), ('https://github.com/o/r/releases/latest/download/manifest.json', str(key)))
            src.write_text(json.dumps({'manifest_url': 'http://insecure.invalid/m.json'}))
            with self.assertRaises(SystemExit): sr.update_source(src, key)
        with unittest.mock.patch.dict(_os.environ, {'WISP_UPDATE_MANIFEST_URL': 'https://override.invalid/m.json', 'WISP_UPDATE_PUBKEY_FILE': str(key)}):
            self.assertEqual(sr.update_source(src, key)[0], 'https://override.invalid/m.json')                  # the release process overrides the files
        self.assertTrue((HERE / 'steamos/update-key.pub').exists())                                              # the committed public key every image will trust

    def test_the_header_fetch_is_hash_pinned_to_the_systemd_the_image_runs(self):
        text = (HERE / 'steamos/fetch-sdbus-headers.sh').read_text()
        self.assertIn('ed1059ff964f5df35b6056434cc17cc83f86dc913f10489948a0b19b6081c5ec', text)         # ROCKNIX's PKG_SHA256 for systemd 261.2
        self.assertIn('sha256sum', text); self.assertIn('/mnt/hdd/', text)


class UiReclaimWiringTests(unittest.TestCase):
    """The UI memory ceiling daemon in the image: ordered before Steam (helpers must be moved young), started with the session target, built and installed with the rest."""
    def test_it_is_ordered_before_steam_takes_its_numbers_from_the_program_freezes_and_is_enabled_with_an_off_switch(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None); unit.optionxform = str
        unit.read(HERE / 'rootfs/usr/lib/systemd/system/wisp-ui-reclaim.service')
        self.assertEqual(unit['Unit']['Before'], 'wisp-steam.service')                  # pages are charged where they were allocated: the helpers must be moved while young
        # the idle ceiling is the C default (one place, tested in test_ui_reclaim.py); the game-time ceiling is 250 MiB because 96 MiB makes a running, UNFROZEN UI thrash. Operator decision
        # 2026-10-14: --follow-focus (ceiling follows the focus, nothing is frozen) with wisp-ui-priority; stability and usability first.
        self.assertEqual(unit['Service']['ExecStart'], '/usr/bin/wisp-ui-reclaim --follow-focus --high-game 262144000')
        self.assertNotIn('--freeze', unit['Service']['ExecStart'])
        self.assertEqual(unit['Install']['WantedBy'], 'wispos.target')
        link = HERE / 'rootfs/usr/lib/systemd/system/wispos.target.wants/wisp-ui-reclaim.service'
        self.assertTrue(link.is_symlink()); self.assertEqual(os.readlink(link), '../wisp-ui-reclaim.service')
        self.assertEqual(unit['Unit']['ConditionPathExists'], '!/storage/.config/wispos/ui-reclaim-off')       # the operator's way out without touching the read-only system
        self.assertEqual(unit['Service']['Restart'], 'on-failure')                                              # a crash must not leave the UI throttled for good

    def test_the_build_compiles_installs_and_checks_it(self):
        build = (HERE / 'build-rootfs.sh').read_text()
        line = [l for l in build.splitlines() if 'wisp-ui-reclaim.c' in l and 'aarch64-linux-gnu-gcc' in l][0]
        self.assertIn('-Werror', line); self.assertNotIn(' -l', line); self.assertNotIn('.so', line)                       # plain libc: no other library
        self.assertIn('WISP_UI_RECLAIM_BIN', build); self.assertIn("usr/bin/wisp-ui-reclaim", (HERE / 'stage_rootfs.py').read_text())
        self.assertIn('usr/bin/wisp-ui-reclaim', (HERE / 'check_rootfs.py').read_text())


class PadWiringTests(unittest.TestCase):
    """The gamepad daemon in the image: built and installed with the rest, tied to the desktop session's lifetime, and started by nothing else (Game mode keeps Steam Input as the only mapping)."""
    def test_the_unit_is_part_of_the_desktop_session_and_is_not_enabled_anywhere(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None); unit.optionxform = str
        unit.read(HERE / 'rootfs/usr/lib/systemd/system/wisp-pad.service')
        self.assertEqual(unit['Unit']['PartOf'], 'wisp-desktop.service')                   # stops when the desktop session stops: the pad is released for Steam
        self.assertEqual(unit['Service']['ExecStart'], '/usr/bin/wisp-pad')
        self.assertNotIn('Install', unit.sections())
        wants = HERE / 'rootfs/usr/lib/systemd/system'
        for d in wants.glob('*.wants'):
            self.assertFalse((d / 'wisp-pad.service').exists(), 'wisp-pad must not be enabled in %s' % d.name)
        for f in wants.glob('wisp-steam*.service'):                                         # and nothing that runs in Game mode pulls it in
            self.assertNotIn('wisp-pad', f.read_text())

    def test_the_build_compiles_installs_and_checks_it(self):
        build = (HERE / 'build-rootfs.sh').read_text()
        line = [l for l in build.splitlines() if 'wisp-pad.c' in l and 'aarch64-linux-gnu-gcc' in l][0]
        self.assertIn('-Werror', line)
        for src in ('pad-io.c', 'pad-logic.c'): self.assertIn(src, line)
        self.assertNotIn('.so', line)                                                       # libc and libm only
        self.assertIn('WISP_PAD_BIN', build); self.assertIn('usr/bin/wisp-pad', (HERE / 'stage_rootfs.py').read_text())
        check = (HERE / 'check_rootfs.py').read_text()
        self.assertIn('usr/bin/wisp-pad', check); self.assertIn('usr/lib/systemd/system/wisp-pad.service', check)


class UsbPortWiringTests(unittest.TestCase):
    """The USB-C port tool in the image (wisp-usb): installed, its watcher not started by anything but host mode, ROCKNIX's own gadget gone, the kernel gate in the build."""
    def test_the_watcher_unit_runs_the_tool_and_is_not_enabled_anywhere(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None); unit.optionxform = str
        unit.read(HERE / 'rootfs/usr/lib/systemd/system/wisp-usb-watch.service')
        self.assertEqual(unit['Service']['ExecStart'], '/usr/bin/wisp-usb watch')
        self.assertNotIn('Install', unit.sections())                                      # host mode is off at every boot: nothing starts the watcher but `wisp-usb host`
        for wants in (HERE / 'rootfs/usr/lib/systemd/system').glob('*.wants'): self.assertFalse((wants / 'wisp-usb-watch.service').exists(), wants)

    def test_the_tool_is_executable_and_part_of_the_boot_contract(self):
        self.assertTrue(os.access(HERE / 'rootfs/usr/bin/wisp-usb', os.X_OK))
        self.assertTrue((HERE / 'rootfs/usr/bin/wisp-usb').read_text().startswith('#!/usr/bin/python3'))
        for path in ('usr/bin/wisp-usb', 'usr/lib/systemd/system/wisp-usb-watch.service'): self.assertIn(path, check_rootfs.BOOT_CONTRACT)

    def test_rocknixs_gadget_is_pruned_so_that_nothing_else_can_switch_the_port_role(self):
        prune = [l.strip() for l in (HERE / 'rootfs-prune.txt').read_text().splitlines() if l.strip() and not l.startswith('#')]
        for path in ('usr/bin/usbgadget', 'usr/lib/udev/rules.d/80-usbgadget.rules', 'usr/lib/autostart/common/081-usbgadget', 'usr/sbin/umtprd', 'etc/umtprd'): self.assertIn(path, prune)

    def test_the_kernel_gate_is_what_the_tool_writes(self):
        patch = (HERE / 'boot/patches/0001-phy-sun4i-usb-host-gate.patch').read_text(); tool = (HERE / 'rootfs/usr/bin/wisp-usb').read_text()
        self.assertIn('host_enable', patch); self.assertIn("sun4i-usb-phy/*/host_enable", tool)          # the attribute's name and the driver directory it appears under
        self.assertIn('.name = "sun4i-usb-phy"', patch)                                                      # the driver whose directory holds it (a context line of the hunk)


class CheckReleaseTests(unittest.TestCase):
    """os/check_release.py: the release-readiness report (decision support; --strict makes DIFFERS rows fail)."""
    @staticmethod
    def tree(**kw):
        import accounts, tempfile
        root = Path(tempfile.mkdtemp(prefix='wispos-rel-', dir=os.environ.get('TMPDIR')))
        for d in ('etc/ssh', 'usr/lib/systemd/system/multi-user.target.wants', 'usr/lib/autostart/common', 'usr/share/wispos'): (root / d).mkdir(parents=True)
        (root / 'etc/shadow').write_text('root:*:1::::::\nwisp:%s:1::::::\n' % (accounts.sha512_crypt('wisp') if kw.get('default_pw', True) else '$6$x$y'))
        (root / 'etc/ssh/sshd_config').write_text('PasswordAuthentication %s\nPermitRootLogin prohibit-password\n' % kw.get('pa', 'yes'))
        (root / 'etc/os-release').write_text('NAME="WispOS"\nID="wispos"\n' if kw.get('wispos', True) else 'OS_NAME="ROCKNIX"\n')
        (root / 'usr/lib/systemd/system').mkdir(parents=True, exist_ok=True)
        (root / 'usr/lib/systemd/system/sshd.service').write_text('[Unit]\n' + ('ConditionPathExists=/storage/.config/wispos/ssh-enabled\n' if kw.get('sshd_conditional') else '') + '[Install]\nWantedBy=multi-user.target\n')
        if kw.get('sshd', True): (root / 'usr/lib/systemd/system/multi-user.target.wants/sshd.service').symlink_to('../sshd.service')
        if kw.get('autostart_input'): (root / 'usr/lib/autostart/common/098-deviceutils').write_text('nohup systemctl start input &\n')
        return root

    def rows(self, root, extlinux='LABEL x\n  APPEND boot=LABEL=ROCKNIX quiet\n'):
        import check_release
        return {r['key']: r for r in check_release.rows(root, extlinux)}

    def test_a_tree_with_todays_defaults_reports_the_known_gaps_and_the_decisions(self):
        r = self.rows(self.tree(), 'LABEL x\n  APPEND boot=LABEL=ROCKNIX console=ttyS0,115200 systemd.debug_shell=ttyS0\n')
        self.assertEqual({k: v['status'] for k, v in r.items() if k in ('wisp_password', 'ssh_password_auth')}, {'wisp_password': 'ACCEPTED', 'ssh_password_auth': 'ACCEPTED'})
        for key in ('sshd_enabled', 'update_source'): self.assertEqual(r[key]['status'], 'DIFFERS', key)
        for key in ('debug_shell_arg', 'serial_console_arg'): self.assertEqual(r[key]['status'], 'OK', key)          # ROCKNIX's own convention, kept (operator, 2026-10-11)
        for key in ('root_password', 'ssh_root_login', 'tty9_shell', 'rocknix_units', 'os_release_identity'): self.assertEqual(r[key]['status'], 'OK', key)

    def test_a_tree_that_follows_the_proposal_has_no_differences(self):
        root = self.tree(sshd_conditional=True, default_pw=False, pa='no'); (root / 'usr/share/wispos/update.conf').write_text('{}'); (root / 'usr/share/wispos/update-key.pub').write_text('k')
        r = self.rows(root)
        self.assertEqual([k for k, v in r.items() if v['status'] != 'OK'], [])

    def test_the_tty9_shell_and_a_rocknix_name_are_differences(self):
        root = self.tree(wispos=False); (root / 'usr/lib/systemd/system/sysinit.target.wants').mkdir(); (root / 'usr/lib/systemd/system/sysinit.target.wants/debug-shell.service').symlink_to('../x')
        r = self.rows(root); self.assertEqual((r['tty9_shell']['status'], r['os_release_identity']['status']), ('DIFFERS', 'DIFFERS'))

    def test_usb_host_mode_without_a_recorded_bench_result_is_a_gap_in_a_release(self):
        root = self.tree(); (root / 'usr/bin').mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.rows(root)['usb_host_bench']['status'], 'OK')                                 # no host-mode tool in the image: nothing to prove
        (root / 'usr/bin/wisp-usb').write_text('#!/bin/sh\n')
        self.assertEqual(self.rows(root)['usb_host_bench']['status'], 'DIFFERS')
        (root / 'usr/share/wispos/usb-host-disabled').write_text('off\n'); r = self.rows(root)['usb_host_bench']                       # switched off: nothing to prove
        self.assertEqual(r['status'], 'OK'); self.assertIn('switched off', r['current']); (root / 'usr/share/wispos/usb-host-disabled').unlink()
        (root / 'usr/share/wispos/usb-host-bench').write_text('T1-T4 passed\n')
        r = self.rows(root)['usb_host_bench']; self.assertEqual(r['status'], 'OK'); self.assertIn('T1-T4 passed', r['current'])

    def test_a_unit_that_an_autostart_step_starts_counts_as_running_even_without_an_enablement_link(self):
        r = self.rows(self.tree(autostart_input=True))
        self.assertEqual(r['rocknix_units']['status'], 'ACCEPTED'); self.assertIn('input.service (started by autostart step 098-deviceutils)', r['rocknix_units']['current'])      # kept on purpose
        self.assertIn('accepted for this release: the volume and brightness buttons', r['rocknix_units']['current'])

    def test_leftovers_kept_on_purpose_are_accepted_with_their_reasons_and_any_other_is_a_gap(self):
        import check_release
        root = self.tree(); wants = root / 'usr/lib/systemd/system/multi-user.target.wants'
        for u in ('rocknix-autostart.service', 'powerstate.service'): (root / 'usr/lib/systemd/system' / u).write_text('[Service]\n'); (wants / u).symlink_to('../' + u)
        r = self.rows(root)['rocknix_units']; self.assertEqual(r['status'], 'ACCEPTED'); self.assertIn('hardware bring-up', r['current']); self.assertIn('CPU performance policy', r['current'])
        (root / 'usr/lib/systemd/system/shell.service').write_text('[Service]\n'); (wants / 'shell.service').symlink_to('../shell.service')                    # a leftover nobody explained
        r = self.rows(root)['rocknix_units']; self.assertEqual(r['status'], 'DIFFERS'); self.assertTrue(r['current'].startswith('shell.service;')); self.assertIn('hardware bring-up', r['current'])
        self.assertEqual(set(check_release.EXPLAINED_LEFTOVERS) & {'shell.service', 'debug-shell.service'}, set())                                                  # the shells are never excused

    def test_a_unit_enabled_only_in_a_target_the_boot_graph_never_reaches_is_not_running(self):
        root = self.tree(); d = root / 'usr/lib/systemd/system/textmode.target.wants'; d.mkdir()
        (root / 'usr/lib/systemd/system/shell.service').write_text('[Service]\n'); (d / 'shell.service').symlink_to('../shell.service')
        self.assertEqual(self.rows(root)['rocknix_units']['status'], 'OK')                                       # textmode.target is only started by a kernel argument

    def test_strict_passes_with_only_accepted_leftovers_and_the_release_text_names_them(self):
        import check_release, contextlib, io
        root = self.tree(sshd_conditional=True, default_pw=False, pa='no'); (root / 'usr/share/wispos/update.conf').write_text('{}'); (root / 'usr/share/wispos/update-key.pub').write_text('k')
        (root / 'usr/lib/systemd/system/input.service').write_text('[Service]\n'); (root / 'usr/lib/systemd/system/multi-user.target.wants/input.service').symlink_to('../input.service')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(check_release.main(['--tree', str(root), '--extlinux', '/nonexistent', '--strict']), 0)
        self.assertIn('1 are gaps accepted for this release', out.getvalue())

    def test_strict_fails_only_on_differences_and_a_missing_extlinux_is_unknown_not_ok(self):
        import check_release, contextlib, io
        root = self.tree()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(check_release.main(['--tree', str(root), '--extlinux', '/nonexistent']), 0)                   # report mode never fails
            self.assertEqual(check_release.main(['--tree', str(root), '--extlinux', '/nonexistent', '--strict']), 1)
            self.assertEqual(check_release.main(['--tree', '/nonexistent']), 2)
        r = self.rows(root, None); self.assertEqual(r['debug_shell_arg']['status'], 'OK'); self.assertIn('unknown', r['debug_shell_arg']['current'])      # informational: never a gap, and an unknown file is said to be unknown


if __name__ == '__main__':
    unittest.main()
