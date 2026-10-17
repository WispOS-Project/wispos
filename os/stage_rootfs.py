#!/usr/bin/env python3
"""Turn an extracted ROCKNIX userland tree into the WispOS root filesystem tree.

    stage_rootfs.py TREE KERNEL_OUT [modules-only]

TREE is the unpacked pinned SYSTEM image (run under fakeroot so ownership is kept); KERNEL_OUT is
the output directory of boot/build-kernel.sh. In order, it:

  1. removes the paths listed in rootfs-prune.txt (the emulation stack and what only serves it);
  2. replaces the kernel modules and built-in firmware copies with the ones from our kernel build;
  3. copies the WispOS files from rootfs/ over the tree (session unit, target, sshd, scripts);
  4. puts WispOS's sshd settings first in sshd_config (sshd uses the first value it reads);
  5. adds the `wisp` account (accounts.py), makes wispos.target the default target, rebrands os-release.

Every step fails loudly: a prune pattern that matches nothing, a missing module directory or a
missing overlay target stops the build instead of producing a quietly different image.
"""
import glob, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import accounts

# OS_VERSION keeps ROCKNIX's YYYYMMDD form: the tools that read it (rocknix-info) parse that shape.
OS_RELEASE_OVERRIDES = {'OS_NAME': 'WispOS', 'OS_VERSION': '20261007', 'OS_BUILD': 'wispos',
                        'HOME_URL': ''}
LOCKFILE = Path(os.environ.get('WISP_LOCKFILE', HERE.parent / 'manifest' / 'lockfile.json'))      # the override is for tests


def lockfile():
    """manifest/lockfile.json, the authoritative version of the image (AGENTS.md, "Versioning and changelog"): `version` (SemVer) and, once a release candidate is being built,
    `build_id` (YYYYMMDD.N). Read when used (a test points LOCKFILE elsewhere); a malformed one stops the build."""
    lock = json.loads(LOCKFILE.read_text())
    if not re.fullmatch(r'\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?', str(lock.get('version', ''))): raise SystemExit(f'{LOCKFILE}: version must look like 0.1.0, not {lock.get("version")!r}')
    if lock.get('build_id') is not None and not re.fullmatch(r'\d{8}\.\d+', str(lock['build_id'])): raise SystemExit(f'{LOCKFILE}: build_id must look like 20261012.1, not {lock["build_id"]!r}')
    return lock


def size_of(path):
    if path.is_symlink() or path.is_file():
        return path.lstat().st_size
    return sum(p.lstat().st_size for p in path.rglob('*') if not p.is_dir())


def prune(tree, manifest):
    """Delete every manifest path; returns (bytes freed, patterns that matched nothing)."""
    freed, unmatched = 0, []
    for raw in Path(manifest).read_text().splitlines():
        pattern = raw.strip()
        if not pattern or pattern.startswith('#'):
            continue
        parts = pattern.split('/')
        if pattern.startswith('/') or '..' in parts or len(parts) < 2 or parts[-1] in ('*', ''):
            raise SystemExit(f'unsafe prune pattern: {pattern!r}')
        hits = sorted(glob.glob(str(tree / pattern)))
        if not hits:
            unmatched.append(pattern)
        for hit in map(Path, hits):
            if not str(hit.parent.resolve()).startswith(str(tree.resolve())):
                raise SystemExit(f'prune target escapes the tree: {hit}')
            freed += size_of(hit)
            if hit.is_symlink() or hit.is_file():
                hit.unlink()
            else:
                shutil.rmtree(hit)
    return freed, unmatched


def install_kernel(tree, kernel_out):
    release = next((kernel_out / 'modules/lib/modules').iterdir()).name
    base = tree / 'usr/lib/kernel-overlays/base/lib'
    if not base.is_dir():
        raise SystemExit('rootfs has no kernel-overlays/base: the initramfs requires it')
    for sub in ('modules', 'firmware'):
        shutil.rmtree(base / sub, ignore_errors=True)
    shutil.copytree(kernel_out / 'modules/lib/modules', base / 'modules', symlinks=True)
    shutil.copytree(kernel_out / 'firmware', base / 'firmware', symlinks=True)
    return release


