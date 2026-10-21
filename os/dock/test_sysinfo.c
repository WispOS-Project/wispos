/* Tests of the shell's system readers (sysinfo.c): canned tool output, a fake /sys, and random bytes for the parsers. Run by os/test_dock_ui.py under the sanitizers. */
#define _GNU_SOURCE
#include "sysinfo.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

static int checks, failed;
static void check_true(const char *w, int ok) { checks++; if (!ok) { failed++; printf("FAIL %s\n", w); } }
static void check_int(const char *w, long g, long x) { checks++; if (g != x) { failed++; printf("FAIL %s: got %ld, want %ld\n", w, g, x); } }
static void check_str(const char *w, const char *g, const char *x) { checks++; if (strcmp(g, x)) { failed++; printf("FAIL %s\n  got [%s]\n  want [%s]\n", w, g, x); } }

static void put(const char *dir, const char *rel, const char *text)
{
    char p[700], d[700];
    snprintf(p, sizeof p, "%s/%s", dir, rel);
    snprintf(d, sizeof d, "%s", p);
    for (char *s = strchr(d + strlen(dir) + 1, '/'); s; s = strchr(s + 1, '/')) { *s = 0; mkdir(d, 0755); *s = '/'; }
    FILE *f = fopen(p, "w");
    if (!f) { perror(p); exit(2); }
    fputs(text, f);
    fclose(f);
}

static void t_wpctl(void)
{
    float v; bool m;
    check_true("plain", sys_parse_wpctl("Volume: 0.62\n", &v, &m) && v > 0.619f && v < 0.621f && !m);
    check_true("muted", sys_parse_wpctl("Volume: 0.40 [MUTED]\n", &v, &m) && v > 0.399f && v < 0.401f && m);
    check_true("past 100 %: shown as full", sys_parse_wpctl("Volume: 1.30\n", &v, &m) && v == 1.0f);
    check_true("zero", sys_parse_wpctl("Volume: 0.00\n", &v, &m) && v == 0.f);
    check_true("another line mentions it", sys_parse_wpctl("noise\nVolume: 0.5\n", &v, &m) && v > 0.49f);
    check_true("not a volume", !sys_parse_wpctl("Failed to connect\n", &v, &m));
    check_true("a number that is not one", !sys_parse_wpctl("Volume: abc\n", &v, &m));
    check_true("an absurd number", !sys_parse_wpctl("Volume: 99999\n", &v, &m));
    check_true("negative", !sys_parse_wpctl("Volume: -1\n", &v, &m));
    check_true("a mute marker on another line is not this one's", sys_parse_wpctl("Volume: 0.5\nother [MUTED]\n", &v, &m) && !m);
    check_true("empty", !sys_parse_wpctl("", &v, &m));
}

static void t_nmcli(void)
{
    char ssid[48]; int lv;
    check_int("connected", sys_parse_nmcli_wifi("*:Home-WiFi-5G:78\n :Neighbour:45\n", ssid, sizeof ssid, &lv), 1);
    check_str("the name", ssid, "Home-WiFi-5G");
    check_int("a strong signal is 3", lv, 3);
    check_int("a later line", sys_parse_nmcli_wifi(" :Neighbour:45\n*:Mine:40\n", ssid, sizeof ssid, &lv), 1);
    check_str("is found", ssid, "Mine");
    check_int("a middling signal is 2", lv, 2);
    check_int("a weak one is 1", (sys_parse_nmcli_wifi("*:x:10\n", ssid, sizeof ssid, &lv), lv), 1);
    check_int("none is 0", (sys_parse_nmcli_wifi("*:x:0\n", ssid, sizeof ssid, &lv), lv), 0);
    check_int("a colon in the name is escaped", sys_parse_nmcli_wifi("*:Caf\\:e:70\n", ssid, sizeof ssid, &lv), 1);
    check_str("and comes through", ssid, "Caf:e");
    check_int("a space as the mark: not connected", sys_parse_nmcli_wifi(" :Neighbour:45\n :Other:60\n", ssid, sizeof ssid, &lv), 0);
    check_int("nothing: no networks", sys_parse_nmcli_wifi("", ssid, sizeof ssid, &lv), -1);
    check_int("a very long name is cut, not overrun", sys_parse_nmcli_wifi("*:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa:50\n", ssid, sizeof ssid, &lv), 1);
    check_true("to the buffer's size", strlen(ssid) < sizeof ssid);
    check_int("the mark alone", sys_parse_nmcli_wifi("*", ssid, sizeof ssid, &lv), 1);
    check_int("signal levels", sys_signal_level(100) * 1000 + sys_signal_level(67) * 100 + sys_signal_level(66) * 10 + sys_signal_level(33), 3000 + 300 + 20 + 1);
}

