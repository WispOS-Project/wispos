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
git config extensions.worktreeConfig true
git config --worktree core.hooksPath tools/githooks
echo "hooks installed for this worktree: $(git config --worktree core.hooksPath)"