def overlay(tree, source):
    """Copy files and symlinks from `source` over `tree`, keeping modes; directories are merged."""
    for src in sorted(source.rglob('*')):
        if '__pycache__' in src.relative_to(source).parts or src.suffix == '.pyc':
            continue                           # host bytecode written next to a script by a test run: never part of an image
        dst = tree / src.relative_to(source)
        if src.is_symlink():
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.symlink_to(os.readlink(src))
        elif src.is_dir():
            dst.mkdir(parents=True, exist_ok=True)
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.is_symlink():
                dst.unlink()                   # never write through a link that points outside the tree
            shutil.copyfile(src, dst)
            dst.chmod(0o755 if src.stat().st_mode & 0o111 else 0o644)     # git keeps the exec bit, not the umask


def install_preload(tree, preload):
    """The SIGILL-fallback library lives in the read-only image, with a checksum the session verifies."""
    import hashlib
    dest = tree / 'usr/lib/wispos'
    dest.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(preload, dest / 'libwispos-ldadd.so')
    (dest / 'libwispos-ldadd.so').chmod(0o755)
    digest = hashlib.sha256((dest / 'libwispos-ldadd.so').read_bytes()).hexdigest()
    (dest / 'libwispos-ldadd.sha256').write_text(f'{digest}  libwispos-ldadd.so\n')


def install_setup(tree, binary):
    """The first-run Wi-Fi screen and the badge it draws (loaded from a file so the artwork can be replaced)."""
    bin_dir, share = tree / 'usr/bin', tree / 'usr/share/wispos'
    share.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(binary, bin_dir / 'wisp-setup'); (bin_dir / 'wisp-setup').chmod(0o755)
    shutil.copyfile(HERE / 'setup/assets/wispos-badge.png', share / 'wispos-badge.png'); (share / 'wispos-badge.png').chmod(0o644)


UPDATE_SOURCE_FILE, UPDATE_KEY_FILE = HERE / 'steamos/update-source.json', HERE / 'steamos/update-key.pub'


def update_source(source_file=None, key_file=None):
    """(manifest url, public key file) to bake into the image, or (None, None). The release process can override both with WISP_UPDATE_MANIFEST_URL and WISP_UPDATE_PUBKEY_FILE (all
    or nothing); otherwise the committed os/steamos/update-source.json ({"manifest_url": "https://..."}) and os/steamos/update-key.pub are used when BOTH exist. The key alone, or
    the url alone, bakes nothing: the image then says "no update source is configured" and nothing is guessed. The url must be https."""
    source_file, key_file = source_file or UPDATE_SOURCE_FILE, key_file or UPDATE_KEY_FILE
    url, key = os.environ.get('WISP_UPDATE_MANIFEST_URL'), os.environ.get('WISP_UPDATE_PUBKEY_FILE')
    if bool(url) != bool(key): raise SystemExit('WISP_UPDATE_MANIFEST_URL and WISP_UPDATE_PUBKEY_FILE must be given together')
    if not url and Path(source_file).is_file() and Path(key_file).is_file():
        url, key = json.loads(Path(source_file).read_text()).get('manifest_url'), str(key_file)
    if not url: return None, None
    if not url.startswith('https://'): raise SystemExit('the update manifest url must be https')
    return url, key


def install_steamos_manager(tree, binary):
    """The daemon that answers Steam's SteamOS D-Bus calls and the helper it runs for updates (os/steamos/, docs/WISPOS-UPDATE-DESIGN.md); the units and bus files come with the
    overlay. The update source is baked in as update_source() decides."""
    shutil.copyfile(binary, tree / 'usr/bin/wisp-steamos-manager'); (tree / 'usr/bin/wisp-steamos-manager').chmod(0o755)
    lib = tree / 'usr/lib/wispos'; lib.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / 'steamos/wisp-update', lib / 'wisp-update'); (lib / 'wisp-update').chmod(0o755)
    url, key = update_source()
    if url:
        share = tree / 'usr/share/wispos'; share.mkdir(parents=True, exist_ok=True)
        (share / 'update.conf').write_text(json.dumps({'manifest_url': url}) + '\n'); shutil.copyfile(key, share / 'update-key.pub')


def install_pad(tree, binary):
    """The gamepad daemon for desktop mode (os/pad/); its unit comes with the overlay and is started by the switch to desktop."""
    shutil.copyfile(binary, tree / 'usr/bin/wisp-pad'); (tree / 'usr/bin/wisp-pad').chmod(0o755)


