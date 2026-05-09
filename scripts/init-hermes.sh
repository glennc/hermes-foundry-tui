#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

if [ ! -f .gitmodules ]; then
  echo "Missing .gitmodules; Hermes submodule has not been configured." >&2
  exit 1
fi

git submodule update --init third_party/hermes

branch="$(git config -f .gitmodules --get submodule.third_party/hermes.branch || true)"
if [ -n "$branch" ]; then
  git -C third_party/hermes fetch origin "$branch" --quiet
  if git -C third_party/hermes show-ref --verify --quiet "refs/heads/$branch"; then
    git -C third_party/hermes checkout "$branch"
    git -C third_party/hermes merge --ff-only "origin/$branch"
  else
    git -C third_party/hermes checkout -b "$branch" "origin/$branch"
  fi
fi

echo "Hermes submodule ready: third_party/hermes"
