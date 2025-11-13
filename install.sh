#!/usr/bin/env bash
set -euo pipefail

# Intelligent installer for the WingMAV MAVProxy module.
# This script detects a MAVProxy installation, installs required
# dependencies, deploys the WingMAV module, and verifies the setup.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT="$SCRIPT_DIR"
MODULE_NAME="mavproxy_wingmav.py"
RUNNER_NAME="run_wingmav_proxy.py"

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m[SUCCESS]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

require_command() {
    local cmd=$1
    if ! command_exists "$cmd"; then
        error "Required command '$cmd' was not found in PATH."
        exit 1
    fi
}

# Determine whether elevated privileges are available.
init_privilege_helper() {
    if [[ $EUID -eq 0 ]]; then
        SUDO=""
        return
    fi

    if command_exists sudo; then
        SUDO="sudo"
    else
        error "This script requires root privileges for some operations. Install sudo or run as root."
        exit 1
    fi
}

# Install missing dependencies via apt-get when available.
ensure_apt_dependencies() {
    if ! command_exists apt-get; then
        warn "apt-get not available; skipping automatic package installation."
        return
    fi

    local packages=(python3 python3-pip python3-mavproxy python3-pymavlink python3-pygame joystick)
    local missing=()
    for pkg in "${packages[@]}"; do
        if ! dpkg -s "$pkg" >/dev/null 2>&1; then
            missing+=("$pkg")
        fi
    done

    if ((${#missing[@]} == 0)); then
        info "All apt packages already installed."
        return
    fi

    info "Installing missing apt packages: ${missing[*]}"
    $SUDO apt-get update
    $SUDO apt-get install -y "${missing[@]}"
}

# Verify that required Python modules are importable.
verify_python_environment() {
    info "Verifying Python environment …"
    python3 - <<'PY'
import importlib
import sys

required = {
    "MAVProxy": "python3-mavproxy",
    "pymavlink": "python3-pymavlink",
    "pygame": "python3-pygame",
}
missing = []
for module, package in required.items():
    try:
        importlib.import_module(module)
    except Exception as exc:  # pragma: no cover
        missing.append(f"{module} (install via apt package '{package}'): {exc}")

if missing:
    for message in missing:
        print(message)
    sys.exit(1)
PY
    info "Python dependencies look good."
}

# Detect potential MAVProxy module directories from the Python installation.
detect_existing_mavproxy_paths() {
    python3 - <<'PY'
from pathlib import Path
import importlib.util

spec = importlib.util.find_spec("MAVProxy.modules")
if spec and spec.submodule_search_locations:
    for location in spec.submodule_search_locations:
        path = Path(location).resolve()
        print(path)
PY
}

# Choose the target directory for installing the WingMAV module.
select_install_directory() {
    local detected_paths user_path
    mapfile -t detected_paths < <(detect_existing_mavproxy_paths)

    if ((${#detected_paths[@]} > 0)); then
        info "Detected MAVProxy module paths:"
        for path in "${detected_paths[@]}"; do
            echo "  - $path"
        done
    else
        warn "No MAVProxy module path detected from Python."
    fi

    user_path="${MAVPROXY_HOME:-$HOME/.mavproxy}/modules"
    mkdir -p "$user_path"
    echo "$user_path"
}

install_wingmav_module() {
    local target_dir=$1
    local target_file="$target_dir/$MODULE_NAME"

    info "Installing WingMAV module to $target_file"
    install -Dm755 "$REPO_ROOT/$MODULE_NAME" "$target_file"
}

install_runner_script() {
    local target
    if [[ -z ${SUDO:-} && $EUID -ne 0 ]]; then
        target="$HOME/.local/bin/wingmav-proxy"
        mkdir -p "$(dirname "$target")"
        install -Dm755 "$REPO_ROOT/$RUNNER_NAME" "$target"
        info "Installed helper runner to $target"
        warn "Ensure $HOME/.local/bin is in your PATH."
    else
        target="/usr/local/bin/wingmav-proxy"
        $SUDO install -Dm755 "$REPO_ROOT/$RUNNER_NAME" "$target"
        info "Installed helper runner to $target"
    fi
}

configure_dialout_group() {
    local target_user
    target_user=${SUDO_USER:-$USER}

    if [[ -z "$target_user" || "$target_user" == "root" ]]; then
        warn "Skipping dialout group configuration (no non-root user detected)."
        return
    fi

    if id -nG "$target_user" | grep -qw dialout; then
        info "User '$target_user' already in dialout group."
        return
    fi

    info "Adding user '$target_user' to dialout group"
    $SUDO usermod -a -G dialout "$target_user"
    warn "User '$target_user' must log out and back in for dialout membership to take effect."
}

run_environment_checks() {
    if command_exists mavproxy.py; then
        info "MAVProxy version:"
        mavproxy.py --version || warn "Unable to get MAVProxy version."
    else
        warn "mavproxy.py not found in PATH. You may need to adjust your PATH or install MAVProxy."
    fi

    info "Performing module import smoke test …"
    python3 - <<'PY'
import importlib
modules = ["MAVProxy.modules.lib.mp_module", "pymavlink", "pygame"]
for name in modules:
    importlib.import_module(name)
print("All required modules imported successfully.")
PY
}

print_post_install_instructions() {
    cat <<'MSG'

Next steps:
  • Launch MAVProxy with: mavproxy.py --load-module=rc,wingmav
  • Or add the following to ~/.mavinit.rc for automatic loading:
        module load rc
        module load wingmav

If you installed the helper script, you can start it via 'wingmav-proxy --help'.
MSG
}

main() {
    require_command python3
    init_privilege_helper
    ensure_apt_dependencies
    verify_python_environment || {
        error "Python environment verification failed. Install the missing modules above and re-run."
        exit 1
    }
    local module_dir
    module_dir=$(select_install_directory)
    install_wingmav_module "$module_dir"
    install_runner_script
    configure_dialout_group
    run_environment_checks
    success "WingMAV installation completed."
    print_post_install_instructions
}

main "$@"