def install_dock(tree, binary):
    """The desktop-mode shell (os/dock/) and the wallpaper it sits on; its unit, the desktop entries and the scripts come with the overlay."""
    shutil.copyfile(binary, tree / 'usr/bin/wisp-dock'); (tree / 'usr/bin/wisp-dock').chmod(0o755)
    share = tree / 'usr/share/wispos'; share.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(HERE / 'dock/design/wallpaper.png', share / 'wallpaper.png'); (share / 'wallpaper.png').chmod(0o644)


def install_mime_database(tree):
    """The shared MIME database, generated from the tree's own /usr/share/mime/packages. The pinned ROCKNIX root ships the package XML but not the result, and gdk-pixbuf 2.42 needs the
    result to recognise an image: without it swaybg logged "Couldn't recognize the image file format" and the desktop wallpaper was black (device, 2026-10-15)."""
    mime = tree / 'usr/share/mime'
    if not list((mime / 'packages').glob('*.xml')):
        raise SystemExit(f'no MIME package XML under {mime}/packages to build the database from')
    tool = shutil.which('update-mime-database')
    if tool is None:
        raise SystemExit('update-mime-database (shared-mime-info) is needed on the build host to generate the image\'s MIME database')
    subprocess.run([tool, str(mime)], check=True, capture_output=True)
    if not (mime / 'mime.cache').is_file():
        raise SystemExit('update-mime-database ran but produced no mime.cache')


def install_bootscreen(tree, binary):
    """The Wayland phase of the boot screen (os/splash/wisp-bootscreen.c); its unit comes with the overlay."""
    shutil.copyfile(binary, tree / 'usr/bin/wisp-bootscreen'); (tree / 'usr/bin/wisp-bootscreen').chmod(0o755)


def install_ui_reclaim(tree, binary):
    """The Steam UI's memory ceiling daemon (os/reclaim/); its unit comes with the overlay."""
    shutil.copyfile(binary, tree / 'usr/bin/wisp-ui-reclaim'); (tree / 'usr/bin/wisp-ui-reclaim').chmod(0o755)


# Libraries Steam's client needs that this root does not have, from the pinned Ubuntu packages in steam/native-libs.lock.json.
# Found by asking what the UPDATED client (the one that actually runs; Steam updates itself on its first start) cannot load:
# steamui.so needs libva (-> "Fatal error: Failed to load steamui.so", Steam exits 255 in a loop), steamwebhelper needs libibus,
# libcurl needs libbrotli and libssh2, libavcodec/libavutil need libva and libvdpau, libpulsecommon needs libasyncns. The other 24
# packages in that lock were for the old ROCKNIX root and are not needed here.
STEAM_LIB_PACKAGES = ('libibus-1.0-5', 'libasyncns0', 'libbrotli1', 'libssh2-1t64', 'libva2', 'libvdpau1')


# sudo and its libraries (os/sudo.lock.json, noble arm64, hash-pinned): the device
# owner's way to act as root. The image carries no PAM, polkit or sudo of its
# own, and `wheel` membership alone grants nothing; this installs the standard
# tool instead of a bespoke helper. `wisp` runs anything with no password
# (sudoers.d drop-in, SteamOS-deck style); root itself stays key-only, so the
# release gate on a passwordless root is untouched.
SUDOERS_WISP = 'wisp ALL=(ALL:ALL) NOPASSWD: ALL\n'
# The package's own /etc/pam.d/sudo names Debian's common-* stacks (libpam-runtime
# config the image does not ship), so it is replaced with a permit stack: the
# sudoers rule above is the whole policy, PAM authenticates nothing.
PAM_SUDO = ('auth sufficient pam_permit.so\n'
            'account sufficient pam_permit.so\n'
            'password sufficient pam_permit.so\n'
            'session required pam_permit.so\n')
SUDO_SKIP_PREFIXES = ('usr/share/doc/', 'usr/share/man/')
# Exact paths skipped: the package masks its own unit with a symlink to
# /dev/null (standard Debian practice); the safety extractor refuses absolute
# links, and dropping the mask changes nothing because no sudo.service file
# ships either way.
SUDO_SKIP_EXACT = ('usr/lib/systemd/system/sudo.service',)
# Only these runtime sonames come out of the library packages, flattened into
# usr/lib (the tree's own convention, a real loader path, and a directory the
# rootfs gate already searches). Everything else a lib deb carries (unused PAM
# modules, helper libs nothing references) stays out.
SUDO_LIB_SONAMES = ('libpam.so.0', 'libaudit.so.1', 'libselinux.so.1',
                    'libapparmor.so.1', 'libcap-ng.so.0', 'libpcre2-8.so.0')
