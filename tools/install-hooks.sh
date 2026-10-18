#!/bin/sh
# Install issue-linked commit validation for this worktree only. Run once in
# each clone/worktree. Relative hooksPath survives moving the checkout.
set -eu
# Worktree configuration requires this repository extension. A bare repository
# or explicit core.worktree needs manual config migration before enabling it.
if [ "$(git rev-parse --is-bare-repository)" = true ] ||
   git config --local --get core.worktree >/dev/null 2>&1; then
    echo "refusing: install from a non-bare worktree without explicit core.worktree" >&2
    exit 1
fi
# Never silently replace a developer's hook suite, at either scope. Plain
# `git config --get` prefers config.worktree once worktree config exists, so it
# cannot be the only probe: a shared value in .git/config would stay invisible
# and every sibling worktree would keep inheriting this worktree's choice.
effective=$(git config --get core.hooksPath || true)
shared=$(git config --local --get core.hooksPath || true)
for scope_value in "$effective" "$shared"; do
    if [ -n "$scope_value" ] && [ "$scope_value" != tools/githooks ]; then
        echo "refusing: existing core.hooksPath must be integrated explicitly" >&2
        exit 1
    fi
done
if [ -z "$effective" ] && [ -z "$shared" ]; then
    hooks=$(git rev-parse --git-common-dir)/hooks
    for hook in "$hooks"/*; do
        case "$hook" in *.sample) continue ;; esac
        if [ -f "$hook" ] && [ -x "$hook" ]; then
            echo "refusing: existing default hooks must be integrated explicitly" >&2
            exit 1
        fi
    done
fi
git config extensions.worktreeConfig true
# A shared `tools/githooks` left by the pre-worktree installer is migrated to
# worktree scope here: leaving it in .git/config re-enables the inheritance this
# installer exists to avoid, and would silently govern sibling worktrees.
if [ "$shared" = tools/githooks ]; then
    git config --local --unset core.hooksPath
fi
git config --worktree core.hooksPath tools/githooks
echo "hooks installed for this worktree: $(git config --worktree core.hooksPath)"