static void t_bluetooth(void)
{
    bool a, on;
    sys_parse_bluetooth("Controller AA:BB:CC:DD:EE:FF (public)\n\tName: wisp\n\tPowered: yes\n", &a, &on);
    check_true("a powered controller", a && on);
    sys_parse_bluetooth("Controller AA:BB:CC:DD:EE:FF (public)\n\tPowered: no\n", &a, &on);
    check_true("an unpowered one", a && !on);
    sys_parse_bluetooth("No default controller available\n", &a, &on);
    check_true("none", !a && !on);
    sys_parse_bluetooth("Powered: yes\n", &a, &on);
    check_true("a Powered line without a controller is nothing", !a && !on);
    sys_parse_bluetooth("", &a, &on);
    check_true("empty", !a && !on);
}

static void t_ssh_usb(void)
{
    bool on; char addr[64];
    sys_parse_ssh("ssh: on\nhost name: wisp\naddress: 192.0.2.17\naddress: 10.0.0.9\nlog in as: wisp\n", &on, addr, sizeof addr);
    check_true("on, the first address", on && !strcmp(addr, "wisp@192.0.2.17"));
    sys_parse_ssh("ssh: on\nhost name: wisp\n", &on, addr, sizeof addr);
    check_true("on with no network", on && !addr[0]);
    sys_parse_ssh("ssh: off\n", &on, addr, sizeof addr);
    check_true("off", !on && !addr[0]);
    sys_parse_ssh("ssh: off\naddress: 1.2.3.4\n", &on, addr, sizeof addr);
    check_true("off shows no address", !on && !addr[0]);
    sys_parse_ssh("", &on, addr, sizeof addr);
    check_true("empty is off", !on);
    check_int("usb off", sys_parse_usb("usb: off\nhost: available\n"), 0);
    check_int("computer", sys_parse_usb("usb: computer\ncomputer: on\n"), 1);
    check_int("host", sys_parse_usb("usb: host\n"), 2);
    check_int("sleep is offered", sys_parse_can_sleep("sleep: yes\n"), 1);
    check_int("sleep is not offered", sys_parse_can_sleep("sleep: no\n"), 0);
    check_int("no newline is fine", sys_parse_can_sleep("sleep: yes"), 1);
    check_int("a longer word is not yes", sys_parse_can_sleep("sleep: yesterday\n"), -1);
    check_int("a longer word is not no", sys_parse_can_sleep("sleep: nothing\n"), -1);
    check_int("another tool's line is not understood", sys_parse_can_sleep("usb: off\n"), -1);
    check_int("a line that only ends like ours is not ours", sys_parse_can_sleep("usb:   yes\n"), -1);
    check_int("nothing is not understood", sys_parse_can_sleep(""), -1);
    check_int("a timeout in seconds", sys_parse_screen_timeout("300\n"), 300);
    check_int("off is never", sys_parse_screen_timeout("off\n"), 0);
    check_int("Off in any case", sys_parse_screen_timeout("OFF"), 0);
    check_int("zero is never too", sys_parse_screen_timeout("0\n"), 0);
    check_int("a day is allowed", sys_parse_screen_timeout("86400\n"), 86400);
    check_int("more than a day is not", sys_parse_screen_timeout("86401\n"), -1);
    check_int("words are not", sys_parse_screen_timeout("soon\n"), -1);
    check_int("trailing junk is not", sys_parse_screen_timeout("300s\n"), -1);
    check_int("nothing is not", sys_parse_screen_timeout(""), -1);
    check_int("nothing", sys_parse_usb(""), -1);
    check_int("something else", sys_parse_usb("usb: banana\n"), -1);
    check_int("not at the start", sys_parse_usb("note\nusb: host\n"), -1);
}

