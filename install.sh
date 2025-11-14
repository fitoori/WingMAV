#!/usr/bin/env bash
set -euo pipefail

# WingMAV setup wizard. This installer detects a MAVProxy installation,
# installs the WingMAV module, optionally deploys the helper runner script,
# manages permissions, and performs environment checks.

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT="$SCRIPT_DIR"
MODULE_NAME="mavproxy_wingmav.py"
RUNNER_NAME="run_wingmav_proxy.py"

info() { printf '\033[1;34m[INFO]\033[0m %s\n' "$*"; }
success() { printf '\033[1;32m[SUCCESS]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[WARN]\033[0m %s\n' "$*"; }
error() { printf '\033[1;31m[ERROR]\033[0m %s\n' "$*" >&2; }

usage() {
    cat <<'USAGE'
Usage: ./install.sh [options]

Options:
  -y, --yes               Automatically answer "yes" to all prompts.
      --dry-run           Show the actions that would be taken without modifying the system.
      --module-dir DIR    Install the WingMAV module into DIR.
      --runner-target F   Install the wingmav-proxy helper script at F.
      --skip-apt          Do not attempt to install apt packages.
      --skip-dialout      Skip adding the invoking user to the dialout group.
      --skip-runner       Skip installing the wingmav-proxy helper script.
      --skip-checks       Skip the environment verification checks.
      --non-interactive   Assume defaults for all prompts (implies --yes for actions
                          whose default is "yes").
  -h, --help              Show this help message and exit.
USAGE
}

command_exists() { command -v "$1" >/dev/null 2>&1; }

require_command() {
    local cmd=$1
    if ! command_exists "$cmd"; then
        error "Required command '$cmd' was not found in PATH."
        exit 1
    fi
}

ASSUME_YES=false
DRY_RUN=false
SKIP_APT=false
SKIP_DIALOUT=false
SKIP_RUNNER=false
SKIP_CHECKS=false
NON_INTERACTIVE=false
MODULE_DIR_OVERRIDE=""
RUNNER_TARGET_OVERRIDE=""
SUDO=""
PRIVILEGE_AVAILABLE=false
PIP_INSTALL_MODE=""
VENV_PATH=""
PYTHON_BIN="python3"
PYTHON_CONTEXT_DESC="the system default python3"

CAN_PROMPT=true
if [[ ! -t 0 ]]; then
    CAN_PROMPT=false
fi

while [[ $# -gt 0 ]]; do
    case $1 in
        -y|--yes)
            ASSUME_YES=true
            ;;
        --dry-run)
            DRY_RUN=true
            ;;
        --module-dir)
            shift || { error "Missing value for --module-dir"; exit 1; }
            MODULE_DIR_OVERRIDE=$1
            ;;
        --module-dir=*)
            MODULE_DIR_OVERRIDE=${1#*=}
            ;;
        --runner-target)
            shift || { error "Missing value for --runner-target"; exit 1; }
            RUNNER_TARGET_OVERRIDE=$1
            ;;
        --runner-target=*)
            RUNNER_TARGET_OVERRIDE=${1#*=}
            ;;
        --skip-apt)
            SKIP_APT=true
            ;;
        --skip-dialout)
            SKIP_DIALOUT=true
            ;;
        --skip-runner)
            SKIP_RUNNER=true
            ;;
        --skip-checks)
            SKIP_CHECKS=true
            ;;
        --non-interactive)
            NON_INTERACTIVE=true
            CAN_PROMPT=false
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        *)
            error "Unknown option: $1"
            usage
            exit 1
            ;;
    esac
    shift
done

if $NON_INTERACTIVE; then
    ASSUME_YES=true
fi

prompt_yes_no() {
    local prompt=$1
    local default=${2:-Y}
    local default_lower=${default,,}
    local choices=""
    local default_answer=""

    if [[ $default_lower == y* ]]; then
        choices="Y/n"
        default_answer="y"
    else
        choices="y/N"
        default_answer="n"
    fi

    if $ASSUME_YES; then
        info "$prompt -> yes (auto)"
        return 0
    fi

    if ! $CAN_PROMPT; then
        [[ $default_lower == y* ]]
        return
    fi

    local reply
    while true; do
        read -rp "$prompt [$choices] " reply || reply=""
        reply=${reply:-$default_answer}
        case ${reply,,} in
            y|yes) return 0 ;;
            n|no) return 1 ;;
            *) echo "Please answer yes or no." ;;
        esac
    done
}

