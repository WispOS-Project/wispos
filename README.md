<p align="center">
  <img src="docs/brand/wispos-device-spotlight.png" alt="WispOS running Steam's gamepad UI on the Anbernic RG34XX-SP" width="560">
</p>

# WispOS

> **Project Status: Proof of Concept**  
> WispOS is currently in the **proof of concept** stage. It is an experimental appliance prototype demonstrating Steam's gamepad UI, compatibility tools, and game execution on the Anbernic RG34XX-SP. While core boot, setup, desktop mode, signed OTA updates, and game launches have been proven on physical hardware, this is an active prototype and not a finished consumer release.

A single-purpose operating system for the **Anbernic RG34XX-SP** handheld (Allwinner H700, four Cortex-A53 cores, 1 GiB of RAM, a 720x480 panel): it boots into Steam's gamepad UI
and then into the games you launch from your library, and does nothing else. Steam and its games run through the stock, unmodified compatibility layers (FEX-Emu and Proton for
ARM64). The goal is *general* compatibility of older Steam titles (1990s first, then pre-2005) through a strong core system, never fixes for one particular game.

## Custom System Implementations

Around Steam's Big Picture Mode and SteamOS gamepad UI, WispOS provides custom, lightweight core implementations engineered for the tight memory and display constraints of the Allwinner H700:

| First-Time Setup (`wisp-setup`) | Desktop Shell (`wisp-dock` & `wisp-pad`) |
| :---: | :---: |
| <img src="docs/brand/wispos-setup-screen.jpg" width="400" alt="First-Time Setup on RG34XX-SP"> | <img src="docs/brand/wispos-desktop-screen.png" width="400" alt="Desktop Mode on RG34XX-SP"> |
| **First-Run Provisioning**: Gamepad-driven Wi-Fi connection, network verification, and live-progress Steam package downloads. | **Dedicated Desktop Mode**: Custom layer-shell dock, application launcher, virtual gamepad pointer/keyboard, and seamless switching to/from Steam Game Mode. |

## What is in this repository
| Path | What it is |
| --- | --- |
| `os/` | The OS image build: kernel/U-Boot/initramfs scripts (`os/boot/`), the root file system overlay and its build (`os/rootfs/`, `os/build-rootfs.sh`, `os/stage_rootfs.py`), the card image (`os/build-image.sh`), the first-time setup screen (`os/setup/`), the boot splash (`os/splash/`), the shared UI tokens (`os/ui/`), the SteamOS-compatibility daemon and update helper (`os/steamos/`), the Steam UI memory ceiling (`os/reclaim/`), the release tool (`os/make-release.py`) and development tooling (`os/dev/`) |
| `steam/` | Pinned inputs the build verifies by hash (lock files) and the small helpers around Steam's native ARM64 client |
| `docs/brand/` | The logo, brand assets, and device hardware showcase captures |
| `CHANGELOG.md` | The change ledger |

The change ledger mentions scripts, layers and documents from earlier stages of the project, or the author's own workflow tooling, that are not part of this repository; those references are history, not missing files.

## Building and testing
The build needs `aarch64-linux-gnu-gcc`, `mksquashfs`/`unsquashfs`, `fakeroot`, `mke2fs`, `openssl`, `python3` and `curl`, and several gigabytes of free space; large inputs are
downloaded and checked against the hashes in `steam/*.lock.json` and in the scripts. By default everything is written under a work directory on a large disk
(`/mnt/hdd/wispos-staging/wispos-os-20261007` is the author's; every script takes another directory as its first argument). `os/build-all.sh` builds everything in order; `os/repro-check.sh` checks that the build is reproducible.

The tests need no hardware: `for t in os/test_*.py steam/test_*.py; do python3 -I $t; done`. Several of them build and run small programs against fake `/proc`, cgroup and D-Bus
setups, and `os/qemu-test.sh` boots the built image's userspace in QEMU.

## Updates
Installed systems update from signed releases of this repository: `os/make-release.py` builds the assets and the ed25519-signed manifest; `os/steamos/update-key.pub` is the key
every image trusts.

## Licence
MIT, see `LICENSE` (copyright 2026 Daxiongmao87). Third-party material keeps its own terms: the SteamOS interface definitions in `os/steamos/interfaces/` are MIT-licensed by their
authors (the notices are in the files), and the OS image built from this repository contains many other projects under their own licences.

## Development and releases

See [CONTRIBUTING.md](CONTRIBUTING.md) for the direct branch/PR workflow and both
image variants, and [the release guide](docs/RELEASING.md) for candidate verification
and signed dev/stable updates. All product and developer-tool changes belong here.
