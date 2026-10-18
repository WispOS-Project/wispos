#!/usr/bin/env python3
"""Static checks on a staged WispOS rootfs tree: did pruning leave anything dangling?

    check_rootfs.py snapshot TREE OUT.json        # record the dependency picture of an unpruned tree
    check_rootfs.py verify   TREE [BASELINE.json] # check the staged tree; with a baseline, only
                                                  # problems that pruning/staging introduced count

Checks (all offline; nothing is executed):
  * ELF: every DT_NEEDED library of every aarch64 ELF resolves inside the tree (RPATH/RUNPATH with
    $ORIGIN, then the system directories).
  * Units: every ExecStart*/ExecStop* program of every unit exists in the tree (programs under
    /storage, /run, /dev, /proc, /sys, /tmp are runtime paths and are skipped).
  * Enablement: every symlink in a *.wants / *.requires directory resolves.
  * Boot contract: the files ROCKNIX's initramfs and our session need are present.
"""
import json, os, re, subprocess, sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

READELF = 'aarch64-linux-gnu-readelf'
# Libraries Steam's client needs that the root lacks are baked here (stage_rootfs.STEAM_LIB_PACKAGES); the Steam session puts this
# directory on LD_LIBRARY_PATH (usr/bin/wisp-steam-session), so the libraries in it resolve each other here. Nothing else may.
BAKED_LIB_DIR = 'usr/lib/wispos/lib'
SYSTEM_DIRS = ['usr/lib', 'usr/lib64', 'usr/lib/pulseaudio', 'usr/lib/pipewire-0.3', 'usr/lib/systemd']
RUNTIME_PREFIXES = ('/storage', '/run', '/dev', '/proc', '/sys', '/tmp', '/var', '/flash')


def real(tree, abs_path, depth=0):
    """Resolve an absolute in-image path against the tree, following symlinks inside it."""
    if depth > 20:
        return None
    cur = tree
    for part in Path(abs_path).parts[1:]:
        cur = cur / part
        if cur.is_symlink():
            target = os.readlink(cur)
            nxt = target if target.startswith('/') else os.path.normpath('/' + str(cur.parent.relative_to(tree)) + '/' + target)
            sub = real(tree, nxt, depth + 1)
            if sub is None:
                return None
            cur = sub
    return cur if cur.exists() or cur.is_symlink() else None


def elf_needs(path):
    """Return (needed sonames, rpath dirs) for an aarch64 ELF, or None for anything else."""
    try:
        with open(path, 'rb') as f:
            head = f.read(20)
    except OSError:
        return None
    if head[:4] != b'\x7fELF' or head[4] != 2 or int.from_bytes(head[18:20], 'little') != 183:
        return None
    out = subprocess.run([READELF, '-d', str(path)], capture_output=True, text=True).stdout
    needed = re.findall(r'\(NEEDED\)\s+Shared library: \[(.+?)\]', out)
    rpaths = [p for m in re.findall(r'\((?:RPATH|RUNPATH)\)\s+Library r(?:un)?path: \[(.+?)\]', out) for p in m.split(':')]
    return needed, rpaths


def elf_report(tree):
    files = []
    for dirpath, _dirs, names in os.walk(tree):
        for name in names:
            p = Path(dirpath) / name
            if not p.is_symlink() and p.stat().st_size > 64:
                files.append(p)
    # Every library name any file in the tree provides (file or symlink name), by directory.
    provided = {}
    for p in files:
        provided.setdefault(p.name, []).append(str(p.parent.relative_to(tree)))
    for dirpath, _d, names in os.walk(tree):
        for name in names:
            p = Path(dirpath) / name
            if p.is_symlink():
                provided.setdefault(p.name, []).append(str(p.parent.relative_to(tree)))
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(lambda p: (p, elf_needs(p)), files))
    missing = {}
    for path, info in results:
        if info is None:
            continue
        needed, rpaths = info
        here = str(path.parent.relative_to(tree))
        search = [here if r in ('$ORIGIN', '${ORIGIN}') else r.replace('$ORIGIN', '/' + here).lstrip('/') for r in rpaths]
        if here == BAKED_LIB_DIR:
            search.append(BAKED_LIB_DIR)
        for soname in needed:
            dirs = provided.get(soname, [])
            if not any(d in SYSTEM_DIRS or d in search for d in dirs):
                missing.setdefault(str(path.relative_to(tree)), []).append(soname)
    return missing


