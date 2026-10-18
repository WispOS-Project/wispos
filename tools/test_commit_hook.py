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

    def commit_verdict(self, args, editor_seed=None, cleanup=None):
        """Run a real `git commit` with the shipped hook installed.

        The hook must validate what Git will store, which depends on the
        effective cleanup mode; feeding the hook a file cannot show that.
        Returns (exit code, stored subject or '').
        """
        with tempfile.TemporaryDirectory(prefix='wispos-commit-') as root:
            root, repo = Path(root), Path(root) / 'repo'
            env = dict(os.environ)
            subprocess.run(['git', 'init', '-q', str(repo)], check=True, env=env)
            subprocess.run(['git', 'config', 'core.hooksPath', str(HOOK.parent)],
                           cwd=repo, check=True, env=env)
            if cleanup:
                subprocess.run(['git', 'config', 'commit.cleanup', cleanup],
                               cwd=repo, check=True, env=env)
            (repo / 'f.txt').write_text('x\n')
            subprocess.run(['git', 'add', 'f.txt'], cwd=repo, check=True, env=env)
            if editor_seed is not None:
                seed = root / 'seed.txt'
                seed.write_text(editor_seed)
                ed = root / 'ed.sh'
                ed.write_text(f'#!/bin/sh\ncp "{seed}" "$1"\n')
                ed.chmod(0o755)
                env['GIT_EDITOR'] = str(ed)          # a real editor: not ':'
            r = subprocess.run(['git', '-c', 'user.name=T', '-c',
                                'user.email=t@example.com', 'commit', '-q'] + list(args),
                               cwd=repo, capture_output=True, text=True, env=env)
            subject = subprocess.run(['git', '-c', 'user.name=T', '-c',
                                      'user.email=t@example.com', 'log', '-1', '--format=%s'],
                                     cwd=repo, capture_output=True, text=True,
                                     env=env).stdout.strip() if r.returncode == 0 else ''
            return r.returncode, subject

    def test_no_editor_commit_rejects_comment_shaped_subject(self):
        # `git commit -m` keeps '#' lines (cleanup whitespace), so that comment is
        # the real subject and the conforming second line is not what Git stores.
        rc, subject = self.commit_verdict(
            ['-m', '# forged subject', '-m', 'docs: notes', '-m', 'Refs #8'])
        self.assertNotEqual(rc, 0, f'fail-open: committed with subject {subject!r}')
        self.assertEqual(subject, '')

    def test_explicit_strip_cleanup_still_accepts_commented_message(self):
        # With cleanup=strip Git does drop the comment, so accepting is correct.
        rc, subject = self.commit_verdict(
            ['-m', '# forged subject', '-m', 'docs: notes', '-m', 'Refs #8'],
            cleanup='strip')
        self.assertEqual(rc, 0)
        self.assertEqual(subject, 'docs: notes')

    def test_editor_commit_strips_template_comment(self):
        rc, subject = self.commit_verdict(['-e'],
                                          editor_seed='# WIP stuff\ndocs: notes\n\nRefs #8\n')
        self.assertEqual(rc, 0)
        self.assertEqual(subject, 'docs: notes')

    def test_installer_migrates_a_shared_hooks_path(self):
        # A shared core.hooksPath left by the earlier installer is invisible to a
        # plain --get once worktree config exists, and siblings inherit it.
        with tempfile.TemporaryDirectory(prefix='wispos-migrate-') as root:
            root, repo = Path(root), Path(root) / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], check=True)
            subprocess.run(['git', 'config', 'core.hooksPath', 'tools/githooks'],
                           cwd=repo, check=True)
            subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.com',
                            'commit', '-q', '--allow-empty', '-m', 'seed'],
                           cwd=repo, check=True)
            pre = root / 'pre'
            subprocess.run(['git', '-c', 'user.name=T', '-c', 'user.email=t@example.com',
                            'worktree', 'add', '-q', '--detach', str(pre)],
                           cwd=repo, check=True)
            subprocess.run(['sh', str(HERE / 'install-hooks.sh')], cwd=repo,
                           check=True, capture_output=True, text=True)
            self.assertEqual(subprocess.run(
                ['git', 'config', '--local', '--get', 'core.hooksPath'],
                cwd=repo, capture_output=True, text=True).stdout.strip(), '')
            self.assertEqual(subprocess.run(
                ['git', 'config', '--worktree', '--get', 'core.hooksPath'],
                cwd=repo, capture_output=True, text=True).stdout.strip(), 'tools/githooks')
            self.assertEqual(subprocess.run(
                ['git', 'config', '--get', 'core.hooksPath'],
                cwd=pre, capture_output=True, text=True).stdout.strip(), '')


if __name__ == '__main__':
    unittest.main()
