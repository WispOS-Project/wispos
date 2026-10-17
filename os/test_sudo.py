#!/usr/bin/env python3
"""sudo for the device owner: the pinned debs install, the wisp rule lands
exact, and a tampered or wrong-arch package is refused (os/sudo.lock.json,
stage_rootfs.install_sudo). Fake debs are built with dpkg-deb; no network."""
import hashlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import stage_rootfs


def make_deb(root, package, architecture='arm64', payload=(), links=(), control_extra=''):
    """Build a minimal fake deb under root; returns its path."""
    src = Path(root) / f'{package}-src'
    (src / 'DEBIAN').mkdir(parents=True)
    (src / 'DEBIAN/control').write_text(
        f'Package: {package}\nVersion: 0\nArchitecture: {architecture}\n{control_extra}')
    for rel, text, mode in payload:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        p.chmod(mode)
    for rel, target in links:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.symlink_to(target)
    deb = Path(root) / f'{package}_0_{architecture}.deb'
    subprocess.run(['dpkg-deb', '-b', str(src), str(deb)],
                   check=True, capture_output=True, text=True)
    return deb


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_lock(root, debs):
    lock = {'schema': 1, 'purpose': 'test', 'packages': [
        {'package': name, 'version': '0', 'architecture': arch,
         'sha256': sha(deb),
         'url': f'https://example.invalid/{deb.name}'}
        for name, (deb, arch) in debs.items()]}
    p = Path(root) / 'test-sudo.lock.json'
    p.write_text(json.dumps(lock))
    return p


