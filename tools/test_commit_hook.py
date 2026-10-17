#!/usr/bin/env python3
"""The commit-msg hook accepts tied commits and rejects untied ones
(tools/githooks/commit-msg, installed by tools/install-hooks.sh)."""
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / 'githooks' / 'commit-msg'


def verdict(message):
    with tempfile.TemporaryDirectory(prefix='wispos-msg-') as root:
        f = Path(root) / 'msg'
        f.write_text(message)
        return subprocess.run(['sh', str(HOOK), str(f)],
                              capture_output=True, text=True).returncode


class CommitHookTests(unittest.TestCase):
    def test_feat_with_refs_passes(self):
        self.assertEqual(verdict('feat: wisp sudo, no password\n\nRefs #8\n'), 0)

    def test_fix_with_scope_and_fixes_passes(self):
        self.assertEqual(verdict('fix(steam): honour resolution override\n\nFixes #1\n'), 0)

    def test_release_with_refs_passes(self):
        self.assertEqual(verdict('release: 0.2.0\n\nRefs #8\n'), 0)

    def test_merge_commit_is_exempt(self):
        self.assertEqual(verdict("Merge branch 'main' into dev\n"), 0)

    def test_missing_issue_link_is_rejected(self):
        self.assertEqual(verdict('fix: heal the dirty bit\n'), 1)

    def test_bare_hash_without_keyword_is_rejected(self):
        self.assertEqual(verdict('docs: notes\n\n(#7)\n'), 1)

    def test_wrong_subject_shape_is_rejected(self):
        self.assertEqual(verdict('WIP stuff\n\nRefs #8\n'), 1)

    def test_installer_points_at_the_hooks_dir(self):
        with tempfile.TemporaryDirectory(prefix='wispos-repo-') as root:
            subprocess.run(['git', 'init', '-q', str(root)], check=True)
            subprocess.run(['sh', str(HERE / 'install-hooks.sh')],
                           cwd=root, check=True, capture_output=True, text=True)
            out = subprocess.run(['git', 'config', 'core.hooksPath'],
                                 cwd=root, check=True, capture_output=True,
                                 text=True).stdout.strip()
            self.assertEqual(out, 'tools/githooks')

    def test_verbose_diff_refs_does_not_satisfy_the_link(self):
        msg = ('chore: cleanup\n'
               '# Please enter the commit message.\n'
               '# Refs #99\n'
               '-- >8 --\n'
               'diff --git a/f\n'
               '+Refs #99\n')
        self.assertEqual(verdict(msg), 1)

    def test_verbose_real_link_with_diff_noise_passes(self):
        msg = ('fix: heal the dirty bit\n\nRefs #8\n'
               '# On branch main.\n'
               '-- >8 --\n'
               'diff --git a/f\n'
               '+nothing relevant\n')
        self.assertEqual(verdict(msg), 0)

    def test_commented_subject_line_is_ignored(self):
        msg = ('# WIP stuff\n'
               'docs: notes\n\nRefs #8\n')
        self.assertEqual(verdict(msg), 0)


if __name__ == '__main__':
    unittest.main()
