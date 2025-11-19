#!/usr/bin/env bash
set -euo pipefail

# Ensure we are in the repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# Refresh to the latest main branch
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git fetch origin main
  git switch main
  git pull --ff-only origin main
else
  echo "Error: This script must be run inside a git repository." >&2
  exit 1
fi

# Update system packages
sudo apt update
sudo apt full-upgrade -y
sudo apt autoremove -y