# The only PAM module the permit stack names; it must stay at the module path.
SUDO_PAM_MODULES = ('pam_permit.so',)


def sudo_dest(name, rel):
    """Where one archive member lands in the tree, or None to leave it out."""
    s = str(rel)
    if any(s.startswith(p) for p in SUDO_SKIP_PREFIXES):
        return None
    if s in SUDO_SKIP_EXACT:
        return None
    if name == 'sudo':
        if '/security/' in s and Path(s).name not in SUDO_PAM_MODULES:
            return None
        return rel
    base = Path(s).name
    if '/security/' in s and base in SUDO_PAM_MODULES:
        return rel
    if any(base == son or base.startswith(son + '.') for son in SUDO_LIB_SONAMES):
        return Path('usr/lib') / base
    return None


def drop_sudo_masks(data):
    """The data.tar of a package minus its unit-mask symlinks (SUDO_SKIP_EXACT):
    anything else, safe or not, passes through untouched for the strict
    extractor to judge. A skip entry pointing anywhere but /dev/null is kept,
    so a hijacked mask still raises."""
    import io
    import tarfile
    src = tarfile.open(fileobj=io.BytesIO(data), mode='r:*')
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode='w') as dst:
        for member in src.getmembers():
            name = member.name.removeprefix('./')
            if (member.issym() and name in SUDO_SKIP_EXACT
                    and member.linkname == '/dev/null'):
                continue
            f = src.extractfile(member) if member.isfile() else None
            dst.addfile(member, f)
    return out.getvalue()


def install_sudo(tree, debs_dir, lock_path=None):
    """Unpack the pinned sudo packages into the tree and wire the wisp rule."""
    import importlib.util
    tree = Path(tree)
    lock = Path(lock_path) if lock_path else HERE / 'sudo.lock.json'
    packages = {x['package']: x for x in json.loads(lock.read_text())['packages']}
    spec = importlib.util.spec_from_file_location('native_libs', HERE.parent / 'steam' / 'native-libs.py')
    native_libs = importlib.util.module_from_spec(spec); spec.loader.exec_module(native_libs)
    for name, entry in packages.items():
        deb = Path(debs_dir) / entry['url'].rsplit('/', 1)[1]
        if hashlib.sha256(deb.read_bytes()).hexdigest() != entry['sha256']:
            raise SystemExit(f'{name} package does not match the lock')
        if subprocess.run(['dpkg-deb', '-f', str(deb), 'Architecture'], capture_output=True, text=True, check=True).stdout.strip() != 'arm64':
            raise SystemExit(f'{name} package is not arm64')
        raw = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(deb)], capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as scratch:
            scratch = Path(scratch); native_libs.extract(scratch, drop_sudo_masks(raw))
            for src in sorted(scratch.rglob('*')):
                rel = src.relative_to(scratch)
                if src.is_dir():
                    continue
                dest_rel = sudo_dest(name, rel)
                if dest_rel is None:
                    continue
                dest = tree / dest_rel
                if src.is_symlink():
                    if dest.is_symlink() or dest.exists():
                        dest.unlink()
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.symlink_to(os.readlink(src))
                elif src.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                elif src.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest); shutil.copymode(src, dest)
    sudoers_d = tree / 'etc/sudoers.d'; sudoers_d.mkdir(parents=True, exist_ok=True)
    (sudoers_d / 'wisp').write_text(SUDOERS_WISP); (sudoers_d / 'wisp').chmod(0o440)
    pam_d = tree / 'etc/pam.d'; pam_d.mkdir(parents=True, exist_ok=True)
    (pam_d / 'sudo').write_text(PAM_SUDO); (pam_d / 'sudo').chmod(0o644)
    # The login shell uses the separate sudo-i service (WispOS-Project/wispos#23):
    # the package's retained stack (pam_limits, pam_env, Debian common-*) is
    # not installed, so it gets the same permit-only policy, not the package file.
    (pam_d / 'sudo-i').write_text(PAM_SUDO); (pam_d / 'sudo-i').chmod(0o644)
    (tree / 'usr/bin/sudo').chmod(0o4755)


