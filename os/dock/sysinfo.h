/* What the desktop shell asks the system for, and how it reads the answers: the parsers of the tools' output (wpctl, nmcli, bluetoothctl, wisp-ssh, wisp-usb) and the sysfs reads and writes
 * (battery, backlight). Pure functions over text and a directory root, so they are tested with canned output and a fake /sys (os/dock/test_sysinfo.c); the shell (shell.c) runs the commands. */
#ifndef WISPOS_DOCK_SYSINFO_H
#define WISPOS_DOCK_SYSINFO_H

#include <stdbool.h>
#include <stddef.h>

/* `wpctl get-volume @DEFAULT_AUDIO_SINK@`: "Volume: 0.62" or "Volume: 0.62 [MUTED]". 1 when it was understood. */
int  sys_parse_wpctl(const char *out, float *volume, bool *muted);
/* `nmcli -t -f IN-USE,SSID,SIGNAL device wifi list`: the line starting "*" is the connection. 1 when connected (ssid and level 0..3 set), 0 when not, -1 when the output is not that. */
int  sys_parse_nmcli_wifi(const char *out, char *ssid, size_t cap, int *level);
/* `bluetoothctl show`: *available when a controller is reported, *on its "Powered". */
void sys_parse_bluetooth(const char *out, bool *available, bool *on);
/* `wisp-ssh status`: "ssh: on" and the first "address: x" (as addr "wisp@x"). */
void sys_parse_ssh(const char *out, bool *on, char *addr, size_t cap);
/* `wisp-usb status`: the first line "usb: off|computer|host" as 0, 1, 2; -1 when it says nothing. */
int  sys_parse_usb(const char *out);

/* `wisp-screen timeout`: seconds without a button press before the panel blanks, "300", or "off" (never blanks) as 0; -1 when it says neither. */
int  sys_parse_screen_timeout(const char *out);

/* `wisp-power can-sleep`: "sleep: yes" as 1, "sleep: no" as 0; -1 when it says neither. */
int  sys_parse_can_sleep(const char *out);

/* The first power supply of type Battery under `root` (/sys/class/power_supply): charge 0..1 and whether it is charging. 1 found, 0 none. */
int  sys_read_battery(const char *root, float *charge, bool *charging);
/* The first backlight under `root` (/sys/class/backlight): brightness 0..1 of its maximum, and the setting of it (never below `floor`). */
int  sys_read_backlight(const char *root, float *level);
int  sys_write_backlight(const char *root, float level, float floor);

/* The signal in percent as the status glyph's level 0..3. */
int  sys_signal_level(int percent);
/* The clock text for a time (HH:MM, 24 hour) written into out (at least 6 bytes). */
void sys_clock_text(long epoch_seconds, char *out, size_t cap);

#endif
