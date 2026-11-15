#!/usr/bin/env bash
# Give an agent its own working copy, so two agents never commit, push or review each other's files.
#
#   scripts/agent_worktree.sh bird          # creates ../secure-query-bird on branch agent/bird
#   git worktree remove ../secure-query-bird   # when its work is merged
#
# The copy starts from the current trusted-core commit and links the untracked things the code
# needs: .venv (344 MB), data/ (10 GB) and .env (keys; linked, never copied). The shared .venv has
# an editable install of THIS checkout's src, so always run with PYTHONPATH=src (the commit hook
# and the scripts already do) or the worktree's own changes are not the code that runs.
set -euo pipefail
name=${1:?usage: scripts/agent_worktree.sh <name>}
root=$(git rev-parse --show-toplevel)
dest="$(dirname "$root")/$(basename "$root")-$name"
# .gitignore's ".venv/" and "data/" match folders, not links: exclude the links locally.
exclude="$(git -C "$root" rev-parse --git-common-dir)/info/exclude"
for item in /.venv /data; do grep -qxF "$item" "$exclude" 2>/dev/null || echo "$item" >> "$exclude"; done
git -C "$root" worktree add -b "agent/$name" "$dest" trusted-core
for item in .venv data .env; do ln -s "$root/$item" "$dest/$item"; done
echo "Ready: cd $dest  (branch agent/$name; start the agent there)"