def install_usb_host_bench(tree):
    """The record that USB host mode passed its bench tests (docs/WISPOS-OS-DESIGN.md, spike S-USB). Operator rule, 2026-10-11: the host toggle ships in a release only after the bench
    test (an OTG adapter, external power appearing, 10 plug cycles, a USB-C meter) passed and its result is written in that document. WISP_USB_HOST_BENCH names the record (one line, e.g.
    'T1-T4 passed 2026-10-20, docs/WISPOS-OS-DESIGN.md "Phase 2 results"'). WITHOUT it host mode is SWITCHED OFF in the image (operator, 2026-10-12: no bench meter, no host mode in 0.1.0):
    the marker usr/share/wispos/usb-host-disabled makes `wisp-usb host` refuse; the code, the quick-settings segment and the kernel gate (peripheral-only by default) stay."""
    ref = os.environ.get('WISP_USB_HOST_BENCH', '').strip()
    share = tree / 'usr/share/wispos'; share.mkdir(parents=True, exist_ok=True)
    if not ref:
        (share / 'usb-host-disabled').write_text('host mode is not part of this release: no bench result is recorded (docs/WISPOS-OS-DESIGN.md, spike S-USB)\n'); return
    (share / 'usb-host-disabled').unlink(missing_ok=True)
    (share / 'usb-host-bench').write_text(ref + '\n')


def install_steam_installer(tree, debs_dir):
    """What the first-run Steam download needs, none of it Valve's: the pinned locks (client and compatibility tool), the one safe extractor (rootfs/usr/lib/wispos/
    steam-install imports it) and the libraries above, baked into usr/lib/wispos/lib (the session puts that on LD_LIBRARY_PATH).
    The Steam client itself is downloaded on the device."""
    import importlib.util
    lib, share = tree / 'usr/lib/wispos', tree / 'usr/share/wispos'
    share.mkdir(parents=True, exist_ok=True); lib.mkdir(parents=True, exist_ok=True)
    steam = HERE.parent / 'steam'
    shutil.copyfile(steam / 'native-client.py', lib / 'native_client.py'); (lib / 'native_client.py').chmod(0o644)
    shutil.copyfile(steam / 'native-client.lock.json', share / 'native-client.lock.json'); (share / 'native-client.lock.json').chmod(0o644)
    # The compatibility tool's pin (steam-install downloads it in the same first run; the tool is not in the image).
    shutil.copyfile(steam / 'compat-tool.lock.json', share / 'compat-tool.lock.json'); (share / 'compat-tool.lock.json').chmod(0o644)
    # Valve's wow64.dll: the pin of the one file wisp-wow64-valve may copy out of Valve's Proton app (known-good hashes only).
    shutil.copyfile(steam / 'valve-wow64.lock.json', share / 'valve-wow64.lock.json'); (share / 'valve-wow64.lock.json').chmod(0o644)
    packages = {x['package']: x for x in json.loads((steam / 'native-libs.lock.json').read_text())['packages']}
    spec = importlib.util.spec_from_file_location('native_libs', steam / 'native-libs.py')
    native_libs = importlib.util.module_from_spec(spec); spec.loader.exec_module(native_libs)
    (lib / 'lib').mkdir(exist_ok=True)
    for name in STEAM_LIB_PACKAGES:
        entry = packages[name]
        deb = Path(debs_dir) / entry['url'].rsplit('/', 1)[1]
        if hashlib.sha256(deb.read_bytes()).hexdigest() != entry['sha256']:
            raise SystemExit(f'{name} package does not match the lock')
        if subprocess.run(['dpkg-deb', '-f', str(deb), 'Architecture'], capture_output=True, text=True, check=True).stdout.strip() != 'arm64':
            raise SystemExit(f'{name} package is not arm64')
        data = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(deb)], capture_output=True, check=True).stdout
        with tempfile.TemporaryDirectory(dir=os.environ.get('TMPDIR')) as scratch:
            scratch = Path(scratch); native_libs.extract(scratch, data)
            found = sorted((scratch / 'usr/lib/aarch64-linux-gnu').glob('lib*.so*'))
            if not any(f.is_file() and not f.is_symlink() for f in found):
                raise SystemExit(f'no libraries found in {name}')
            for f in found:
                target = lib / 'lib' / f.name
                if f.is_symlink():
                    target.symlink_to(os.readlink(f))
                else:
                    shutil.copyfile(f, target); target.chmod(0o755)


