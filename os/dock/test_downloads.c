/* Tests of the download classifier (downloads.c): names, magic numbers, what is offered, and random bytes. Run by os/test_dock_ui.py under the sanitizers and against rule mutants. */
#include "downloads.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int checks, failed;
static void check_true(const char *w, int ok) { checks++; if (!ok) { failed++; printf("FAIL %s\n", w); } }
static void check_int(const char *w, long g, long x) { checks++; if (g != x) { failed++; printf("FAIL %s: got %ld, want %ld\n", w, g, x); } }
static void check_str(const char *w, const char *g, const char *x) { checks++; if (strcmp(g, x)) { failed++; printf("FAIL %s\n  got [%s]\n  want [%s]\n", w, g, x); } }

static DlKind cls(const char *name, const void *head, size_t n) { return dl_classify(name, head, n); }

static void elf(unsigned char *h, int machine, bool appimage, int type)
{
    memset(h, 0, DL_HEAD);
    memcpy(h, "\x7f" "ELF", 4);
    h[4] = 2; h[5] = 1; h[6] = 1;
    h[18] = (unsigned char)(machine & 255); h[19] = (unsigned char)(machine >> 8);
    if (appimage) { h[8] = 'A'; h[9] = 'I'; h[10] = (unsigned char)type; }
}

static void t_ignore(void)
{
    check_true("a partial file", dl_ignore("foo.zip.part"));
    check_true("another browser's", dl_ignore("foo.zip.crdownload"));
    check_true("case does not matter", dl_ignore("FOO.PART"));
    check_true("hidden", dl_ignore(".hidden"));
    check_true("a temp file", dl_ignore("x.tmp") && dl_ignore("x.temp") && dl_ignore("x.download"));
    check_true("a backup", dl_ignore("notes.txt~"));
    check_true("a directory part is not the name", dl_ignore("/storage/downloads/.git") && !dl_ignore("/storage/.cache/foo.zip"));
    check_true("empty", dl_ignore("") && dl_ignore("/storage/downloads/"));
    check_true("a download", !dl_ignore("foo.zip") && !dl_ignore("Foo-1.0.AppImage") && !dl_ignore("a.part.zip"));
}