def unit_report(tree):
    problems = {}
    for unit_dir in ('usr/lib/systemd/system', 'etc/systemd/system'):
        base = tree / unit_dir
        if not base.is_dir():
            continue
        for unit in sorted(base.iterdir()):
            if unit.is_symlink() or not unit.is_file() or unit.suffix not in ('.service', '.mount', '.socket', '.path', '.timer'):
                continue
            for line in unit.read_text(errors='replace').splitlines():
                m = re.match(r'\s*(Exec\w*)=\s*([-@+!:|]*)\s*(\S+)', line)
                if not m or not m.group(3).startswith('/') or m.group(3).startswith(RUNTIME_PREFIXES):
                    continue
                if real(tree, m.group(3)) is None:
                    problems.setdefault(f'{unit_dir}/{unit.name}', []).append(f'{m.group(1)}={m.group(3)}')
    return problems


def wants_report(tree):
    dangling = []
    for dirpath, _d, names in os.walk(tree / 'usr/lib/systemd/system'):
        if not Path(dirpath).name.endswith(('.wants', '.requires')):
            continue
        for name in names:
            link = Path(dirpath) / name
            if link.is_symlink() and not link.exists():
                dangling.append(str(link.relative_to(tree)))
    return dangling


BOOT_CONTRACT = [
    'usr/lib/systemd/systemd', 'usr/sbin/kernel-overlays-setup', 'usr/bin/busybox', 'usr/bin/vi',
    'usr/sbin/sshd', 'usr/bin/ssh-keygen', 'usr/bin/sway', 'usr/bin/seatd', 'usr/bin/dbus-run-session',
    'usr/lib/systemd/system/wispos.target', 'usr/lib/systemd/system/wisp-steam.service',
    'usr/lib/systemd/system/sshd.service', 'usr/lib/systemd/system/sway.service', 'usr/bin/wisp-steam-session', 'usr/bin/wisp-display', 'usr/bin/wisp-game-display', 'usr/lib/systemd/system/wisp-game-display.service', 'usr/bin/wisp-ui-priority', 'usr/bin/wisp-home', 'usr/bin/wisp-steam-open', 'usr/lib/systemd/system/wisp-ui-priority.service', 'usr/lib/systemd/system/wisp-home.service', 'usr/bin/wisp-screen', 'usr/bin/wisp-screen-idle', 'usr/bin/wisp-steam-seed', 'usr/bin/wisp-steam-watch', 'usr/lib/systemd/system/wisp-steam-watch.service', 'usr/bin/wisp-proton-patch', 'usr/bin/steamos-polkit-helpers/steamos-format-sdcard', 'usr/bin/steamos-polkit-helpers/steamos-format-device', 'usr/lib/wispos/tf2-card', 'usr/bin/wisp-compat-gate', 'usr/bin/wisp-fex-official', 'usr/bin/wisp-wow64-valve', 'usr/lib/systemd/system/wisp-wow64-valve.path', 'usr/lib/systemd/system/wisp-wow64-valve.service', 'usr/share/wispos/valve-wow64.lock.json', 'usr/share/wispos/fex-official/official-fex.lock.json', 'usr/lib/udev/rules.d/98-wisp-tf2.rules', 'usr/lib/systemd/system/wisp-tf2-mount.service', 'usr/lib/systemd/system/wisp-tf2-umount.service', 'usr/lib/systemd/system/wisp-screen-idle.service',
    'usr/bin/wisp-setup', 'usr/bin/wisp-steamos-manager', 'usr/bin/wisp-steam-launch', 'usr/bin/steamos-polkit-helpers/steamos-update', 'usr/bin/wisp-ui-reclaim', 'usr/lib/systemd/system/wisp-ui-reclaim.service', 'usr/bin/wisp-pad', 'usr/lib/systemd/system/wisp-pad.service', 'usr/bin/wisp-dock', 'usr/lib/systemd/system/wisp-dock.service', 'usr/lib/systemd/system/wisp-desktop.service', 'usr/lib/wispos/wisp-desktop-session', 'usr/bin/wisp-session-select', 'usr/bin/steamos-session-select', 'usr/bin/wisp-about', 'usr/bin/wisp-browser', 'usr/bin/wisp-download-action', 'usr/share/wispos/browser.lock.json', 'usr/lib/wispos/browser-policies.json', 'usr/lib/wispos/browser-user.js', 'usr/share/wispos/startpage/index.html', 'usr/share/wispos/wallpaper.png', 'usr/share/applications/wisp-terminal.desktop', 'usr/bin/wisp-usb', 'usr/bin/sudo', 'etc/sudoers.d/wisp', 'etc/pam.d/sudo', 'etc/pam.d/sudo-i', 'usr/lib/systemd/system/wisp-usb-watch.service', 'usr/lib/wispos/wisp-update', 'usr/lib/wispos/wisp-update-confirm', 'usr/lib/wispos/wisp-fat-backup-heal', 'usr/bin/wisp-bootscreen', 'usr/lib/systemd/system/wisp-bootscreen.service', 'usr/lib/systemd/system/wisp-update-confirm.service', 'usr/bin/steamos-polkit-helpers/steamos-reboot', 'usr/lib/systemd/system/wisp-steamos-manager.service', 'usr/share/dbus-1/system-services/com.steampowered.Atomupd1.service', 'usr/share/dbus-1/services/com.steampowered.SteamOSManager1.service', 'usr/share/dbus-1/system.d/com.steampowered.Atomupd1.conf', 'usr/lib/wispos/steam-install', 'usr/lib/wispos/native_client.py', 'usr/share/wispos/native-client.lock.json', 'usr/share/wispos/compat-tool.lock.json',
    'usr/lib/wispos/lib/libibus-1.0.so.5', 'usr/lib/wispos/lib/libva.so.2', 'usr/lib/wispos/lib/libvdpau.so.1', 'usr/lib/wispos/lib/libssh2.so.1', 'usr/lib/wispos/lib/libbrotlidec.so.1', 'usr/lib/wispos/lib/libbrotlicommon.so.1', 'usr/lib/wispos/lib/libasyncns.so.0', 'usr/share/sway/config', 'usr/share/wispos/wispos-badge.png', 'usr/lib/systemd/system/wisp-setup.service',
    'usr/lib/wispos/libwispos-ldadd.so', 'usr/lib/wispos/libwispos-ldadd.sha256', 'usr/bin/wisp-bootlog', 'usr/bin/wisp-firstboot', 'usr/bin/wisp-splash-say', 'usr/lib/systemd/system/sway.service.d/10-wisp-splash.conf', 'usr/lib/systemd/system/wisp-bootlog.timer',
    'usr/bin/autostart', 'usr/lib/systemd/system/rocknix-autostart.service', 'usr/lib/autostart/common/050-audio',
    'usr/lib/autostart/common/006-display', 'usr/lib/autostart/common/111-sway-init', 'usr/lib/autostart/quirks/platforms/H700',
    'etc/passwd', 'etc/shadow', 'etc/group', 'etc/os-release',
    'usr/share/mime/mime.cache', 'usr/bin/wisp-images',
]