static void t_sysfs(const char *dir)
{
    char root[600];
    snprintf(root, sizeof root, "%s/power_supply", dir);
    float c; bool ch;
    check_int("no battery directory", sys_read_battery(root, &c, &ch), 0);
    put(dir, "power_supply/AC/type", "Mains\n");
    put(dir, "power_supply/axp20x-battery/type", "Battery\n");
    put(dir, "power_supply/axp20x-battery/capacity", "78\n");
    put(dir, "power_supply/axp20x-battery/status", "Discharging\n");
    check_true("the battery is found past the charger", sys_read_battery(root, &c, &ch) && c > 0.779f && c < 0.781f && !ch);
    put(dir, "power_supply/axp20x-battery/status", "Charging\n");
    check_true("charging", sys_read_battery(root, &c, &ch) && ch);
    put(dir, "power_supply/axp20x-battery/status", "Full\n");
    check_true("full is not charging", sys_read_battery(root, &c, &ch) && !ch);
    put(dir, "power_supply/axp20x-battery/capacity", "250\n");
    check_true("a capacity past 100 is full", sys_read_battery(root, &c, &ch) && c == 1.0f);
    put(dir, "power_supply/axp20x-battery/capacity", "-5\n");
    check_true("and below 0 is empty", sys_read_battery(root, &c, &ch) && c == 0.f);
    put(dir, "power_supply/axp20x-battery/capacity", "x\n");
    check_true("garbage is 0, not a crash", sys_read_battery(root, &c, &ch) && c == 0.f);

    snprintf(root, sizeof root, "%s/backlight", dir);
    float b;
    check_int("no backlight", sys_read_backlight(root, &b), 0);
    put(dir, "backlight/pwm-backlight/max_brightness", "255\n");
    put(dir, "backlight/pwm-backlight/brightness", "128\n");
    check_true("read", sys_read_backlight(root, &b) && b > 0.50f && b < 0.51f);
    check_true("write 25 %", sys_write_backlight(root, 0.25f, 0.05f));
    char p[700], got[16] = "";
    snprintf(p, sizeof p, "%s/backlight/pwm-backlight/brightness", dir);
    FILE *f = fopen(p, "r");
    if (f) { if (!fgets(got, sizeof got, f)) got[0] = 0; fclose(f); }
    check_int("written as raw units", atoi(got), 64);
    check_true("write 0: never to black (the floor)", sys_write_backlight(root, 0.0f, 0.05f));
    f = fopen(p, "r"); if (f) { if (!fgets(got, sizeof got, f)) got[0] = 0; fclose(f); }
    check_int("the floor in raw units", atoi(got), 13);
    check_true("past 1 is full", sys_write_backlight(root, 7.0f, 0.05f));
    f = fopen(p, "r"); if (f) { if (!fgets(got, sizeof got, f)) got[0] = 0; fclose(f); }
    check_int("255", atoi(got), 255);
    char root2[600];
    snprintf(root2, sizeof root2, "%s/bl2", dir);
    put(dir, "bl2/tiny/max_brightness", "3\n");
    put(dir, "bl2/tiny/brightness", "2\n");
    check_true("a zero setting with no floor", sys_write_backlight(root2, 0.0f, 0.0f));
    snprintf(p, sizeof p, "%s/bl2/tiny/brightness", dir);
    f = fopen(p, "r");
    if (f) { if (!fgets(got, sizeof got, f)) got[0] = 0; fclose(f); }
    check_int("is never written as 0 (the panel would go black)", atoi(got), 1);
    put(dir, "bl3/dead/max_brightness", "0\n");
    put(dir, "bl3/dead/brightness", "0\n");
    put(dir, "bl3/zlive/max_brightness", "10\n");
    put(dir, "bl3/zlive/brightness", "5\n");
    snprintf(root2, sizeof root2, "%s/bl3", dir);
    check_true("a backlight with a maximum of 0 is skipped for the next", sys_read_backlight(root2, &b) && b > 0.49f && b < 0.51f);
}

static void t_clock(void)
{
    char s[8];
    sys_clock_text(0, s, sizeof s);
    check_true("a clock reads HH:MM", strlen(s) == 5 && s[2] == ':');
    sys_clock_text(86399, s, sizeof s);
    check_true("another", strlen(s) == 5 && s[2] == ':');
}

static unsigned long long rng = 88172645463325252ULL;
static unsigned rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng >> 11); }

static void t_fuzz(void)
{
    static const char *seeds[] = { "Volume: 0.62 [MUTED]\n", "*:Home:78\n :x:1\n", "Controller AA (public)\n\tPowered: yes\n", "ssh: on\naddress: 1.2.3.4\n", "usb: host\n" };
    for (int i = 0; i < 20000; i++) {
        char s[200];
        const char *seed = seeds[rnd() % 5];
        snprintf(s, sizeof s, "%s", seed);
        size_t len = strlen(s);
        for (int k = 0; k < 6; k++) if (len) s[rnd() % len] = (char)(rnd() % 256);
        if (rnd() % 4 == 0 && len) s[rnd() % len] = 0;
        for (size_t j = 0; j < len; j++) if (s[j] == 0 && j + 1 < len && rnd() % 2) s[j] = 'a';
        float v; bool m, on, a; char ssid[48], addr[64]; int lv;
        sys_parse_wpctl(s, &v, &m);
        sys_parse_nmcli_wifi(s, ssid, sizeof ssid, &lv);
        sys_parse_bluetooth(s, &a, &on);
        sys_parse_ssh(s, &on, addr, sizeof addr);
        sys_parse_usb(s);
    }
    check_true("20000 mutated outputs went through every parser without a fault", 1);
}

int main(void)
{
    const char *base = getenv("TMPDIR");
    char dir[512];
    snprintf(dir, sizeof dir, "%s/sysinfo-XXXXXX", base ? base : "/tmp");
    if (!mkdtemp(dir)) { perror("mkdtemp"); return 2; }
    t_wpctl(); t_nmcli(); t_bluetooth(); t_ssh_usb(); t_sysfs(dir); t_clock(); t_fuzz();
    char cmd[600];
    snprintf(cmd, sizeof cmd, "rm -rf '%s'", dir);
    if (system(cmd) != 0) return 2;
    printf("%d checks, %d failed\n", checks, failed);
    return failed ? 1 : 0;
}