static void t_classify(void)
{
    unsigned char h[DL_HEAD];
    elf(h, 0xb7, true, 2);
    check_int("an aarch64 AppImage", cls("Foo.AppImage", h, sizeof h), DL_APPIMAGE_ARM);
    check_int("by its magic, whatever it is called", cls("download", h, sizeof h), DL_APPIMAGE_ARM);
    elf(h, 0xb7, true, 1);
    check_int("a type 1 AppImage", cls("Foo.AppImage", h, sizeof h), DL_APPIMAGE_ARM);
    elf(h, 0xb7, true, 3);
    check_int("an AI magic of another type is not one by magic, but the name still says AppImage", cls("x.bin", h, sizeof h), DL_OTHER);
    elf(h, 0x3e, true, 2);
    check_int("an x86-64 AppImage", cls("Foo.AppImage", h, sizeof h), DL_APPIMAGE_X86);
    elf(h, 0x28, true, 2);
    check_int("an ARM 32-bit AppImage cannot run here", cls("Foo.AppImage", h, sizeof h), DL_OTHER);
    elf(h, 0xb7, false, 0);
    check_int("an ELF named AppImage without the magic", cls("Foo.AppImage", h, sizeof h), DL_APPIMAGE_ARM);
    check_int("a plain program is not offered", cls("tool", h, sizeof h), DL_OTHER);
    elf(h, 0xb7, true, 2);
    h[5] = 2; h[18] = 0; h[19] = 0xb7;
    check_int("a big-endian ELF is read the other way", cls("Foo.AppImage", h, sizeof h), DL_APPIMAGE_ARM);
    check_int("a short read of an ELF is not classified", cls("Foo.AppImage", h, 10), DL_OTHER);

    check_int("zip", cls("a.zip", "PK\x03\x04rest", 8), DL_ARCHIVE);
    check_int("7z", cls("a.7z", "\x37\x7a\xbc\xaf\x27\x1c\x00\x04", 8), DL_ARCHIVE);
    check_int("tar.gz", cls("a.tar.gz", "\x1f\x8b\x08\x00", 4), DL_ARCHIVE);
    check_int("tgz", cls("a.tgz", "\x1f\x8b\x08\x00", 4), DL_ARCHIVE);
    check_int("tar.xz", cls("a.tar.xz", "\xfd\x37\x7a\x58\x5a\x00\x00", 7), DL_ARCHIVE);
    check_int("tar.bz2", cls("a.tar.bz2", "BZh91AY", 7), DL_ARCHIVE);
    check_int("tar.zst", cls("a.tar.zst", "\x28\xb5\x2f\xfd\x00", 5), DL_ARCHIVE);
    check_int("rar", cls("a.rar", "Rar!\x1a\x07\x00", 7), DL_ARCHIVE);
    unsigned char tar[300];
    memset(tar, 0, sizeof tar); memcpy(tar + 257, "ustar", 5);
    check_int("a plain tar (magic at 257)", cls("a.tar", tar, sizeof tar), DL_ARCHIVE);
    check_int("a plain tar read short is not recognised", cls("a.tar", tar, 64), DL_OTHER);
    check_int("a zip that is called something else is not offered (a jar, a document)", cls("a.jar", "PK\x03\x04rest", 8), DL_OTHER);
    check_int("a name that says archive over bytes that are not", cls("a.zip", "not a zip at all", 16), DL_OTHER);
    check_int("case of the extension", cls("A.ZIP", "PK\x03\x04", 4), DL_ARCHIVE);

    check_int("an exe", cls("setup.exe", "MZ\x90\x00", 4), DL_WINDOWS);
    check_int("an exe in capitals", cls("SETUP.EXE", "MZ\x90\x00", 4), DL_WINDOWS);
    check_int("a msi", cls("setup.msi", "\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", 8), DL_WINDOWS);
    check_int("a file that starts MZ with another name", cls("notes", "MZ\x90\x00", 4), DL_OTHER);
    check_int("an exe name over other bytes", cls("setup.exe", "hello", 5), DL_OTHER);

    check_int("png", cls("a.png", "\x89PNG\r\n\x1a\n\0\0\0\rIHDR", 16), DL_IMAGE);
    check_int("jpeg", cls("a", "\xff\xd8\xff\xe0", 4), DL_IMAGE);
    check_int("gif", cls("a.gif", "GIF89a", 6), DL_IMAGE);
    check_int("webp", cls("a.webp", "RIFF\0\0\0\0WEBPVP8 ", 16), DL_IMAGE);
    check_int("a wav is not a picture", cls("a.wav", "RIFF\0\0\0\0WAVEfmt ", 16), DL_OTHER);
    check_int("text", cls("readme.txt", "Hello\n", 6), DL_OTHER);
    check_int("nothing read", cls("x", "", 0), DL_OTHER);
    check_int("the directory is not part of the name", cls("/a/b.zip/c", "PK\x03\x04", 4), DL_OTHER);
}

static void t_stem(void)
{
    char s[64];
    dl_stem("Foo-1.0-aarch64.AppImage", s, sizeof s); check_str("an AppImage", s, "Foo-1.0-aarch64");
    dl_stem("/storage/downloads/game.tar.gz", s, sizeof s); check_str("a path and a double extension", s, "game");
    dl_stem("a.b.zip", s, sizeof s); check_str("only the known one", s, "a.b");
    dl_stem("x.tar.xz", s, sizeof s); check_str("tar.xz", s, "x");
    dl_stem("x.tar.gz", s, sizeof s); check_str("tar.gz", s, "x");
    dl_stem(".zip", s, sizeof s); check_str("an extension alone stays", s, ".zip");
    dl_stem("README", s, sizeof s); check_str("no extension", s, "README");
    dl_stem("FOO.ZIP", s, sizeof s); check_str("capitals", s, "FOO");
    char t[5];
    dl_stem("abcdefghij.zip", t, sizeof t); check_str("cut to the room", t, "abcd");
    char one[1] = { 'x' };
    dl_stem("a", one, 0); check_true("no room at all writes nothing", one[0] == 'x');
    dl_stem("a", one, 1); check_true("room for the terminator only", one[0] == 0);
}

