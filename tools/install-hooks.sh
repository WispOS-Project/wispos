#!/bin/sh
# Install the project's git hooks (tools/githooks/): commit-msg rejects any
# commit whose message is not tied to a GitHub issue. Run once per clone:
#   tools/install-hooks.sh
# (A clone that never runs this gets no enforcement: git has no way to push
# hooks. The hook is a backstop, not the rule itself; the rule is AGENTS.md.)
set -eu
# Relative on purpose: an absolute path breaks when the clone is moved or
# renamed, and modern git resolves a relative hooksPath from any directory
# inside the worktree (verified). Linked worktrees inherit it via --shared
# config only when that config is used; per-clone install stays the rule.
git config core.hooksPath tools/githooks
echo "hooks installed: $(git config core.hooksPath)"