prompt_for_path() {
    local prompt=$1
    local default=$2

    if $ASSUME_YES || ! $CAN_PROMPT; then
        printf '%s\n' "$default"
        return
    fi

    local response
    read -rp "$prompt [$default]: " response || response=""
    response=${response:-$default}
    printf '%s\n' "$response"
}

expand_path() {
    python3 - "$1" <<'PY'
import os
import sys
path = sys.argv[1] if len(sys.argv) > 1 else ''
print(os.path.abspath(os.path.expanduser(path)))
PY
}

run_cmd() {
    if $DRY_RUN; then
        printf '    (dry-run) would run:'
        printf ' %q' "$@"
        printf '\n'
        return 0
    fi
    "$@"
}

verify_repository_contents() {
    local missing=()
    for name in "$MODULE_NAME" "$RUNNER_NAME"; do
        if [[ ! -f "$REPO_ROOT/$name" ]]; then
            missing+=("$name")
        fi
    done

    if ((${#missing[@]} > 0)); then
        error "The repository is missing required files: ${missing[*]}"
        error "Ensure you are running the installer from a complete WingMAV checkout."
        exit 1
    fi
}

init_privilege_helper() {
    if [[ $EUID -eq 0 ]]; then
        SUDO=""
        PRIVILEGE_AVAILABLE=true
        return
    fi

    if command_exists sudo; then
        SUDO="sudo"
        PRIVILEGE_AVAILABLE=true
    else
        SUDO=""
        PRIVILEGE_AVAILABLE=false
        warn "sudo not found; privileged operations that require root will be unavailable."
    fi
}

require_privilege() {
    local action=${1:-"This action"}
    if $PRIVILEGE_AVAILABLE; then
        return 0
    fi

    error "$action requires root privileges, but sudo is not available and the installer is not running as root."
    error "Re-run the installer with elevated permissions or skip this step using the provided flags."
    exit 1
}

ensure_directory() {
    local dir=$1
    if [[ -d $dir ]]; then
        return
    fi
    info "Creating directory $dir"
    if run_cmd mkdir -p "$dir"; then
        return
    fi

    if [[ -n ${SUDO:-} ]]; then
        info "Retrying directory creation with sudo"
        if run_cmd "$SUDO" mkdir -p "$dir"; then
            return
        fi
    fi

    if ! $PRIVILEGE_AVAILABLE; then
        error "Failed to create directory $dir (permission denied). Choose a writable location or re-run with sudo."
    else
        error "Failed to create directory $dir."
    fi
    exit 1
}

ensure_apt_dependencies() {
    if $SKIP_APT; then
        info "Skipping apt dependency installation as requested."
        return
    fi

    if ! command_exists apt-get; then
        warn "apt-get not available; skipping automatic package installation."
        return
    fi

    local packages=(python3 python3-pip python3-venv joystick)
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

    info "Missing apt packages detected: ${missing[*]}"
    if ! prompt_yes_no "Install missing packages via apt?" "Y"; then
        warn "Skipping apt package installation."
        return
    fi

    require_privilege "APT package installation"

    info "Updating apt package lists"
    if [[ -n ${SUDO:-} ]]; then
        run_cmd "$SUDO" apt-get update
        run_cmd "$SUDO" apt-get install -y "${missing[@]}"
    else
        run_cmd apt-get update
        run_cmd apt-get install -y "${missing[@]}"
    fi
}

pip_supports_break_system_packages() {
    local python_cmd=${1:-python3}
    if $DRY_RUN; then
        return 0
    fi
    if "$python_cmd" -m pip help install 2>/dev/null | grep -q -- "--break-system-packages"; then
        return 0
    fi
    return 1
}

configure_python_installation() {
    if [[ -n ${VIRTUAL_ENV:-} ]]; then
        VENV_PATH=$(expand_path "$VIRTUAL_ENV")
        PYTHON_BIN="$VENV_PATH/bin/python"
        PIP_INSTALL_MODE="active-venv"
        PYTHON_CONTEXT_DESC="the active virtual environment at $VENV_PATH"
        info "Detected active Python virtual environment at $VENV_PATH"
        return
    fi

    if $NON_INTERACTIVE || ! $CAN_PROMPT || $ASSUME_YES; then
        PIP_INSTALL_MODE="user"
        PYTHON_CONTEXT_DESC="the user site-packages directory (~/.local)"
        info "Automated mode; Python packages will be installed for the current user."
        return
    fi

    info "Select how WingMAV's Python dependencies should be installed:"
    echo "  1) Install for the current user (~/.local)"
    echo "  2) Create or reuse a dedicated virtual environment"

    local allow_system_break=false
    local prompt_range="[1-2]"
    if pip_supports_break_system_packages "$PYTHON_BIN"; then
        echo "  3) Install into the system Python using --break-system-packages"
        allow_system_break=true
        prompt_range="[1-3]"
    else
        echo "  (System-wide installation with --break-system-packages is unavailable; pip is too old.)"
    fi

    local choice
    while true; do
        read -rp "Enter selection $prompt_range: " choice || choice=""
        choice=${choice:-1}
        case $choice in
            1)
                PIP_INSTALL_MODE="user"
                PYTHON_CONTEXT_DESC="the user site-packages directory (~/.local)"
                break
                ;;
            2)
                local default_venv="$REPO_ROOT/.wingmav-venv"
                local venv_dir
                venv_dir=$(prompt_for_path "Virtual environment directory" "$default_venv")
                venv_dir=$(expand_path "$venv_dir")
                create_or_use_virtualenv "$venv_dir"
                PIP_INSTALL_MODE="venv"
                PYTHON_CONTEXT_DESC="the virtual environment at $VENV_PATH"
                break
                ;;
            3)
                if ! $allow_system_break; then
                    echo "Option 3 is not available with the current pip version."
                    continue
                fi
                PIP_INSTALL_MODE="system-break"
                PYTHON_CONTEXT_DESC="the system Python with --break-system-packages"
                break
                ;;
            *)
                echo "Please enter 1, 2, or 3."
                ;;
        esac
    done
}

