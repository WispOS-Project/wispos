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
# Git's own scissor line: the comment character, a space, 24 dashes, ' >8 ',
# 24 dashes. Only THIS form truncates; a bare `-- >8 --` is stored as content.
SCISSOR = '------------------------ >8 ------------------------'


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

    def commit_verdict(self, args, editor_seed=None, cleanup=None, comment_char=None,
                       editor_append=None, editor_subject=None):
        """Run a real `git commit` with the shipped hook installed.

        The hook must validate what Git will store, which depends on the
        effective cleanup mode AND on the effective comment character; feeding
        the hook a file cannot show either. `editor_append` adds text to the
        template Git itself wrote, so a footer lands below Git's own scissor
        line instead of an imitation of one.
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
            if comment_char:
                subprocess.run(['git', 'config', 'core.commentChar', comment_char],
                               cwd=repo, check=True, env=env)
            (repo / 'f.txt').write_text('x\n')
            subprocess.run(['git', 'add', 'f.txt'], cwd=repo, check=True, env=env)
            if editor_seed is not None or editor_append is not None or \
                    editor_subject is not None:
                lines = ['#!/bin/sh']
                if editor_subject is not None:
                    # Keep Git's own template (its comment lines and its scissor
                    # line) and put a real subject above it, so an appended footer
                    # falls below the marker Git writes rather than replacing it.
                    subj = root / 'subject.txt'
                    subj.write_text(editor_subject)
                    lines.append('{ cat "%s"; cat "$1"; } > "$1.new"' % subj)
                    lines.append('mv "$1.new" "$1"')
                if editor_seed is not None:
                    seed = root / 'seed.txt'
                    seed.write_text(editor_seed)
                    lines.append(f'cp "{seed}" "$1"')
                if editor_append is not None:
                    # Land after Git's own scissor line, in the region Git cuts away.
                    app = root / 'append.txt'
                    app.write_text(editor_append)
                    lines.append(f'printf "\\n" >> "$1"')
                    lines.append(f'cat "{app}" >> "$1"')
                ed = root / 'ed.sh'
                ed.write_text('\n'.join(lines) + '\n')
                ed.chmod(0o755)
                env['GIT_EDITOR'] = str(ed)          # a real editor: not ':'
            r = subprocess.run(['git', '-c', 'user.name=T', '-c',
                                'user.email=t@example.com', 'commit', '-q'] + list(args),
                               cwd=repo, capture_output=True, text=True, env=env,
                               stdin=subprocess.DEVNULL)
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

    def test_scissors_cleanup_keeps_comment_subject_like_whitespace(self):
        # commit.cleanup=scissors truncates at the scissor line but does NOT strip
        # comments, so an editor run still stores the comment as the subject.
        rc, subject = self.commit_verdict(
            ['-e'], cleanup='scissors',
            editor_seed='# forged subject\ndocs: notes\n\nRefs #8\n')
        self.assertNotEqual(rc, 0, f'fail-open under scissors cleanup: {subject!r}')
        self.assertEqual(subject, '')

    def test_scissor_marker_as_content_does_not_reject(self):
        # Under the no-editor default Git stores `-- >8 --` as ordinary text, so
        # cutting there would drop the real footer and reject a valid commit.
        rc, subject = self.commit_verdict(
            ['-m', 'docs: notes', '-m', '-- >8 --', '-m', 'Refs #8'])
        self.assertEqual(rc, 0, f'rejected a message Git keeps whole: {subject!r}')
        self.assertEqual(subject, 'docs: notes')

    def test_bare_scissor_text_does_not_truncate_the_message(self):
        # Git's own scissor line carries the comment char and 24 dashes on each
        # side; a bare `-- >8 --` is ordinary text Git stores, so it must not
        # truncate the message and swallow the footer below it.
        self.assertEqual(verdict('docs: notes\n-- >8 --\n\nRefs #8\n'), 0)

    def test_whitespace_only_summary_is_rejected(self):
        # `: .+` accepted 'docs:  ' because the summary could be all spaces, and
        # cleanup=verbatim keeps that whitespace in the stored commit.
        self.assertEqual(verdict('docs:  \n\nRefs #8\n'), 1)

    def test_scissor_line_of_the_configured_character_truncates(self):
        # Git writes its scissor line with the comment character. Under
        # core.commentChar=';' the line it appends is ';' + marker, and a footer
        # below it is text Git discards. Anchoring the cut on '#' counted it (#30).
        rc, subject = self.commit_verdict(['-e', '-v'], cleanup='scissors',
                                          comment_char=';',
                                          editor_subject='docs: notes\n',
                                          editor_append='Refs #99\n')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored no footer for {subject!r}')
        self.assertEqual(subject, '')

    def test_scissor_line_of_the_bang_character_truncates(self):
        rc, subject = self.commit_verdict(['-e', '-v'], cleanup='scissors',
                                          comment_char='!',
                                          editor_subject='docs: notes\n',
                                          editor_append='Refs #99\n')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored no footer for {subject!r}')
        self.assertEqual(subject, '')

    def test_control_default_character_scissor_line_still_truncates(self):
        # Same shape with the default '#', which the hook already handled: proves
        # the two cases above differ only in the comment character.
        rc, subject = self.commit_verdict(['-e', '-v'], cleanup='scissors',
                                          editor_subject='docs: notes\n',
                                          editor_append='Refs #99\n')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored no footer for {subject!r}')

    def test_control_tied_commit_under_a_custom_character_still_passes(self):
        # A footer Git really stores must not be rejected by the new cut.
        rc, subject = self.commit_verdict(['-e', '-v'], cleanup='scissors',
                                          comment_char=';',
                                          editor_subject='docs: notes\n\nRefs #8\n')
        self.assertEqual(rc, 0, 'falsely rejected a tied commit')
        self.assertEqual(subject, 'docs: notes')

    def test_scissor_line_of_another_character_is_content_and_keeps_the_footer(self):
        # Git truncates only at its OWN marker, so under commentChar=';' a
        # '#'-prefixed marker line is ordinary text and the footer below it is
        # stored. A cut that ignores the character rejects a valid commit.
        rc, subject = self.commit_verdict(
            ['-e'], cleanup='scissors', comment_char=';',
            editor_seed='docs: notes\n\n# ' + SCISSOR + '\nRefs #99\n')
        self.assertEqual(rc, 0, 'falsely rejected: Git keeps that footer')
        self.assertEqual(subject, 'docs: notes')

    def test_strip_removes_the_configured_character_not_the_default_hash(self):
        # With commentChar=';' a '#'-prefixed line is content, and Git stores it as
        # the subject. Stripping '#' as a comment made the hook judge a different
        # message than Git keeps, passing a nonconforming subject (#30).
        rc, subject = self.commit_verdict(
            ['-m', '# forged subject', '-m', 'docs: notes', '-m', 'Refs #8'],
            comment_char=';', cleanup='strip')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored subject {subject!r}')
        self.assertEqual(subject, '')

    def test_auto_without_editor_has_no_comment_character_at_all(self):
        # Git resolves `auto` only while writing the editor template
        # (adjust_comment_line_char, builtin/commit.c), so under -m it strips
        # nothing: the '#'-prefixed line IS the stored subject (#30).
        rc, subject = self.commit_verdict(
            ['-m', '# forged subject', '-m', 'docs: notes', '-m', 'Refs #8'],
            comment_char='auto', cleanup='strip')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored subject {subject!r}')
        self.assertEqual(subject, '')

    def test_auto_uses_the_character_git_wrote_into_its_template(self):
        # A pre-editor message line starting with '#' makes auto escalate to the
        # next candidate; Git then writes its scissor line with ';'. The appended
        # footer sits below it, so Git discards it and the commit is untied.
        rc, subject = self.commit_verdict(
            ['-e', '-v', '-m', '# occupies the default character'],
            cleanup='scissors', comment_char='auto',
            editor_subject='docs: notes\n', editor_append='Refs #99\n')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored no footer for {subject!r}')

    def test_auto_that_kept_the_hash_character_still_truncates(self):
        # Control for the case above: with nothing occupying '#', auto resolves to
        # '#', so the '#' marker is Git's own and the footer below it is discarded.
        rc, subject = self.commit_verdict(['-e', '-v'], cleanup='scissors',
                                          comment_char='auto',
                                          editor_subject='docs: notes\n',
                                          editor_append='Refs #99\n')
        self.assertNotEqual(rc, 0, f'fail-open: Git stored no footer for {subject!r}')

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