def install_sway_default_config(tree):
    """sway.service starts before ROCKNIX's autostart writes /storage/.config/sway/config (111-sway-init does, on every boot), and
    sway's own start helper (usr/lib/sway/sway-config) copies a default usr/share/sway/config when the user's is missing, which
    ROCKNIX does not ship. On the first start after a fresh flash sway therefore ran with no config and crashed (SIGSEGV, status 139,
    seen on the handheld) until systemd restarted it. The default is the kiosk config, the same base 111-sway-init uses."""
    kiosk = tree / 'usr/share/sway/config.kiosk'
    shutil.copyfile(kiosk, tree / 'usr/share/sway/config'); (tree / 'usr/share/sway/config').chmod(0o644)


def install_dev(tree):
    """Development builds only: the announcer that tells the developer's machine where the device is, the flight recorder, the memory watchdog and the
    pad-triggered panel test (rootfs-dev/), plus the on-device diagnostic tools of os/dev/diag under /usr/lib/wispos-dev (the tools expect to live at
    /storage/wispos/dev: tmpfiles.d links that to /usr/lib/wispos-dev on a card that has none)."""
    overlay(tree, HERE / 'rootfs-dev')
    root = tree / 'usr/lib/wispos-dev'; diag = root / 'diag'; diag.mkdir(parents=True, exist_ok=True)
    for tool in sorted((HERE / 'dev/diag').iterdir()):
        if tool.is_file() and tool.suffix in ('.py', '.sh'):
            shutil.copyfile(tool, diag / tool.name); (diag / tool.name).chmod(0o755)
    for name in ('kill-game.sh', 'gamelaunch.sh', 'replay_app.py'):                       # the tools call these as $W/<name>
        (root / name).symlink_to(f'diag/{name}')
    (root / 'wisp-dev-pad').symlink_to('/usr/bin/wisp-dev-pad')


def merge_sshd(tree):
    ours = tree / 'etc/ssh/sshd_config.wisp'
    config = tree / 'etc/ssh/sshd_config'
    config.write_text(ours.read_text() + '\n' + config.read_text())
    ours.unlink()


# Keys ROCKNIX's os-release carries for its upstream project and that nothing in the image reads (checked 2026-10-11 against every script, unit and the initramfs).
OS_RELEASE_DROPPED = {'GIT_ORGANIZATION', 'GIT_REPO'}


def build_id():
    """The image's build id, comparable like a version number ("20261012.1"; the update scheme compares them). In order: WISP_BUILD_ID (the release process), the lockfile's
    build_id (the release candidate being built), else the build date (SOURCE_DATE_EPOCH, which pins reproducibility) with sequence 0."""
    given = os.environ.get('WISP_BUILD_ID')
    if given:
        if not re.fullmatch(r'\d{8}\.\d+', given): raise SystemExit(f'WISP_BUILD_ID must look like 20261012.1, not {given!r}')
        return given
    locked = lockfile().get('build_id')
    if locked: return locked
    return time.strftime('%Y%m%d', time.gmtime(int(os.environ.get('SOURCE_DATE_EPOCH', '0')))) + '.0'


def rebrand(tree):
    """WispOS's identity in /etc/os-release: the standard keys (NAME, ID, PRETTY_NAME, VERSION_ID, BUILD_ID) are ours, so the update interface and `lsb_release` agree on one
    version and one build id; ROCKNIX's own keys that its remaining scripts read (OS_NAME, OS_VERSION, HW_*) stay, relabelled; its upstream coordinates go."""
    path = tree / 'etc/os-release'
    version = lockfile()['version']
    standard = {'NAME': 'WispOS', 'ID': 'wispos', 'PRETTY_NAME': f'WispOS {version}', 'VERSION_ID': version, 'BUILD_ID': build_id()}
    out = []
    for line in path.read_text().splitlines():
        key = line.split('=', 1)[0]
        if key in OS_RELEASE_DROPPED or key in standard: continue
        out.append(f'{key}="{OS_RELEASE_OVERRIDES[key]}"' if key in OS_RELEASE_OVERRIDES else line)
    out += [f'{k}="{v}"' for k, v in standard.items()]
    out.append(f'WISPOS_VERSION="{version}"')
    path.write_text('\n'.join(out) + '\n')


