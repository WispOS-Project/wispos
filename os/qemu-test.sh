#!/bin/bash
# Boot the built WispOS image headless in QEMU and check what a boot should produce.
#
#   os/qemu-test.sh [WORKDIR]
#
# Uses the test-only kernel from boot/build-qemu-kernel.sh with the real initramfs, SYSTEM image and
# partitions (the image is opened with snapshot=on, so it is never modified). Waits for SSH on
# localhost:2229, logs in with the baked SSH key as root and with the default wisp password, and
# prints what it finds. Exit status is non-zero if any check fails. This is emulation of a generic
# arm64 machine: it proves the userspace boot flow, not the H700 hardware.
set -uo pipefail
WORK=${1:-/mnt/hdd/wispos-staging/wispos-os-20261007}
case "$WORK" in /mnt/hdd/*) ;; *) echo "work directory must be on the HDD" >&2; exit 2 ;; esac
PORT=${WISP_QEMU_SSH_PORT:-2229}      # host port forwarded to the guest's sshd (2222 is held by other work on this host; seen 2026-10-12 and 2026-10-16)
HERE=$(cd "$(dirname "$0")" && pwd); IMG=$WORK/out/qemu/test-image.img; KERNEL=$WORK/out/qemu/Image; D=$WORK/qemu-boot; LOG=$D/serial.log
[ -f "$KERNEL" ] || { echo "need $KERNEL (os/boot/build-qemu-kernel.sh)" >&2; exit 1; }
mkdir -p "$D" "$WORK/tmp"; export TMPDIR=$WORK/tmp; : >"$LOG"
printf '#!/bin/sh\necho wisp\n' >"$D/askpass.sh"; chmod +x "$D/askpass.sh"
# The shipped image carries no keys. The test builds its own copy of the image with a throwaway key
# generated here, so no real credential is ever involved. It pins the RELEASE rootfs (WISP_SYSTEM): this test is about what ships.
KEY=$D/test-key; rm -f "$KEY" "$KEY.pub"; ssh-keygen -q -t ed25519 -N '' -C wispos-qemu-test -f "$KEY"
WISP_DEV=1 WISP_SYSTEM=$WORK/out/SYSTEM WISP_SYSTEM_MD5=$WORK/out/SYSTEM.md5 WISP_SSH_KEY=$KEY.pub WISP_IMAGE_NAME=qemu/test-image.img bash "$HERE/build-image.sh" "$WORK" >"$D/test-image-build.log" 2>&1 \
    || { echo "could not build the test image"; tail -5 "$D/test-image-build.log"; exit 1; }
SSHO=(-p "$PORT" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8)
as_root() { ssh "${SSHO[@]}" -i "$KEY" -o IdentitiesOnly=yes -o PasswordAuthentication=no -o BatchMode=yes root@127.0.0.1 "$@"; }
as_wisp() { SSH_ASKPASS="$D/askpass.sh" SSH_ASKPASS_REQUIRE=force ssh "${SSHO[@]}" -o PubkeyAuthentication=no wisp@127.0.0.1 "$@"; }
timeout 1500 qemu-system-aarch64 -M virt -cpu cortex-a53 -smp 4 -m 1024 -display none -monitor none -serial "file:$LOG" \
    -kernel "$KERNEL" -append "boot=LABEL=ROCKNIX disk=LABEL=STORAGE console=ttyAMA0 loglevel=6" \
    -drive "file=$IMG,format=raw,if=none,id=d0,snapshot=on" -device virtio-blk-pci,drive=d0 \
    -netdev user,id=n0,hostfwd=tcp:127.0.0.1:$PORT-:22 -device virtio-net-pci,netdev=n0 >"$D/qemu.out" 2>&1 &
QPID=$!; trap 'kill $QPID 2>/dev/null' EXIT
fail=0; check() { if eval "$2" >/dev/null 2>&1; then echo "PASS  $1"; else echo "FAIL  $1"; fail=1; fi; }
up=no; for _ in $(seq 1 90); do as_root true 2>/dev/null && { up=yes; break; }; sleep 10; done
check "SSH as root with the baked key comes up (initramfs, resize reboot, systemd, sshd)" "[ $up = yes ]"
[ $up = yes ] || { tail -20 "$LOG"; exit 1; }
for _ in $(seq 1 60); do [ "$(as_root systemctl is-system-running 2>/dev/null)" != starting ] && break; sleep 5; done
HOSTNAME_=$(as_root hostname); DEFAULT=$(as_root readlink /usr/lib/systemd/system/default.target)
STATE=$(as_root systemctl is-system-running); FAILED=$(as_root "systemctl --failed --no-legend --plain | awk '{print \$1}'" | tr '\n' ' ')
ACTIVE=$(as_root 'systemctl is-active wisp-firstboot sshd seatd dbus NetworkManager iwd rocknix-autostart' | sort | uniq -c | tr -s ' ' | tr '\n' ';')
SESSION=$(as_root 'systemctl show -p ActiveState,Result wisp-steam' | tr '\n' ' ')
IP=$(as_root "ip -4 -o addr show eth0 | awk '{print \$4}'")
echo "    hostname=$HOSTNAME_ default.target->$DEFAULT state=$STATE failed=[$FAILED] ip=$IP"
echo "    services: $ACTIVE   session: $SESSION"
check "default target is wispos.target" "[ '$DEFAULT' = wispos.target ]"
check "base services active (firstboot sshd seatd dbus NetworkManager iwd rocknix-autostart); sway needs a GPU the VM lacks" "[ '$ACTIVE' = ' 7 active;' ]"
# The VM has no battery and no GPU: batteryplus cannot run, and sway cannot start, so wisp-setup (which needs sway) restarts every 3 s and is "failed" at some instants and "activating"
# at others: a one-shot sample of `systemctl --failed` must not count the units that need a display. Anything ELSE failing is a finding.
check "only display- and battery-less units failed (batteryplus, sway, wisp-setup, wisp-bootscreen: the VM has no battery and no GPU)" "[ -z '$(echo "$FAILED" | sed -e 's/batteryplus.service//' -e 's/wisp-setup.service//' -e 's/wisp-bootscreen.service//' -e 's/sway.service//' | tr -d ' ')' ]"
check "network came up by DHCP" "[ -n '$IP' ]"
check "session unit is skipped, not failed, without a Steam runtime" "[[ '$SESSION' == *Result=success*ActiveState=inactive* ]]"
check "storage holds the session directories (tmpfiles)" "as_root 'test -d /storage/wispos/logs && test -d /storage/home/wisp'"
check "wisp can log in over SSH with the default password" "as_wisp true"
check "wisp has the groups the session and tools need" "as_wisp 'id -nG' | grep -qw video && as_wisp 'id -nG' | grep -qw input && as_wisp 'id -nG' | grep -qw systemd-journal"
check "wisp can read the journal" "as_wisp 'journalctl -b --no-pager -n 1'"
check "root has no password: password login as root is refused" "! SSH_ASKPASS='$D/askpass.sh' SSH_ASKPASS_REQUIRE=force ssh ${SSHO[*]} -o PubkeyAuthentication=no root@127.0.0.1 true"
check "telemetry and the emulator frontend are gone" "! as_root 'ls /usr/bin/rocknix-report-stats /usr/bin/emulationstation /usr/lib/libretro'"
check "the system is the read-only squashfs" "as_wisp mount | grep -q 'on / type squashfs'"
# --- SSH is off by default and persistent (operator, 2026-10-11): this test image is a dev image, so its `ssh` marker file turned it on at first boot, once ----------------------------
check "SSH was seeded on by the boot partition's ssh file (first boot) and the unit carries the persistent-flag condition" \
    "as_root 'test -e /storage/.config/wispos/ssh-enabled && test -e /storage/.config/wispos/ssh-seeded && grep -q ConditionPathExists=/storage/.config/wispos/ssh-enabled /usr/lib/systemd/system/sshd.service'"
SSH_STATUS=$(as_root 'wisp-ssh status'; echo "exit=$?"); echo "    wisp-ssh status -> $(echo "$SSH_STATUS" | tr '\n' '|')"      # captured whole: `grep -q` in a pipe closes it early and pipefail would fail the check
check "wisp-ssh status reports on (exit 0) with the host name and an address" "[[ \"\$SSH_STATUS\" == *'ssh: on'* && \"\$SSH_STATUS\" == *'address: '* && \"\$SSH_STATUS\" == *'exit=0' ]]"
# --- The USB-C port tool (docs/WISPOS-OS-DESIGN.md): this VM has no USB controller and its kernel has no host gate, so the tool must say so, refuse, and change nothing -------------
USB_STATUS=$(as_root 'wisp-usb status'; echo "exit=$?")
check "wisp-usb status: off, and neither host mode (switched off in this release) nor computer mode (no controller) is offered here" \
    "[[ \"\$USB_STATUS\" == *'usb: off'* && \"\$USB_STATUS\" == *'host: unavailable: Host mode is not part of this release.'* && \"\$USB_STATUS\" == *'computer: unavailable: This device has no USB device controller'* && \"\$USB_STATUS\" == *'exit=0' ]]"
USB_HOST_RC=$(as_root 'wisp-usb host >/dev/null 2>&1; echo $?'); USB_OFF_RC=$(as_root 'wisp-usb off >/dev/null 2>&1; echo $?')
check "wisp-usb host refuses, host mode being switched off in this release (exit 4) and starts no watcher; wisp-usb off succeeds" \
    "[ '$USB_HOST_RC' = 4 ] && [ '$USB_OFF_RC' = 0 ] && [ \"\$(as_root 'systemctl is-active wisp-usb-watch.service')\" != active ]"
check "ROCKNIX's usbgadget and MTP daemon are gone (nothing else may switch the port role)" "! as_root 'ls /usr/bin/usbgadget /usr/sbin/umtprd /etc/umtprd'"
# --- The SteamOS contract daemon, started by the buses (docs/WISPOS-STEAMOS-CONTRACT.md) -------------
# Atomupd1 is asked for on the SYSTEM bus; nothing enables the unit, the bus activates it. SteamOSManager1 is asked for on Steam's SESSION bus: a throwaway session bus here.
check "Atomupd1: the system bus starts wisp-steamos-manager and it answers with the image's own version" \
    "as_root 'busctl --system get-property com.steampowered.Atomupd1 /com/steampowered/Atomupd1 com.steampowered.Atomupd1 CurrentVersion' | grep -q '^s \"'"
check "the unit is active, started by the bus and not enabled in any target" \
    "[ \"\$(as_root 'systemctl is-active wisp-steamos-manager.service')\" = active ] && [ \"\$(as_root 'systemctl is-enabled wisp-steamos-manager.service 2>&1')\" != enabled ]"
check "Steam's start-up call (Atomupd1.DisableHttpProxy) succeeds" "as_root 'busctl --system call com.steampowered.Atomupd1 /com/steampowered/Atomupd1 com.steampowered.Atomupd1 DisableHttpProxy'"
update_check_output() { as_root 'busctl --system call com.steampowered.Atomupd1 /com/steampowered/Atomupd1 com.steampowered.Atomupd1 CheckForUpdates a{sv} 0 2>&1' || true; }     # busctl exits 1 on the expected error: pipefail must not see it
# A DEFINITE answer: a successful reply (busctl prints `a{sa{sv}}a{sa{sv}} N M`: nothing newer, or entries when an update exists) or one of the plain errors for no release published,
# no network or no source. Anything else (access denied, a timeout, no output) is not an answer. The three plain errors predate the first published release (issue #35).
update_check_ok() {
    case "$1" in
        *'no update has been published yet'*|*'cannot reach the update server'*|*'no update source is configured'*) return 0 ;;
        'a{sa{sv}}a{sa{sv}} '[0-9]*) return 0 ;;
    esac
    return 1
}
check "an update check reaches the helper and gets a definite answer (a successful reply, or no release published, no network, or no source: nothing is guessed)" "update_check_ok \"\$(update_check_output)\""
check "SteamOSManager1: the session bus starts the manager role inside the session and DeviceModel answers" \
    "as_root 'dbus-run-session -- busctl --user get-property com.steampowered.SteamOSManager1 /com/steampowered/SteamOSManager1 com.steampowered.SteamOSManager1.Manager2 DeviceModel' | grep -q '^(ss)'"
session_role_running() { as_root "ps w | grep -q -- '[-]-manager session'"; }     # the system-bus instance is the same program: tell them apart by the role on its command line
check "that session-activated process ended with its session bus (nothing left behind)" "sleep 2; ! session_role_running"
# --- The Steam UI's memory ceiling and freeze (wisp-ui-reclaim --freeze): enabled by default in the release image (0.1.0 plan); it must be running on the real cgroup v2, have created the UI
# cgroup at the idle ceiling, lift the ceiling when stopped, and honour the off-switch file. (No sway here: it counts the UI as visible and keeps retrying, never freezing blind.) ---
check "wisp-ui-reclaim is installed, enabled, running, and started with --follow-focus (nothing is frozen: operator decision 2026-10-14)" \
    "as_root 'test -x /usr/bin/wisp-ui-reclaim' && as_root 'systemctl show -p WantedBy wisp-ui-reclaim.service' | grep -q wispos.target && [ \"\$(as_root 'systemctl is-active wisp-ui-reclaim.service')\" = active ] && as_root 'systemctl show -p ExecStart wisp-ui-reclaim.service' | grep -q -- '--follow-focus' && ! as_root 'systemctl show -p ExecStart wisp-ui-reclaim.service' | grep -q -- '--freeze'"
check "the priority daemon and the taskset shim are in the image, and the shim gives the web helper cores 0-3 on the device's own userland (Valve's mask would give 2-3)" \
    "as_root 'test -x /usr/bin/wisp-ui-priority && test -x /usr/lib/wispos/bin/taskset && grep -q wisp-ui-priority.service /usr/lib/systemd/system/wisp-steam.service' && [ \"\$(as_root 'printf \"#!/bin/sh\\ngrep Cpus_allowed_list /proc/self/status\\n\" > /tmp/steamwebhelper; chmod 755 /tmp/steamwebhelper; PATH=/usr/lib/wispos/bin:\$PATH taskset 0x7c /tmp/steamwebhelper | cut -f2')\" = 0-3 ] && [ \"\$(as_root 'taskset 0x7c /tmp/steamwebhelper | cut -f2')\" = 2-3 ]"
check "it holds the UI cgroup at the idle ceiling (550 MiB) on the real kernel's cgroup v2 and lifts it when stopped, thawed" \
    "as_root 'cat /sys/fs/cgroup/unified/wisp-ui/memory.high' | grep -qx 576716800 && as_root 'systemctl stop wisp-ui-reclaim.service; cat /sys/fs/cgroup/unified/wisp-ui/memory.high; cat /sys/fs/cgroup/unified/wisp-ui/cgroup.freeze' | tr '\n' ' ' | grep -q '^max 0 ' ; as_root 'systemctl start wisp-ui-reclaim.service'"
check "the off-switch file keeps it from starting (and removing the file lets it)" \
    "as_root 'systemctl stop wisp-ui-reclaim.service; touch /storage/.config/wispos/ui-reclaim-off; systemctl start wisp-ui-reclaim.service; systemctl is-active wisp-ui-reclaim.service; true' | grep -qx inactive && as_root 'rm /storage/.config/wispos/ui-reclaim-off; systemctl start wisp-ui-reclaim.service; systemctl is-active wisp-ui-reclaim.service' | grep -qx active"
# --- Steam wrapper, end to end, with a stand-in client -------------------------------------------
# The stand-in is a dynamic aarch64 program (it runs against the image's own libc) that executes a
# real LSE instruction, LDADD, which the emulated Cortex-A53 rejects with SIGILL. If the preload that
# ships in the image works, the instruction is emulated and the program reports the right values.
STUB=$D/stub; mkdir -p "$STUB/runtime/steam/steamrtarm64" "$STUB/runtime/lib" "$STUB/runtime/meta"
cat >"$STUB/steam.c" <<'EOC'
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>
int main(int argc, char **argv) {
    volatile uint64_t v = 40, old = 0, add = 2;
    __asm__ volatile(".arch armv8.1-a\n ldadd %2, %0, [%1]" : "=&r"(old) : "r"(&v), "r"(add) : "memory");
    printf("stub: LD_PRELOAD=%s old=%llu new=%llu args=%d\n", getenv("LD_PRELOAD") ? getenv("LD_PRELOAD") : "(none)",
           (unsigned long long)old, (unsigned long long)v, argc);
    return 7;
}
EOC
aarch64-linux-gnu-gcc -O1 -o "$STUB/runtime/steam/steamrtarm64/steam" "$STUB/steam.c" && : >"$STUB/runtime/READY"
check "push-steam-runtime.sh copies a runtime to the device (READY last)" \
    "WISP_SSH_PRIVATE_KEY='$KEY' WISP_SSH_PORT=$PORT WISP_SKIP_SPACE_CHECK=1 WISP_SSH_EXTRA='-o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -o LogLevel=ERROR' bash '$HERE/push-steam-runtime.sh' --no-start root@127.0.0.1 '$STUB/runtime'"
check "the pushed runtime is in place on the device" "as_root 'test -f /storage/wispos/runtime/READY && test -x /storage/wispos/runtime/steam/steamrtarm64/steam'"
as_root '/usr/bin/wisp-steam-session; echo session-exit=$?' >"$STUB/session.out" 2>&1
check "session wrapper: preload from the image is verified and loaded, client exits 7 and the wrapper passes that on" \
    "grep -q 'session-exit=7' '$STUB/session.out'"
check "the image's SIGILL fallback emulates a real LSE instruction on the Cortex-A53 (LDADD: old=40 new=42)" \
    "as_root 'cat /storage/wispos/logs/session/steam.log' | grep -q 'old=40 new=42 .*' && as_root 'cat /storage/wispos/logs/session/steam.log' | grep -q 'LD_PRELOAD=/usr/lib/wispos/libwispos-ldadd.so'"
as_root 'mkdir -p /tmp/t && cp /usr/lib/wispos/libwispos-ldadd.so /tmp/t/ && printf "%064d  libwispos-ldadd.so\n" 0 > /tmp/t/libwispos-ldadd.sha256 && sed "s#cd /usr/lib/wispos#cd /tmp/t#" /usr/bin/wisp-steam-session > /tmp/t/s.sh && bash /tmp/t/s.sh; echo rc=$?' >"$STUB/altered.out" 2>&1
check "an altered preload is refused with exit 3 (which the unit does not retry)" "grep -q 'rc=3' '$STUB/altered.out'"
for _ in $(seq 1 40); do as_root 'test -s /flash/wispos/last-boot.txt' && break; sleep 5; done
check "the boot report reached the FAT partition (wispos/last-boot.txt) and the storage copy" "as_root 'test -s /flash/wispos/last-boot.txt && test -s /storage/wispos/logs/last-boot.txt && grep -q \"== kernel log\" /flash/wispos/last-boot.txt'"
echo; echo "--- /flash/wispos/last-boot.txt (head)"; as_root 'head -32 /flash/wispos/last-boot.txt | cut -c1-150'
echo; echo "--- /var/log/boot.log (ROCKNIX autostart steps; hardware steps are expected to complain in a VM)"
as_root 'cut -c1-160 /var/log/boot.log | tail -45'
echo; echo "serial log: $LOG"; exit $fail