class SudoInstallTests(unittest.TestCase):
    def test_install_lands_the_binary_setuid_and_the_wisp_rule_exact(self):
        with tempfile.TemporaryDirectory(prefix='wispos-sudo-') as root:
            sudo = make_deb(root, 'sudo', payload=[
                ('usr/bin/sudo', 'fake-sudo', 0o755),
                ('usr/libexec/sudo/sudoers.so', 'fake-plugin', 0o644),
                ('etc/sudoers', 'root ALL=(ALL:ALL) ALL\n', 0o440)])
            lib = make_deb(root, 'libpam0g', payload=[
                ('usr/lib/aarch64-linux-gnu/libpam.so.0.85.1', 'fake-pam', 0o644),
                ('usr/lib/aarch64-linux-gnu/security/pam_permit.so', 'permit', 0o644),
                ('usr/lib/aarch64-linux-gnu/security/pam_unix.so', 'unused', 0o644),
                ('usr/lib/aarch64-linux-gnu/libpam_misc.so.0', 'unused', 0o644)],
                links=[('usr/lib/aarch64-linux-gnu/libpam.so.0', 'libpam.so.0.85.1')])
            lock = write_lock(root, {'sudo': (sudo, 'arm64'),
                                     'libpam0g': (lib, 'arm64')})
            tree = Path(root) / 'tree'
            stage_rootfs.install_sudo(tree, root, lock_path=lock)
            bits = (tree / 'usr/bin/sudo')
            self.assertTrue(bits.is_file())
            self.assertEqual(stat.S_IMODE(bits.stat().st_mode), 0o4755)
            rule = tree / 'etc/sudoers.d/wisp'
            self.assertEqual(rule.read_text(), stage_rootfs.SUDOERS_WISP)
            self.assertIn('wisp ALL=(ALL:ALL) NOPASSWD: ALL', rule.read_text())
            self.assertEqual(stat.S_IMODE(rule.stat().st_mode), 0o440)
            pam = tree / 'etc/pam.d/sudo'
            self.assertEqual(pam.read_text(), stage_rootfs.PAM_SUDO)
            self.assertIn('pam_permit', pam.read_text())
            # login shells use the separate sudo-i service (#23): same
            # permit-only policy, never the package's unusable stack
            login = tree / 'etc/pam.d/sudo-i'
            self.assertEqual(login.read_text(), stage_rootfs.PAM_SUDO)
            self.assertTrue((tree / 'usr/libexec/sudo/sudoers.so').is_file())
            # libraries flatten into usr/lib; the used module stays on its path
            self.assertTrue((tree / 'usr/lib/libpam.so.0.85.1').is_file())
            self.assertEqual(os.readlink(tree / 'usr/lib/libpam.so.0'),
                             'libpam.so.0.85.1')
            self.assertTrue((tree / 'usr/lib/aarch64-linux-gnu/security/pam_permit.so').is_file())
            # nothing unreferenced ships: no unused modules, no helper libs
            self.assertFalse((tree / 'usr/lib/aarch64-linux-gnu/security/pam_unix.so').exists())
            self.assertFalse((tree / 'usr/lib/libpam_misc.so.0').exists())
            self.assertFalse((tree / 'usr/lib/aarch64-linux-gnu/libpam.so.0.85.1').exists())

    def test_tampered_package_is_refused_before_anything_installs(self):
        with tempfile.TemporaryDirectory(prefix='wispos-sudo-') as root:
            sudo = make_deb(root, 'sudo', payload=[('usr/bin/sudo', 'x', 0o755)])
            lock = write_lock(root, {'sudo': (sudo, 'arm64')})
            with open(sudo, 'r+b') as f:  # flip a byte: hash no longer matches
                f.seek(100)
                f.write(b'\x00' if f.read(1) != b'\x00' else b'\x01')
            tree = Path(root) / 'tree'
            with self.assertRaises(SystemExit):
                stage_rootfs.install_sudo(tree, root, lock_path=lock)
            self.assertFalse((tree / 'usr/bin/sudo').exists())

    def test_only_the_known_unit_mask_is_skipped_other_absolute_links_refused(self):
        with tempfile.TemporaryDirectory(prefix='wispos-sudo-') as root:
            sudo = make_deb(root, 'sudo',
                            payload=[('usr/bin/sudo', 'x', 0o755)],
                            links=[('usr/lib/systemd/system/sudo.service',
                                    '/dev/null')])
            lock = write_lock(root, {'sudo': (sudo, 'arm64')})
            tree = Path(root) / 'tree'
            stage_rootfs.install_sudo(tree, root, lock_path=lock)
            self.assertTrue((tree / 'usr/bin/sudo').is_file())
            self.assertFalse(
                (tree / 'usr/lib/systemd/system/sudo.service').exists())
            evil = make_deb(root, 'libpam0g',
                            payload=[('usr/lib/x', 'x', 0o644)],
                            links=[('usr/bin/evil', '/etc/shadow')])
            lock = write_lock(root, {'sudo': (sudo, 'arm64'),
                                     'libpam0g': (evil, 'arm64')})
            with self.assertRaises(ValueError):
                stage_rootfs.install_sudo(Path(root) / 'tree2', root,
                                          lock_path=lock)

    def test_wrong_architecture_is_refused(self):
        with tempfile.TemporaryDirectory(prefix='wispos-sudo-') as root:
            sudo = make_deb(root, 'sudo', architecture='amd64',
                            payload=[('usr/bin/sudo', 'x', 0o755)])
            lock = write_lock(root, {'sudo': (sudo, 'amd64')})
            with self.assertRaises(SystemExit):
                stage_rootfs.install_sudo(Path(root) / 'tree', root,
                                          lock_path=lock)


class SudoLockTests(unittest.TestCase):
    def test_the_real_lock_pins_arm64_urls_and_hashes(self):
        lock = json.loads((HERE / 'sudo.lock.json').read_text())
        self.assertEqual(
            {p['package'] for p in lock['packages']},
            {'sudo', 'libpam0g', 'libpam-modules', 'libaudit1',
             'libselinux1', 'libapparmor1', 'libcap-ng0', 'libpcre2-8-0'})
        for entry in lock['packages']:
            self.assertEqual(entry['architecture'], 'arm64')
            self.assertTrue(entry['url'].startswith(
                'https://ports.ubuntu.com/ubuntu-ports/'))
            self.assertTrue(entry['url'].endswith('_arm64.deb'))
            self.assertIn(entry['version'].split(':')[-1], entry['url'])
            self.assertRegex(entry['sha256'], r'^[0-9a-f]{64}$')


if __name__ == '__main__':
    unittest.main()
