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
# Never silently replace a developer's existing hook suite. Leave it intact
# and report the integration requirement instead of disabling other hooks.
existing=$(git config --get core.hooksPath || true)
if [ -n "$existing" ] && [ "$existing" != tools/githooks ]; then
    echo "refusing: existing core.hooksPath must be integrated explicitly" >&2
    exit 1
fi
if [ -z "$existing" ]; then
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
git config --worktree core.hooksPath tools/githooks
echo "hooks installed for this worktree: $(git config --worktree core.hooksPath)"
