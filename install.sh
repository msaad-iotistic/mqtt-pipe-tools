#!/bin/bash
#
# install.sh - Install mqtt-wormhole and mqttcat globally
#
# Usage:
#   ./install.sh           # Install for current user
#   sudo ./install.sh      # Install system-wide
#   ./install.sh --uninstall
#

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m' # No Color

# Script locations
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORMHOLE_SCRIPT="$SCRIPT_DIR/mqtt-wormhole.py"
MQTTCAT_SCRIPT="$SCRIPT_DIR/mqttcat.py"
REQUIREMENTS="$SCRIPT_DIR/requirements.txt"
VENV_DIR="$SCRIPT_DIR/venv"

# Known system Python environment
SYSTEM_PYTHON="/opt/iotistic-mnvr/mnvrenv/bin/python"

# Target command names
WORMHOLE_CMD="mqtt-wormhole"
MQTTCAT_CMD="mqttcat"

# Will be set during install
PYTHON_BIN=""

# ─── HELPER FUNCTIONS ───────────────────────────────────────────────────────

print_header() {
    echo ""
    if [ "$EUID" -eq 0 ]; then
        echo -e "${CYAN}${BOLD}🔧 MQTT Tools Installer (running as root)${NC}"
    else
        echo -e "${CYAN}${BOLD}🔧 MQTT Tools Installer${NC}"
    fi
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""
}

print_success() {
    echo -e "${GREEN}✓${NC} $1"
}

print_error() {
    echo -e "${RED}✗${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}!${NC} $1"
}

print_info() {
    echo -e "${BLUE}→${NC} $1"
}

# ─── PYTHON ENVIRONMENT ────────────────────────────────────────────────────

detect_python() {
    # Priority 1: System python env at known path
    if [ -x "$SYSTEM_PYTHON" ]; then
        PYTHON_BIN="$SYSTEM_PYTHON"
        print_success "Using system Python: $SYSTEM_PYTHON"
        return 0
    fi

    # Priority 2: Existing venv in project
    if [ -x "$VENV_DIR/bin/python" ]; then
        PYTHON_BIN="$VENV_DIR/bin/python"
        print_success "Using existing venv: $VENV_DIR"
        return 0
    fi

    # Priority 3: Create a new venv
    local base_python=""
    if command -v python3 &>/dev/null; then
        base_python="python3"
    elif command -v python &>/dev/null; then
        base_python="python"
    else
        print_error "Python not found. Install Python 3 first."
        return 1
    fi

    print_info "Creating venv at $VENV_DIR ..."
    $base_python -m venv "$VENV_DIR"
    PYTHON_BIN="$VENV_DIR/bin/python"
    print_success "Created venv: $VENV_DIR"
    return 0
}

install_dependencies() {
    if [ ! -f "$REQUIREMENTS" ]; then
        print_warning "requirements.txt not found, skipping dependencies"
        return
    fi

    # Determine pip from our detected python
    local pip_cmd=""
    local python_dir=$(dirname "$PYTHON_BIN")

    if [ -x "$python_dir/pip" ]; then
        pip_cmd="$python_dir/pip"
    elif [ -x "$python_dir/pip3" ]; then
        pip_cmd="$python_dir/pip3"
    else
        # Fall back to running pip as module
        pip_cmd="$PYTHON_BIN -m pip"
    fi

    print_info "Installing dependencies into: $(dirname "$PYTHON_BIN")/"
    if $pip_cmd install -r "$REQUIREMENTS" -q 2>/dev/null; then
        print_success "Dependencies installed"
    else
        print_warning "Some dependencies may have failed to install"
        print_info "Try manually: $pip_cmd install -r $REQUIREMENTS"
    fi
}

# ─── DIRECTORY DETECTION ────────────────────────────────────────────────────

detect_bin_dir() {
    if [ "$EUID" -eq 0 ]; then
        echo "/usr/local/bin"
        return
    fi

    # Try ~/.local/bin first (XDG standard)
    if [ -d "$HOME/.local/bin" ]; then
        echo "$HOME/.local/bin"
        return
    fi

    # Try ~/bin
    if [ -d "$HOME/bin" ]; then
        echo "$HOME/bin"
        return
    fi

    # Create ~/.local/bin (preferred)
    mkdir -p "$HOME/.local/bin" 2>/dev/null && echo "$HOME/.local/bin" && return

    # Fallback to ~/bin
    mkdir -p "$HOME/bin" 2>/dev/null && echo "$HOME/bin" && return

    echo ""
}