create_or_use_virtualenv() {
    local path=$1
    if [[ -z $path ]]; then
        error "Virtual environment path cannot be empty."
        exit 1
    fi

    if [[ ! -d $path ]]; then
        info "Creating virtual environment at $path"
        if ! run_cmd python3 -m venv "$path"; then
            error "Failed to create virtual environment at $path. Ensure python3-venv is installed."
            exit 1
        fi
    else
        info "Using existing virtual environment at $path"
    fi

    VENV_PATH=$path
    if [[ ! -x "$VENV_PATH/bin/python" ]]; then
        error "Virtual environment at $VENV_PATH is missing its Python executable."
        exit 1
    fi
    PYTHON_BIN="$VENV_PATH/bin/python"
}

pip_install_packages() {
    local packages=("$@")
    if ((${#packages[@]} == 0)); then
        return 0
    fi

    local args=(install --upgrade)
    local pip_cmd=("$PYTHON_BIN" -m pip)
    local target_desc="$PYTHON_CONTEXT_DESC"
    local need_privilege=false

    case $PIP_INSTALL_MODE in
        active-venv|venv)
            target_desc="the virtual environment at $VENV_PATH"
            ;;
        user|'')
            args+=(--user)
            target_desc="the user site-packages directory (~/.local)"
            ;;
        system-break)
            args+=(--break-system-packages)
            target_desc="the system Python with --break-system-packages"
            if [[ $EUID -ne 0 ]]; then
                need_privilege=true
            fi
            ;;
        *)
            error "Unknown pip installation mode '$PIP_INSTALL_MODE'"
            exit 1
            ;;
    esac

    if [[ $PIP_INSTALL_MODE == "system-break" ]] && ! pip_supports_break_system_packages "$PYTHON_BIN"; then
        error "pip for $PYTHON_BIN does not support --break-system-packages. Choose a different installation mode."
        exit 1
    fi

    if $need_privilege; then
        require_privilege "System Python package installation"
        pip_cmd=("$SUDO" "$PYTHON_BIN" -m pip)
    fi

    info "Installing Python packages (${packages[*]}) into $target_desc"

    if ! run_cmd "${pip_cmd[@]}" "${args[@]}" "${packages[@]}"; then
        error "Failed to install Python packages via pip."
        exit 1
    fi
}

detect_missing_python_packages() {
    "$PYTHON_BIN" - <<'PY'
import importlib
import contextlib
import io


def import_silently(name: str):
    """Import ``name`` while swallowing noisy stdout/stderr."""

    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        return importlib.import_module(name)


requirements = {
    "MAVProxy": "mavproxy",
    "pymavlink": "pymavlink",
    "pygame": "pygame",
}
for module, package in requirements.items():
    try:
        import_silently(module)
    except Exception:
        print(package)
PY
}

