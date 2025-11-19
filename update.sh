#!/usr/bin/env bash
set -euo pipefail

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m[SUCCESS]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }

SUDO=""
if command -v sudo >/dev/null 2>&1 && [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  SUDO="sudo"
fi

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

info "Refreshing repository to the latest main branch"
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  git fetch origin main
  git switch main
  git pull --ff-only origin main
  info "Syncing submodules to their remote tracking branches"
  git submodule sync --recursive
  git submodule update --init --recursive --remote
else
  error "This script must be run inside a git repository."
  exit 1
fi

if command -v apt >/dev/null 2>&1; then
  info "Updating system package index"
  ${SUDO:+$SUDO }apt update
  info "Applying system package upgrades"
  ${SUDO:+$SUDO }apt full-upgrade -y
  info "Removing unused packages"
  ${SUDO:+$SUDO }apt autoremove -y
else
  warn "Skipping apt operations (apt not available on this system)."
fi

if command -v pip3 >/dev/null 2>&1; then
  info "Upgrading WingMAV runtime Python modules"
  PIP_FLAGS=()
  if [[ -z "$SUDO" && ${EUID:-$(id -u)} -ne 0 ]]; then
    PIP_FLAGS+=(--user)
  fi
  pip3 install "${PIP_FLAGS[@]}" --upgrade MAVProxy pymavlink pygame
else
  warn "Skipping Python module upgrades (pip3 not available)."
fi

INSTALLER="$REPO_ROOT/install.sh"
if [[ -x "$INSTALLER" ]]; then
  info "Refreshing installed WingMAV assets via installer"
  "$INSTALLER" --non-interactive --yes --skip-apt
else
  warn "Installer not found; skipping WingMAV asset refresh."
fi

if command -v systemctl >/dev/null 2>&1; then
  SERVICES=(wingmav-proxy.service mavproxy.service)
  RESTARTED=false
  for service in "${SERVICES[@]}"; do
    if systemctl list-unit-files "$service" >/dev/null 2>&1; then
      if ! $RESTARTED; then
        info "Reloading systemd units"
        ${SUDO:+$SUDO }systemctl daemon-reload || warn "Failed to reload systemd units"
        RESTARTED=true
      fi
      info "Restarting $service"
      if ! ${SUDO:+$SUDO }systemctl restart "$service"; then
        warn "Could not restart $service"
      fi
    fi
  done
else
  warn "Skipping service reloads (systemctl not available)."
fi

success "System and WingMAV components are up to date."
