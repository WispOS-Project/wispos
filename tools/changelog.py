#!/usr/bin/env python3
"""Changelog fragments: one small file per change under changelog.d/, assembled into CHANGELOG.md at release time (issue #49, recommendation on #45).

  tools/changelog.py check                          the fragments are well named and not empty (exit 1 otherwise)
  tools/changelog.py highest                        the highest SemVer class among the fragments: major, minor, patch or none
  tools/changelog.py release VERSION YYYY-MM-DD     move the fragments and the current Unreleased entries into a new dated section, delete the fragments

A fragment is `changelog.d/<issue>.<class>.md`: `<issue>` the GitHub issue number, `<class>` one of major, minor, patch, none (`none`: documentation and tooling with no SemVer
effect). The file holds the entry text as it would read in the changelog, without the leading "- ". The author names the class and review checks it; nothing infers it from a
commit prefix. The manifest version and the tag stay with the maintainer (docs/RELEASING.md): `release` only edits CHANGELOG.md and removes fragments, and `highest` says which
bump the pending changes call for. Entries already under `## [Unreleased]` are kept and move into the new section after the fragments.
Options --dir and --changelog point at other locations (the tests use them)."""
import argparse, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CLASSES = ('major', 'minor', 'patch', 'none')
NAME = re.compile(r'^(\d+)\.(major|minor|patch|none)\.md$')
SEMVER = re.compile(r'^\d+\.\d+\.\d+$')
DATE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
UNRELEASED = '## [Unreleased]'


class ChangelogError(Exception):
    pass


def fragments(d):
    """The fragments of directory d as (issue, class, path, text), ordered by class (major first) then issue. README.md is not a fragment; any other file must match the name."""
    out = []
    for p in sorted(Path(d).iterdir()):
        if p.name == 'README.md' or p.name.startswith('.'): continue
        m = NAME.match(p.name)
        if not m: raise ChangelogError('%s: name must be <issue>.<major|minor|patch|none>.md' % p.name)
        text = p.read_text().strip()
        if not text: raise ChangelogError('%s: empty' % p.name)
        out.append((int(m.group(1)), m.group(2), p, text))
    out.sort(key=lambda f: (CLASSES.index(f[1]), f[0]))
    return out


def highest(frags):
    for c in CLASSES:
        if any(f[1] == c for f in frags): return c
    return 'none'


def bullet(text):
    lines = text.split('\n')
    return '- ' + lines[0] + ''.join('\n  ' + l if l.strip() else '\n' for l in lines[1:])


def release(changelog, d, version, date):
    if not SEMVER.match(version): raise ChangelogError('version must be X.Y.Z, got %r' % version)
    if not DATE.match(date): raise ChangelogError('date must be YYYY-MM-DD, got %r' % date)
    frags = fragments(d)
    text = Path(changelog).read_text()
    if UNRELEASED not in text: raise ChangelogError('%s has no "%s" heading' % (changelog, UNRELEASED))
    if re.search(r'^## \[%s\]' % re.escape(version), text, re.M): raise ChangelogError('%s already has a section for %s' % (changelog, version))
    head, rest = text.split(UNRELEASED, 1)
    nxt = re.search(r'^## \[', rest, re.M)
    body, tail = (rest[:nxt.start()], rest[nxt.start():]) if nxt else (rest, '')
    body = body.strip('\n')
    if not frags and not body: raise ChangelogError('nothing to release: no fragments and nothing under Unreleased')
    parts = [bullet(f[3]) for f in frags] + ([body] if body else [])
    section = '## [%s] - %s\n\n%s\n' % (version, date, '\n\n'.join(parts))
    new = head + UNRELEASED + '\n\n' + section + ('\n' + tail if tail else '')
    Path(changelog).write_text(new)
    for f in frags: f[2].unlink()
    return len(frags)


def main(argv):
    ap = argparse.ArgumentParser(prog='changelog.py')
    ap.add_argument('--dir', default=str(ROOT / 'changelog.d')); ap.add_argument('--changelog', default=str(ROOT / 'CHANGELOG.md'))
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('check'); sub.add_parser('highest')
    r = sub.add_parser('release'); r.add_argument('version'); r.add_argument('date')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'check':
            print('changelog.py: %d fragment(s) ok' % len(fragments(a.dir)))
        elif a.cmd == 'highest':
            print(highest(fragments(a.dir)))
        else:
            n = release(a.changelog, a.dir, a.version, a.date)
            print('changelog.py: released %s with %d fragment(s); set the manifest version and tag it (docs/RELEASING.md)' % (a.version, n))
    except (ChangelogError, OSError) as e:
        print('changelog.py: %s' % e, file=sys.stderr); return 1
    return 0


if __name__ == '__main__': sys.exit(main(sys.argv[1:]))