def install_official_fex(tree, deb, lock_path=None):
    """Bake the FEX project's own Windows modules into the image, unmodified, for wisp-fex-official to put into the compatibility tool.
    Source of truth is steam/official-fex.lock.json (URL, sha256 of the package and of each member). Only the pinned members are read from the package: nothing else in it is
    extracted, and nothing is built by us. Result: usr/share/wispos/fex-official/{the four files, copyright, official-fex.lock.json}."""
    import io, tarfile
    lock_path = Path(lock_path or HERE.parent / 'steam' / 'official-fex.lock.json')
    lock = json.loads(lock_path.read_text())
    deb = Path(deb)
    if hashlib.sha256(deb.read_bytes()).hexdigest() != lock['sha256']:
        raise SystemExit('the fex-emu-wine package does not match steam/official-fex.lock.json')
    meta = {k: subprocess.run(['dpkg-deb', '-f', str(deb), k], capture_output=True, text=True, check=True).stdout.strip() for k in ('Package', 'Version', 'Architecture')}
    if meta != {'Package': lock['package'], 'Version': lock['version'], 'Architecture': lock['architecture']}:
        raise SystemExit(f'the fex-emu-wine package is {meta}, the lock says {lock["package"]} {lock["version"]} {lock["architecture"]}')
    data = subprocess.run(['dpkg-deb', '--fsys-tarfile', str(deb)], capture_output=True, check=True).stdout
    dest = tree / 'usr/share/wispos/fex-official'
    dest.mkdir(parents=True, exist_ok=True)
    wanted = {f['member']: f['sha256'] for f in lock['files']}; wanted[lock['license_member']] = lock['license_sha256']
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for member in archive.getmembers():
            name = member.name[2:] if member.name.startswith('./') else member.name
            if name not in wanted: continue
            if not member.isfile(): raise SystemExit(f'{name} in the fex-emu-wine package is not a regular file')
            blob = archive.extractfile(member).read()
            if hashlib.sha256(blob).hexdigest() != wanted[name]:
                raise SystemExit(f'{name} does not match steam/official-fex.lock.json')
            target = dest / name.rsplit('/', 1)[1]
            target.write_bytes(blob); target.chmod(0o644); seen.add(name)
    if seen != set(wanted):
        raise SystemExit(f'the fex-emu-wine package lacks {sorted(set(wanted) - seen)}')
    shutil.copyfile(lock_path, dest / 'official-fex.lock.json'); (dest / 'official-fex.lock.json').chmod(0o644)
    print('official FEX modules:', lock['package'], lock['version'], 'sha256', lock['sha256'][:16])


def main(tree, kernel_out, mode='full'):
    tree, kernel_out = Path(tree), Path(kernel_out)
    if mode == 'modules-only':
        # Stage-isolation variant: ROCKNIX's userland untouched except for the kernel modules, which
        # must match the kernel they run under (symbol checksums differ between compiler versions).
        print('kernel release', install_kernel(tree, kernel_out))
        return
    freed, unmatched = prune(tree, HERE / 'rootfs-prune.txt')
    print(f'pruned {freed / 1e6:.0f} MB')
    for pattern in unmatched:
        print(f'  prune pattern matched nothing: {pattern}')
    print('kernel release', install_kernel(tree, kernel_out))
    overlay(tree, HERE / 'rootfs')
    install_preload(tree, os.environ['WISP_PRELOAD'])
    install_setup(tree, os.environ['WISP_SETUP_BIN'])
    install_steamos_manager(tree, os.environ['WISP_STEAMOS_MANAGER_BIN'])
    install_ui_reclaim(tree, os.environ['WISP_UI_RECLAIM_BIN'])
    install_pad(tree, os.environ['WISP_PAD_BIN'])
    install_dock(tree, os.environ['WISP_DOCK_BIN'])
    install_mime_database(tree)
    install_bootscreen(tree, os.environ['WISP_BOOTSCREEN_BIN'])
    install_usb_host_bench(tree)
    install_steam_installer(tree, os.environ['WISP_STEAM_DEBS'])
    install_sudo(tree, os.environ['WISP_SUDO_DEBS'])
    install_official_fex(tree, os.environ['WISP_OFFICIAL_FEX_DEB'])
    install_sway_default_config(tree)
    if os.environ.get('WISP_DEV') == '1':
        install_dev(tree)
    merge_sshd(tree)
    accounts.apply(tree)
    default = tree / 'usr/lib/systemd/system/default.target'
    if default.is_symlink() or default.exists():
        default.unlink()
    default.symlink_to('wispos.target')
    rebrand(tree)


if __name__ == '__main__':
    if len(sys.argv) not in (3, 4):
        raise SystemExit(__doc__)
    main(*sys.argv[1:])
