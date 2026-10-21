#!/usr/bin/env python3
"""tools/changelog.py: fragment validation, the highest class, and the release step that assembles fragments into CHANGELOG.md."""
import subprocess, sys, tempfile, unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
TOOL = HERE / 'changelog.py'
BASE = '# Changelog\n\n## [Unreleased]\n\n- **Old entry (fix, PATCH; #1):** kept.\n\n## [0.2.0] - 2026-10-16\n\n- shipped\n'


def run(*args):
    p = subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


class Fixture:
    def __init__(self, test, changelog=BASE):
        self.tmp = tempfile.TemporaryDirectory(prefix='wispos-cl-'); test.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.d = self.root / 'changelog.d'; self.d.mkdir()
        self.cl = self.root / 'CHANGELOG.md'; self.cl.write_text(changelog)

    def frag(self, name, text): (self.d / name).write_text(text)

    def run(self, *args): return run('--dir', str(self.d), '--changelog', str(self.cl), *args)


class ChangelogTests(unittest.TestCase):

    def test_check_accepts_well_named_fragments_and_ignores_the_readme(self):
        f = Fixture(self); f.frag('README.md', 'how to'); f.frag('12.minor.md', '**A (feature, MINOR; #12):** text'); f.frag('7.none.md', 'doc only')
        rc, out, _ = f.run('check'); self.assertEqual(rc, 0); self.assertIn('2 fragment', out)

    def test_check_rejects_a_bad_name_an_unknown_class_and_an_empty_file(self):
        for name, text in (('notes.md', 'x'), ('5.huge.md', 'x'), ('5.patch.txt', 'x'), ('5.patch.md', '  \n')):
            f = Fixture(self); f.frag(name, text)
            rc, _, err = f.run('check'); self.assertEqual(rc, 1, name); self.assertIn(name, err)

    def test_highest_picks_the_largest_class_and_none_when_only_none(self):
        f = Fixture(self); f.frag('1.patch.md', 'a'); f.frag('2.minor.md', 'b'); f.frag('3.none.md', 'c')
        self.assertEqual(f.run('highest')[1].strip(), 'minor')
        f = Fixture(self); f.frag('3.none.md', 'c'); self.assertEqual(f.run('highest')[1].strip(), 'none')
        f = Fixture(self); f.frag('4.major.md', 'd'); f.frag('5.minor.md', 'e'); self.assertEqual(f.run('highest')[1].strip(), 'major')

    def test_release_moves_fragments_and_old_entries_into_a_dated_section_and_deletes_the_fragments(self):
        f = Fixture(self); f.frag('12.patch.md', '**Fix (fix, PATCH; #12):** p'); f.frag('9.minor.md', '**Feat (feature, MINOR; #9):** m\nsecond line')
        rc, out, _ = f.run('release', '0.3.0', '2026-11-01'); self.assertEqual(rc, 0, out)
        text = f.cl.read_text()
        self.assertEqual(text, '# Changelog\n\n## [Unreleased]\n\n## [0.3.0] - 2026-11-01\n\n'
                               '- **Feat (feature, MINOR; #9):** m\n  second line\n\n- **Fix (fix, PATCH; #12):** p\n\n'
                               '- **Old entry (fix, PATCH; #1):** kept.\n\n## [0.2.0] - 2026-10-16\n\n- shipped\n')
        self.assertEqual([p.name for p in f.d.iterdir()], [])

    def test_release_refuses_a_repeated_version_a_bad_version_a_bad_date_and_an_empty_release(self):
        f = Fixture(self); f.frag('1.patch.md', 'x')
        for args in (('0.2.0', '2026-11-01'), ('v0.3', '2026-11-01'), ('0.3.0', 'tomorrow')):
            rc, _, err = f.run('release', *args); self.assertEqual(rc, 1, args); self.assertTrue(err)
        self.assertEqual(f.cl.read_text(), BASE); self.assertTrue((f.d / '1.patch.md').exists())   # a refused release changes nothing
        f = Fixture(self, '# Changelog\n\n## [Unreleased]\n\n## [0.2.0] - 2026-10-16\n\n- shipped\n')
        rc, _, err = f.run('release', '0.3.0', '2026-11-01'); self.assertEqual(rc, 1); self.assertIn('nothing to release', err)

    def test_release_without_an_unreleased_heading_is_refused(self):
        f = Fixture(self, '# Changelog\n'); f.frag('1.patch.md', 'x')
        rc, _, err = f.run('release', '0.3.0', '2026-11-01'); self.assertEqual(rc, 1); self.assertIn('Unreleased', err)


if __name__ == '__main__': unittest.main()