static void t_offer(void)
{
    DlOffer o;
    dl_offer(DL_APPIMAGE_ARM, "Foo-1.0.AppImage", &o);
    check_str("title", o.title, "Program downloaded"); check_str("label", o.label, "Install"); check_int("action", o.action, DL_ACT_INSTALL);
    check_true("it names the program and the risk", strstr(o.text, "Foo-1.0") && strstr(o.text, "full access"));
    dl_offer(DL_APPIMAGE_X86, "Foo.AppImage", &o);
    check_int("x86-64: nothing to offer", o.action, DL_ACT_NONE); check_true("and it says why", strstr(o.text, "x86-64") != NULL && o.label[0] == 0);
    dl_offer(DL_ARCHIVE, "Doom.zip", &o);
    check_int("archive: extract", o.action, DL_ACT_EXTRACT); check_str("label", o.label, "Extract"); check_true("names it", strstr(o.text, "Doom") && strstr(o.text, "/storage/games"));
    dl_offer(DL_WINDOWS, "setup.exe", &o);
    check_int("windows: information only", o.action, DL_ACT_NONE); check_true("and where to go", strstr(o.text, "Add game") != NULL);
    dl_offer(DL_IMAGE, "cat.png", &o);
    check_int("picture: open", o.action, DL_ACT_OPEN); check_str("label", o.label, "Open"); check_str("title", o.title, "Picture downloaded");
    dl_offer(DL_OTHER, "/x/notes.txt", &o);
    check_int("anything else: just complete", o.action, DL_ACT_NONE); check_str("the plain name", o.text, "notes.txt");
    dl_offer(DL_OTHER, "evil\nname\x01.txt", &o);
    check_true("control characters never reach a toast", strchr(o.text, '\n') == NULL && strchr(o.text, 1) == NULL && strstr(o.text, "evil?name?") != NULL);
    char longname[400];
    memset(longname, 'a', sizeof longname - 1); longname[sizeof longname - 1] = 0;
    dl_offer(DL_ARCHIVE, longname, &o);
    check_true("a very long name fits the toast's text (cut, terminated)", strlen(o.text) < sizeof o.text && strlen(o.text) > 20);
    check_true("and says it was cut", strstr(o.text, "...: extract") != NULL);
    dl_offer(DL_ARCHIVE, "aaaaaaaaaaaaaaaaaaaaaaaaaaaa.zip", &o);
    check_true("a name of exactly 28 letters is not cut", strstr(o.text, "...") == NULL);
    dl_offer(DL_OTHER, longname, &o);
    check_true("and plain too", strlen(o.text) < sizeof o.text);
}

static unsigned long long rng = 88172645463325252ULL;
static unsigned rnd(void) { rng ^= rng << 13; rng ^= rng >> 7; rng ^= rng << 17; return (unsigned)(rng >> 11); }

static void t_fuzz(void)
{
    static const char *names[] = { "a.zip", "Foo.AppImage", "x.exe", "pic", "weird\x01name", "", "/", "a.tar.gz", ".hidden", "x.part" };
    for (int i = 0; i < 100000; i++) {
        unsigned char h[300];
        size_t n = rnd() % 300;
        for (size_t k = 0; k < n; k++) h[k] = (unsigned char)rnd();
        if (rnd() % 2 && n >= 20) memcpy(h, "\x7f" "ELF", 4);
        if (rnd() % 3 == 0 && n >= 4) memcpy(h, "PK\x03\x04", 4);
        const char *name = names[rnd() % 10];
        DlKind k = dl_classify(name, h, n);
        DlOffer o;
        dl_offer(k, name, &o);
        char stem[16];
        dl_stem(name, stem, sizeof stem);
        dl_ignore(name);
        if (strlen(o.text) >= sizeof o.text || strlen(o.title) >= sizeof o.title) { failed++; printf("FAIL overflow\n"); return; }
    }
    check_true("100000 random heads and names went through without a fault", 1);
}

int main(void)
{
    t_ignore(); t_classify(); t_stem(); t_offer(); t_fuzz();
    printf("%d checks, %d failed\n", checks, failed);
    return failed ? 1 : 0;
}
