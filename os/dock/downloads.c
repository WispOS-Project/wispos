/* What the shell does when a download finishes: see downloads.h. */
#define _GNU_SOURCE
#include "downloads.h"

#include <ctype.h>
#include <stdio.h>
#include <string.h>
#include <strings.h>

static const char *base(const char *name)
{
    const char *s = strrchr(name, '/');
    return s ? s + 1 : name;
}

static bool ends_with(const char *s, const char *suffix)
{
    size_t n = strlen(s), m = strlen(suffix);
    return n >= m && strcasecmp(s + n - m, suffix) == 0;
}

bool dl_ignore(const char *name)
{
    const char *b = base(name);
    if (!*b || b[0] == '.') return true;
    static const char *const skip[] = { ".part", ".crdownload", ".download", ".tmp", ".temp", "~" };
    for (size_t i = 0; i < sizeof skip / sizeof *skip; i++) if (ends_with(b, skip[i])) return true;
    return false;
}

static bool is_archive_name(const char *b)
{
    static const char *const ext[] = { ".zip", ".7z", ".rar", ".tar", ".tgz", ".tar.gz", ".txz", ".tar.xz", ".tbz2", ".tar.bz2", ".tar.zst" };
    for (size_t i = 0; i < sizeof ext / sizeof *ext; i++) if (ends_with(b, ext[i])) return true;
    return false;
}

static bool is_archive_magic(const unsigned char *h, size_t n)
{
    if (n >= 4 && !memcmp(h, "PK\x03\x04", 4)) return true;                          /* zip */
    if (n >= 6 && !memcmp(h, "\x37\x7a\xbc\xaf\x27\x1c", 6)) return true;            /* 7z */
    if (n >= 2 && h[0] == 0x1f && h[1] == 0x8b) return true;                         /* gzip */
    if (n >= 6 && !memcmp(h, "\xfd\x37\x7a\x58\x5a\x00", 6)) return true;            /* xz */
    if (n >= 3 && !memcmp(h, "BZh", 3)) return true;                                 /* bzip2 */
    if (n >= 4 && !memcmp(h, "\x28\xb5\x2f\xfd", 4)) return true;                    /* zstd */
    if (n >= 4 && !memcmp(h, "Rar!", 4)) return true;                                /* rar */
    if (n >= 262 && !memcmp(h + 257, "ustar", 5)) return true;                       /* tar (beyond DL_HEAD bytes: only when the caller read that far) */
    return false;
}

static bool is_image_magic(const unsigned char *h, size_t n)
{
    if (n >= 8 && !memcmp(h, "\x89PNG\r\n\x1a\n", 8)) return true;
    if (n >= 3 && h[0] == 0xff && h[1] == 0xd8 && h[2] == 0xff) return true;
    if (n >= 4 && !memcmp(h, "GIF8", 4)) return true;
    if (n >= 12 && !memcmp(h, "RIFF", 4) && !memcmp(h + 8, "WEBP", 4)) return true;
    return false;
}

DlKind dl_classify(const char *name, const unsigned char *h, size_t n)
{
    const char *b = base(name);
    bool elf = n >= 20 && !memcmp(h, "\x7f" "ELF", 4);
    bool appimage_magic = elf && h[8] == 'A' && h[9] == 'I' && (h[10] == 1 || h[10] == 2);
    if (elf && (appimage_magic || ends_with(b, ".AppImage"))) {
        unsigned machine = h[5] == 1 ? (unsigned)(h[18] | (h[19] << 8)) : (unsigned)((h[18] << 8) | h[19]);
        if (machine == 0xb7) return DL_APPIMAGE_ARM;
        if (machine == 0x3e) return DL_APPIMAGE_X86;
        return DL_OTHER;                                                              /* an architecture this device cannot run at all */
    }
    if (is_archive_name(b) && is_archive_magic(h, n)) return DL_ARCHIVE;
    if (n >= 2 && h[0] == 'M' && h[1] == 'Z' && (ends_with(b, ".exe") || ends_with(b, ".msi"))) return DL_WINDOWS;
    if (n >= 4 && !memcmp(h, "\xd0\xcf\x11\xe0", 4) && ends_with(b, ".msi")) return DL_WINDOWS;
    if (is_image_magic(h, n)) return DL_IMAGE;
    return DL_OTHER;
}

void dl_stem(const char *name, char *out, size_t cap)
{
    static const char *const ext[] = { ".AppImage", ".tar.gz", ".tar.xz", ".tar.bz2", ".tar.zst", ".tgz", ".txz", ".tbz2", ".zip", ".7z", ".rar", ".tar", ".exe", ".msi" };
    const char *b = base(name);
    size_t n = strlen(b);
    for (size_t i = 0; i < sizeof ext / sizeof *ext; i++) {
        size_t m = strlen(ext[i]);
        if (n > m && strcasecmp(b + n - m, ext[i]) == 0) { n -= m; break; }
    }
    if (cap == 0) return;
    if (n >= cap) n = cap - 1;
    memcpy(out, b, n);
    out[n] = 0;
}

void dl_offer(DlKind kind, const char *name, DlOffer *o)
{
    char stem[56], shown[32];
    memset(o, 0, sizeof *o);
    dl_stem(name, stem, sizeof stem);
    /* a control character in a name from outside never reaches a toast, and a long name is cut so that the sentence around it still fits */
    for (char *p = stem; *p; p++) if ((unsigned char)*p < 0x20 || *p == 0x7f) *p = '?';
    snprintf(shown, sizeof shown, "%.28s%s", stem, strlen(stem) > 28 ? "..." : "");
    switch (kind) {
    case DL_APPIMAGE_ARM:
        snprintf(o->title, sizeof o->title, "Program downloaded");
        snprintf(o->text, sizeof o->text, "%s: put it in the application menu? It runs with full access.", shown);
        snprintf(o->label, sizeof o->label, "Install");
        o->action = DL_ACT_INSTALL;
        break;
    case DL_APPIMAGE_X86:
        snprintf(o->title, sizeof o->title, "Program downloaded");
        snprintf(o->text, sizeof o->text, "%s is built for x86-64: not supported yet.", shown);
        break;
    case DL_ARCHIVE:
        snprintf(o->title, sizeof o->title, "Archive downloaded");
        snprintf(o->text, sizeof o->text, "%s: extract it to /storage/games?", shown);
        snprintf(o->label, sizeof o->label, "Extract");
        o->action = DL_ACT_EXTRACT;
        break;
    case DL_WINDOWS:
        snprintf(o->title, sizeof o->title, "Windows program downloaded");
        snprintf(o->text, sizeof o->text, "Open Add game in the menu to add it to Steam.");
        break;
    case DL_IMAGE:
        snprintf(o->title, sizeof o->title, "Picture downloaded");
        snprintf(o->text, sizeof o->text, "%.90s", stem);
        snprintf(o->label, sizeof o->label, "Open");
        o->action = DL_ACT_OPEN;
        break;
    default:
        snprintf(o->title, sizeof o->title, "Download complete");
        snprintf(o->text, sizeof o->text, "%.90s", base(name));
        break;
    }
    for (char *p = o->text; *p; p++) if ((unsigned char)*p < 0x20 || *p == 0x7f) *p = '?';
}