check_in_path() {
    local dir="$1"
    case ":$PATH:" in
        *":$dir:"*) return 0 ;;
        *) return 1 ;;
    esac
}

get_shell_rc() {
    local shell_name=$(basename "$SHELL")
    case "$shell_name" in
        zsh)  echo "$HOME/.zshrc" ;;
        bash) echo "$HOME/.bashrc" ;;
        fish) echo "$HOME/.config/fish/config.fish" ;;
        *)    echo "$HOME/.profile" ;;
    esac
}

# ─── WRAPPER SCRIPTS ───────────────────────────────────────────────────────

check_scripts_exist() {
    local missing=0
    if [ ! -f "$WORMHOLE_SCRIPT" ]; then
        print_error "mqtt-wormhole.py not found at: $WORMHOLE_SCRIPT"
        missing=1
    fi
    if [ ! -f "$MQTTCAT_SCRIPT" ]; then
        print_error "mqttcat.py not found at: $MQTTCAT_SCRIPT"
        missing=1
    fi
    return $missing
}

make_executable() {
    chmod +x "$WORMHOLE_SCRIPT" 2>/dev/null && print_success "Made mqtt-wormhole.py executable"
    chmod +x "$MQTTCAT_SCRIPT" 2>/dev/null && print_success "Made mqttcat.py executable"
}

create_wrapper() {
    local python="$1"
    local script="$2"
    local target="$3"
    local name="$4"

    # Check if target already exists
    if [ -e "$target" ]; then
        # Check if it's our wrapper (contains our script path)
        if grep -q "$script" "$target" 2>/dev/null; then
            print_success "Wrapper already exists: $name (updating)"
        else
            print_warning "$target already exists"
            read -p "         Overwrite? [y/N] " -n 1 -r
            echo
            if [[ ! $REPLY =~ ^[Yy]$ ]]; then
                print_info "Skipped: $name"
                return 1
            fi
        fi
    fi

    # Create wrapper script
    cat > "$target" <<EOF
#!/bin/bash
exec "$python" "$script" "\$@"
EOF
    chmod +x "$target"

    if [ -x "$target" ]; then
        print_success "Created wrapper: $name → $script"
        return 0
    else
        print_error "Failed to create wrapper: $name"
        return 1
    fi
}

# ─── INSTALLATION ───────────────────────────────────────────────────────────

verify_installation() {
    echo ""
    local success=0

    if command -v "$WORMHOLE_CMD" &>/dev/null; then
        print_success "mqtt-wormhole installed at: $(command -v "$WORMHOLE_CMD")"
    else
        print_warning "mqtt-wormhole not found in PATH (may need to restart shell)"
        success=1
    fi

    if command -v "$MQTTCAT_CMD" &>/dev/null; then
        print_success "mqttcat installed at: $(command -v "$MQTTCAT_CMD")"
    else
        print_warning "mqttcat not found in PATH (may need to restart shell)"
        success=1
    fi

    return $success
}

