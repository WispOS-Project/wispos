#!/usr/bin/env python3
"""The commit-msg hook accepts tied commits and rejects untied ones
(tools/githooks/commit-msg, installed by tools/install-hooks.sh)."""
import os
from unittest.mock import patch
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
HOOK = HERE / 'githooks' / 'commit-msg'


def verdict(message, merge=False):
    with tempfile.TemporaryDirectory(prefix='wispos-msg-') as root:
        subprocess.run(['git', 'init', '-q', root], check=True)
        if merge:
            (Path(root) / '.git/MERGE_HEAD').write_text('a' * 40 + '\n')
        f = Path(root) / 'msg'
        f.write_text(message)
        return subprocess.run(['sh', str(HOOK), str(f)],
                              cwd=root, capture_output=True, text=True).returncode


class CommitHookTests(unittest.TestCase):
    def setUp(self):
        config = patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': os.devnull,
                                        'GIT_CONFIG_NOSYSTEM': '1'})
        config.start(); self.addCleanup(config.stop)

    def test_leading_blanks_are_cleaned_before_subject(self):
        self.assertEqual(verdict('# template\n\n  \ndocs: notes\n\nRefs #8\n'), 0)

    def test_installer_preserves_existing_custom_hooks(self):
        with tempfile.TemporaryDirectory(prefix='wispos-existing-hook-') as root:
            subprocess.run(['git', 'init', '-q', root], check=True)
            subprocess.run(['git', 'config', 'core.hooksPath', 'existing-hooks'],
                           cwd=root, check=True)
            result = subprocess.run(['sh', str(HERE / 'install-hooks.sh')],
                                    cwd=root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            actual = subprocess.check_output(['git', 'config', 'core.hooksPath'],
                                             cwd=root, text=True).strip()
            self.assertEqual(actual, 'existing-hooks')

    def test_installer_preserves_existing_default_hooks(self):
        with tempfile.TemporaryDirectory(prefix='wispos-default-hook-') as root:
            subprocess.run(['git', 'init', '-q', root], check=True)
            hook = Path(root) / '.git/hooks/pre-commit'
            hook.write_text('#!/bin/sh\nexit 0\n'); hook.chmod(0o755)
            result = subprocess.run(['sh', str(HERE / 'install-hooks.sh')],
                                    cwd=root, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(subprocess.run(['git', 'config', '--get', 'core.hooksPath'],
                                            cwd=root, capture_output=True).returncode, 1)

    def test_feat_with_refs_passes(self):
        self.assertEqual(verdict('feat: wisp sudo, no password\n\nRefs #8\n'), 0)

    def test_fix_with_scope_and_fixes_passes(self):
        self.assertEqual(verdict('fix(steam): honour resolution override\n\nFixes #1\n'), 0)

    def test_release_with_refs_passes(self):
        self.assertEqual(verdict('release: 0.2.0\n\nRefs #8\n'), 0)

    def test_merge_commit_is_exempt(self):
        self.assertEqual(verdict("Merge branch 'main' into dev\n", merge=True), 0)

    def test_merge_subject_without_merge_state_is_rejected(self):
        self.assertEqual(verdict("Merge anything\n"), 1)

    def test_actual_verbose_boundary_removes_diff_issue_refs(self):
        self.assertEqual(verdict('docs: notes\n'
                                 '# ------------------------ >8 ------------------------\n'
                                 'diff --git a/f b/f\n+Refs #99\n'), 1)

    def test_installer_does_not_change_sibling_worktree(self):
        with tempfile.TemporaryDirectory(prefix='wispos-worktrees-') as root:
            repo = Path(root) / 'repo'; sibling = Path(root) / 'sibling'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', '-c', 'user.name=Test', '-c',
                            'user.email=test@example.com', 'commit', '--allow-empty',
                            '-qm', 'seed'], cwd=repo, check=True)
            subprocess.run(['git', 'worktree', 'add', '-q', '--detach', str(sibling)],
                           cwd=repo, check=True)
            subprocess.run(['sh', str(HERE / 'install-hooks.sh')], cwd=repo,
                           check=True, capture_output=True)
            self.assertEqual(subprocess.run(['git', 'config', '--get', 'core.hooksPath'],
                                            cwd=sibling, capture_output=True).returncode, 1)

    def test_invalid_issue_references_are_rejected(self):
        for footer in ('Refs #0', 'NoRefs #123', 'Refs #12suffix'):
            with self.subTest(footer=footer):
                self.assertEqual(verdict('docs: notes\n\n' + footer + '\n'), 1)

    def test_missing_issue_link_is_rejected(self):
        self.assertEqual(verdict('fix: heal the dirty bit\n'), 1)

    def test_bare_hash_without_keyword_is_rejected(self):
        self.assertEqual(verdict('docs: notes\n\n(#7)\n'), 1)

    def test_wrong_subject_shape_is_rejected(self):
        self.assertEqual(verdict('WIP stuff\n\nRefs #8\n'), 1)

    def test_octal_escape_subject_is_not_expanded_before_validation(self):
        # On dash, `echo` turns \072 into ':' before grep sees it, which let the
        # nonconforming literal subject 'docs\072 forged' pass the shape check.
        self.assertEqual(verdict('docs\\072 forged\n\nRefs #8\n'), 1)

    def test_newline_escape_subject_cannot_smuggle_a_second_line(self):
        self.assertEqual(verdict('feat\\ntest: smuggled\n\nRefs #8\n'), 1)

    def test_installer_allows_empty_or_missing_hooks_dir(self):
        # Review raised this as an unexpanded-glob accident. POSIX leaves an
        # unmatched glob literal, so the -f/-x guard simply sees no file; these
        # cases are specified behavior, and this pins it against future edits.
        for label in ('empty', 'missing'):
            with self.subTest(hooks_dir=label), tempfile.TemporaryDirectory(
                    prefix='wispos-hooks-') as root:
                subprocess.run(['git', 'init', '-q', root], check=True)
                hooks = Path(root) / '.git' / 'hooks'
                if label == 'missing':
                    shutil.rmtree(hooks)
                else:
                    for p in hooks.iterdir():
                        shutil.rmtree(p) if p.is_dir() else p.unlink()
                self.assertEqual(hooks.exists(), label == 'empty')
                result = subprocess.run(['sh', str(HERE / 'install-hooks.sh')],
                                        cwd=root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(subprocess.run(['git', 'config', '--get', 'core.hooksPath'],
                                                cwd=root, capture_output=True,
                                                text=True).stdout.strip(), 'tools/githooks')

    def test_installer_ignores_non_executable_stray_file_in_hooks(self):
        # The guard refuses only an executable regular file; a plain leftover
        # file must not block installation.
        with tempfile.TemporaryDirectory(prefix='wispos-stray-') as root:
            subprocess.run(['git', 'init', '-q', root], check=True)
            (Path(root) / '.git' / 'hooks' / 'notes').write_text('not a hook\n')
            result = subprocess.run(['sh', str(HERE / 'install-hooks.sh')],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

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