ensure_python_packages() {
    local missing_packages=()
    mapfile -t missing_packages < <(detect_missing_python_packages || true)

    if ((${#missing_packages[@]} == 0)); then
        info "Required Python packages already available."
        return
    fi

    info "Missing Python packages detected: ${missing_packages[*]}"

    if ! "$PYTHON_BIN" -m pip --version >/dev/null 2>&1; then
        error "pip is not available for $PYTHON_BIN. Install python3-pip or ensure the selected environment has pip."
        exit 1
    fi

    if ! prompt_yes_no "Install missing Python packages via pip?" "Y"; then
        warn "Skipping pip installation of Python packages."
        return
    fi

    pip_install_packages "${missing_packages[@]}"
}

verify_python_environment() {
    info "Verifying Python environment …"
    if "$PYTHON_BIN" - <<'PY'
import importlib
import sys
import contextlib
import io


def import_silently(name: str):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        return importlib.import_module(name)


required = {
    "MAVProxy": "MAVProxy or mavproxy",
    "pymavlink": "pymavlink",
    "pygame": "pygame",
}
missing = []
for module, package in required.items():
    try:
        import_silently(module)
    except Exception as exc:
        missing.append(f"{module} (install via {package}): {exc}")
if missing:
    for message in missing:
        print(message)
    sys.exit(1)
PY
    then
        info "Python dependencies look good."
        return 0
    else
        warn "Python dependency verification failed."
        return 1
    fi
}

detect_existing_mavproxy_paths() {
    "$PYTHON_BIN" - <<'PY'
from pathlib import Path
import importlib.util
try:
    spec = importlib.util.find_spec("MAVProxy.modules")
except ModuleNotFoundError:
    spec = None
if spec and spec.submodule_search_locations:
    for location in spec.submodule_search_locations:
        print(Path(location).resolve())
PY
}

select_install_directory() {
    if [[ -n $MODULE_DIR_OVERRIDE ]]; then
        expand_path "$MODULE_DIR_OVERRIDE"
        return
    fi

    local detected_paths=()
    mapfile -t detected_paths < <(detect_existing_mavproxy_paths || true)

    if ((${#detected_paths[@]} > 0)); then
        info >&2 "Detected MAVProxy module paths:"
        for path in "${detected_paths[@]}"; do
            printf '  - %s\n' "$path" >&2
        done
    else
        warn >&2 "No MAVProxy module paths detected automatically."
    fi

    local default_path
    default_path="${MAVPROXY_HOME:-$HOME/.mavproxy}/modules"
    if ((${#detected_paths[@]} > 0)); then
        default_path=${detected_paths[0]}
    fi

    local chosen
    chosen=$(prompt_for_path "Directory to install the WingMAV module" "$default_path")
    expand_path "$chosen"
}

install_wingmav_module() {
    local target_dir=$1
    local target_file="$target_dir/$MODULE_NAME"

    info "Installing WingMAV module to $target_file"
    ensure_directory "$target_dir"
    local src="$REPO_ROOT/$MODULE_NAME"
    if run_cmd install -Dm644 "$src" "$target_file"; then
        return
    fi

    if [[ -n ${SUDO:-} ]]; then
        info "Retrying module installation with sudo"
        if run_cmd "$SUDO" install -Dm644 "$src" "$target_file"; then
            return
        fi
    fi

    if ! $PRIVILEGE_AVAILABLE; then
        error "Failed to install WingMAV module to $target_file. Choose a writable directory or re-run with sudo."
    else
        error "Failed to install WingMAV module to $target_file."
    fi
    exit 1
}

select_runner_target() {
    if [[ -n $RUNNER_TARGET_OVERRIDE ]]; then
        expand_path "$RUNNER_TARGET_OVERRIDE"
        return
    fi

    local default_target
    if [[ -z ${SUDO:-} && $EUID -ne 0 ]]; then
        default_target="$HOME/.local/bin/wingmav-proxy"
    else
        default_target="/usr/local/bin/wingmav-proxy"
    fi

    local chosen
    chosen=$(prompt_for_path "Location for the wingmav-proxy helper" "$default_target")
    expand_path "$chosen"
}

install_runner_script() {
    if $SKIP_RUNNER; then
        info "Skipping runner installation as requested."
        return
    fi

    if ! prompt_yes_no "Install the optional wingmav-proxy helper script?" "Y"; then
        info "Skipping runner installation."
        return
    fi

    local target
    target=$(select_runner_target)
    local target_dir
    target_dir=$(dirname "$target")
    ensure_directory "$target_dir"

    info "Installing wingmav-proxy helper to $target"
    local src="$REPO_ROOT/$RUNNER_NAME"
    if run_cmd install -Dm755 "$src" "$target"; then
        :
    elif [[ -n ${SUDO:-} ]]; then
        info "Retrying runner installation with sudo"
        if run_cmd "$SUDO" install -Dm755 "$src" "$target"; then
            :
        else
            if ! $PRIVILEGE_AVAILABLE; then
                error "Failed to install wingmav-proxy to $target. Choose a writable location or re-run with sudo."
            else
                error "Failed to install wingmav-proxy to $target."
            fi
            exit 1
        fi
    else
        if ! $PRIVILEGE_AVAILABLE; then
            error "Failed to install wingmav-proxy to $target. Choose a writable location or re-run with sudo."
        else
            error "Failed to install wingmav-proxy to $target."
        fi
        exit 1
    fi

    if [[ $target == "$HOME/.local/bin"/* ]]; then
        warn "Ensure $HOME/.local/bin is in your PATH."
    fi
}

configure_dialout_group() {
    if $SKIP_DIALOUT; then
        info "Skipping dialout group configuration as requested."
        return
    fi

    local target_user
    target_user=${SUDO_USER:-${USER:-}}
    if [[ -z $target_user ]]; then
        target_user=$(id -un 2>/dev/null || true)
    fi

    if [[ -z "$target_user" || "$target_user" == "root" ]]; then
        warn "Skipping dialout group configuration (no non-root user detected)."
        return
    fi

    if id -nG "$target_user" | grep -qw dialout; then
        info "User '$target_user' already belongs to the dialout group."
        return
    fi

    if ! prompt_yes_no "Add user '$target_user' to the dialout group?" "Y"; then
        warn "Dialout group update skipped. Serial devices may be inaccessible."
        return
    fi

    if ! $PRIVILEGE_AVAILABLE; then
        warn "Cannot modify group memberships without root privileges; skipping dialout group configuration."
        return
    fi

    info "Adding user '$target_user' to the dialout group"
    if [[ -n ${SUDO:-} ]]; then
        run_cmd "$SUDO" usermod -a -G dialout "$target_user"
    else
        run_cmd usermod -a -G dialout "$target_user"
    fi
    warn "User '$target_user' must log out and back in for dialout membership to take effect."
}

run_environment_checks() {
    if $SKIP_CHECKS; then
        info "Skipping environment checks as requested."
        return
    fi

    if command_exists mavproxy.py; then
        info "MAVProxy version:"
        if ! mavproxy.py --version; then
            warn "Unable to retrieve MAVProxy version."
        fi
    else
        warn "mavproxy.py not found in PATH. Install MAVProxy or adjust your PATH."
    fi

    info "Performing module import smoke test …"
    if "$PYTHON_BIN" - <<'PY'
import importlib
import contextlib
import io


def import_silently(name: str):
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(
        io.StringIO()
    ):
        return importlib.import_module(name)


modules = ["MAVProxy.modules.lib.mp_module", "pymavlink", "pygame"]
for name in modules:
    import_silently(name)
print("All required modules imported successfully.")
PY
    then
        info "Python import smoke test passed."
    else
        warn "Python import smoke test failed. Verify your Python environment."
    fi
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
    verify_repository_contents

    info "Welcome to the WingMAV setup wizard."
    if ! prompt_yes_no "Proceed with installation?" "Y"; then
        warn "Installation aborted by user."
        exit 0
    fi

    ensure_apt_dependencies

    configure_python_installation
    info "Python operations will target $PYTHON_CONTEXT_DESC."

    ensure_python_packages

    if ! verify_python_environment; then
        if prompt_yes_no "Continue despite missing Python dependencies?" "N"; then
            warn "Continuing despite Python dependency issues."
        else
            error "Python dependencies are missing. Install them and re-run the installer."
            exit 1
        fi
    fi

    local module_dir
    module_dir=$(select_install_directory)
    install_wingmav_module "$module_dir"

    install_runner_script

    configure_dialout_group

    run_environment_checks

    success "WingMAV installation steps completed."
    print_post_install_instructions
}

main "$@"