do_install() {
    print_header

    # Check scripts exist
    if ! check_scripts_exist; then
        echo ""
        print_error "Installation failed: required scripts not found"
        exit 1
    fi

    # Detect/create Python environment
    if ! detect_python; then
        exit 1
    fi

    # Install dependencies
    install_dependencies

    # Make scripts executable
    echo ""
    make_executable

    # Detect bin directory
    local bin_dir=$(detect_bin_dir)
    if [ -z "$bin_dir" ]; then
        print_error "Could not find or create a suitable bin directory"
        exit 1
    fi
    print_success "Commands directory: $bin_dir"

    # Check if in PATH
    if ! check_in_path "$bin_dir"; then
        print_warning "$bin_dir is not in your PATH"
        local rc_file=$(get_shell_rc)
        echo ""
        print_info "Add this line to $rc_file:"
        echo -e "         ${BOLD}export PATH=\"\$PATH:$bin_dir\"${NC}"
        echo ""
        print_info "Then run: source $rc_file"
        echo ""
    else
        print_success "Directory is in PATH"
    fi

    # Create wrapper scripts
    echo ""
    create_wrapper "$PYTHON_BIN" "$WORMHOLE_SCRIPT" "$bin_dir/$WORMHOLE_CMD" "$WORMHOLE_CMD"
    create_wrapper "$PYTHON_BIN" "$MQTTCAT_SCRIPT" "$bin_dir/$MQTTCAT_CMD" "$MQTTCAT_CMD"

    # Verify
    verify_installation

    # Success message
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}${BOLD}✅ Installation complete!${NC}"
    echo ""
    echo -e "Python: ${BOLD}$PYTHON_BIN${NC}"
    echo ""
    echo "You can now use:"
    echo -e "  ${BOLD}mqtt-wormhole myfile.pdf${NC}              # Send a file"
    echo -e "  ${BOLD}mqtt-wormhole --code 7-cosmic-dolphin${NC} # Receive a file"
    echo -e "  ${BOLD}mqttcat listen prefix profiles.json profile${NC}"
    echo ""
    echo "Run 'mqtt-wormhole --help' for usage information."
    echo ""
}

# ─── UNINSTALLATION ─────────────────────────────────────────────────────────

find_commands() {
    local locations=("/usr/local/bin" "$HOME/.local/bin" "$HOME/bin")
    local found=()

    for dir in "${locations[@]}"; do
        if [ -e "$dir/$WORMHOLE_CMD" ] || [ -e "$dir/$MQTTCAT_CMD" ]; then
            found+=("$dir")
        fi
    done

    # Also check PATH
    local wormhole_path=$(command -v "$WORMHOLE_CMD" 2>/dev/null)
    if [ -n "$wormhole_path" ]; then
        local wormhole_dir=$(dirname "$wormhole_path")
        if [[ ! " ${found[*]} " =~ " ${wormhole_dir} " ]]; then
            found+=("$wormhole_dir")
        fi
    fi

    echo "${found[@]}"
}

do_uninstall() {
    echo ""
    echo -e "${CYAN}${BOLD}🗑️  Uninstalling MQTT Tools${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo ""

    local dirs=$(find_commands)
    local removed=0

    if [ -z "$dirs" ]; then
        print_warning "No commands found to remove"
        return
    fi

    for dir in $dirs; do
        if [ -e "$dir/$WORMHOLE_CMD" ]; then
            if rm -f "$dir/$WORMHOLE_CMD" 2>/dev/null; then
                print_success "Removed: $dir/$WORMHOLE_CMD"
                ((removed++))
            else
                print_error "Failed to remove: $dir/$WORMHOLE_CMD (try with sudo)"
            fi
        fi

        if [ -e "$dir/$MQTTCAT_CMD" ]; then
            if rm -f "$dir/$MQTTCAT_CMD" 2>/dev/null; then
                print_success "Removed: $dir/$MQTTCAT_CMD"
                ((removed++))
            else
                print_error "Failed to remove: $dir/$MQTTCAT_CMD (try with sudo)"
            fi
        fi
    done

    echo ""
    if [ $removed -gt 0 ]; then
        echo -e "${GREEN}${BOLD}✅ Uninstall complete!${NC}"
    else
        print_warning "No commands were removed"
    fi
    echo ""
}

# ─── MAIN ───────────────────────────────────────────────────────────────────

show_help() {
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Install mqtt-wormhole and mqttcat commands globally."
    echo ""
    echo "Options:"
    echo "  --uninstall    Remove installed commands"
    echo "  --help, -h     Show this help message"
    echo ""
    echo "Python detection order:"
    echo "  1. $SYSTEM_PYTHON (if exists)"
    echo "  2. Existing venv at $VENV_DIR"
    echo "  3. Create new venv at $VENV_DIR"
    echo ""
    echo "Examples:"
    echo "  $0              # Install for current user"
    echo "  sudo $0         # Install system-wide to /usr/local/bin"
    echo "  $0 --uninstall  # Remove commands"
    echo ""
}

main() {
    case "${1:-}" in
        --uninstall)
            do_uninstall
            ;;
        --help|-h)
            show_help
            ;;
        "")
            do_install
            ;;
        *)
            print_error "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