# Things no image of ours may contain, dev or release. Each is a finding with a reason in os/rootfs-prune.txt or docs/WISPOS-OS-DESIGN.md.
FORBIDDEN = [
    'usr/lib/systemd/system/sysinit.target.wants/debug-shell.service',      # an unauthenticated root shell on tty9, enabled in every boot by ROCKNIX
]


# Our own programs and settings live here; the image's Python writes no bytecode on a read-only system, so a __pycache__ in these directories is the HOST's, copied in by mistake.
OWN_DIRS = ['usr/bin', 'usr/sbin', 'usr/lib/wispos', 'usr/share/wispos', 'etc']


def stray_bytecode(tree):
    return sorted(str(p.relative_to(tree)) for d in OWN_DIRS if (tree / d).is_dir() for p in (tree / d).rglob('__pycache__'))


def contract_report(tree, release='7.2.0'):
    missing = [p for p in BOOT_CONTRACT if not (tree / p).exists()]
    missing += [f'MUST NOT CONTAIN {p} (host Python bytecode)' for p in stray_bytecode(tree)]
    base = tree / 'usr/lib/kernel-overlays/base/lib'
    for rel in (f'modules/{release}/modules.dep', f'modules/{release}/modules.alias', 'firmware/regulatory.db'):
        if not (base / rel).exists():
            missing.append(f'kernel-overlays/base/lib/{rel}')
    dev_files = ['usr/bin/wisp-dev-announce', 'usr/bin/wisp-dev-pad', 'usr/bin/wisp-dev-blackbox', 'usr/bin/wisp-dev-watchdog', 'usr/lib/systemd/system/wisp-dev-announce.service',
                 'usr/lib/systemd/system/wisp-dev-blackbox.service', 'usr/lib/systemd/system/wisp-dev-watchdog.service', 'usr/lib/systemd/system/wisp-dev-paneltest.service',
                 'usr/lib/tmpfiles.d/wisp-dev.conf', 'usr/lib/wispos-dev/diag/paneltest.py', 'usr/lib/wispos-dev/diag/kmsflip.py', 'usr/lib/wispos-dev/diag/gsstub.py', 'usr/lib/wispos/DEV-BUILD']
    if os.environ.get('WISP_DEV') == '1':
        missing += [f for f in dev_files if not (tree / f).exists()]
    else:
        missing += [f'RELEASE MUST NOT CONTAIN {f}' for f in dev_files if (tree / f).exists()]
    missing += [f'MUST NOT CONTAIN {f}' for f in FORBIDDEN if os.path.lexists(tree / f)]
    default = tree / 'usr/lib/systemd/system/default.target'
    if os.readlink(default) != 'wispos.target' if default.is_symlink() else True:
        missing.append('default.target -> wispos.target')
    return missing


def snapshot(tree, out):
    Path(out).write_text(json.dumps({'elf': elf_report(tree), 'units': unit_report(tree)}, indent=1, sort_keys=True))


def verify(tree, baseline=None):
    elf, units = elf_report(tree), unit_report(tree)
    if baseline:
        old = json.loads(Path(baseline).read_text())
        elf = {f: [s for s in v if s not in old['elf'].get(f, [])] for f, v in elf.items()}
        units = {f: [e for e in v if e not in old['units'].get(f, [])] for f, v in units.items()}
        elf, units = {k: v for k, v in elf.items() if v}, {k: v for k, v in units.items() if v}
    wants, contract = wants_report(tree), contract_report(tree)
    for title, rows in (('ELF libraries that no longer resolve', {f: ', '.join(v) for f, v in elf.items()}),
                        ('unit programs that are missing', {f: ', '.join(v) for f, v in units.items()}),
                        ('dangling enablement links', {f: '' for f in wants}),
                        ('boot-contract files missing', {f: '' for f in contract})):
        print(f'{title}: {len(rows)}')
        for name, detail in list(rows.items())[:40]:
            print(f'  {name}  {detail}')
    return not (elf or units or wants or contract)


if __name__ == '__main__':
    if len(sys.argv) >= 4 and sys.argv[1] == 'snapshot':
        snapshot(Path(sys.argv[2]), sys.argv[3])
    elif len(sys.argv) >= 3 and sys.argv[1] == 'verify':
        sys.exit(0 if verify(Path(sys.argv[2]), sys.argv[3] if len(sys.argv) > 3 else None) else 1)
    else:
        raise SystemExit(__doc__)
