#!/bin/bash
#
# quick-install.sh - One-line installer for mqtt-pipe-tools
#
# Usage:
#   curl -sSL https://raw.githubusercontent.com/msaad-iotistic/mqtt-pipe-tools/main/scripts/quick-install.sh | bash
#
# Custom install location:
#   MQTT_TOOLS_DIR=~/tools/mqtt-pipe-tools bash <(curl -sSL https://raw.githubusercontent.com/msaad-iotistic/mqtt-pipe-tools/main/scripts/quick-install.sh) | bash
#

set -e

INSTALL_DIR="${MQTT_TOOLS_DIR:-$HOME/.local/share/mqtt-pipe-tools}"
REPO_URL="https://github.com/msaad-iotistic/mqtt-pipe-tools.git"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
BOLD='\033[1m'
NC='\033[0m'

print_success() { echo -e "${GREEN}✓${NC} $1"; }
print_error() { echo -e "${RED}✗${NC} $1"; }
print_warning() { echo -e "${YELLOW}!${NC} $1"; }
print_info() { echo -e "${CYAN}→${NC} $1"; }

echo ""
echo -e "${CYAN}${BOLD}🚀 mqtt-pipe-tools Quick Installer${NC}"
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

# Check for git
if ! command -v git &>/dev/null; then
    print_error "git is not installed"
    echo "Please install git first:"
    echo "  Ubuntu/Debian: sudo apt install git"
    echo "  macOS:         brew install git"
    exit 1
fi

# Check if already installed via symlinks/wrappers
check_existing_install() {
    local cmd_path=""
    local install_location=""
    
    # Check if mqtt-wormhole command exists
    if command -v mqtt-wormhole &>/dev/null; then
        cmd_path=$(command -v mqtt-wormhole)
        
        # If it's a symlink, follow it
        if [ -L "$cmd_path" ]; then
            install_location=$(readlink -f "$cmd_path" 2>/dev/null || readlink "$cmd_path")
        elif [ -f "$cmd_path" ]; then
            # It's a wrapper script - extract the path from exec line
            # Wrapper format: exec "/path/to/python" "/path/to/mqtt_wormhole.py" "$@"
            install_location=$(grep -oE '"/[^" ]+/mqtt_wormhole\.py"' "$cmd_path" 2>/dev/null | tr -d '"' | head -1 || echo "")
            if [ -z "$install_location" ]; then
                # Try without quotes
                install_location=$(grep -oE '/[^ ]+/mqtt_wormhole\.py' "$cmd_path" 2>/dev/null | head -1 || echo "")
            fi
        fi
        
        if [ -n "$install_location" ]; then
            # Get the directory containing the scripts
            if [[ "$install_location" == *"/mqtt_wormhole.py" ]]; then
                install_location=$(dirname "$install_location")
            fi
            echo "$install_location"
            return 0
        fi
    fi
    
    return 1
}

existing_install=$(check_existing_install || echo "")

if [ -n "$existing_install" ]; then
    print_info "Found existing installation at: ${BOLD}$existing_install${NC}"
    
    # Check if it's different from our target
    if [ "$existing_install" != "$INSTALL_DIR" ]; then
        print_warning "Existing installation is at a different location"
        echo "  Current: $existing_install"
        echo "  Target:  $INSTALL_DIR"
        echo ""
        read -p "Update existing installation at $existing_install? [Y/n] " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Nn]$ ]]; then
            INSTALL_DIR="$existing_install"
        else
            print_info "Installing to new location: $INSTALL_DIR"
        fi
    fi
fi

# Clone or update
if [ -d "$INSTALL_DIR" ]; then
    if [ -d "$INSTALL_DIR/.git" ]; then
        print_info "Updating repository at $INSTALL_DIR..."
        cd "$INSTALL_DIR"
        if git pull; then
            print_success "Repository updated"
        else
            print_warning "git pull failed, continuing with existing version"
        fi
    else
        print_warning "Directory exists but is not a git repository"
        read -p "Remove and re-clone? [y/N] " -n 1 -r
        echo
        if [[ $REPLY =~ ^[Yy]$ ]]; then
            rm -rf "$INSTALL_DIR"
            print_info "Cloning repository to $INSTALL_DIR..."
            mkdir -p "$(dirname "$INSTALL_DIR")"
            git clone "$REPO_URL" "$INSTALL_DIR"
            print_success "Repository cloned"
        else
            print_error "Cannot proceed without a valid git repository"
            exit 1
        fi
    fi
else
    print_info "Cloning repository to $INSTALL_DIR..."
    mkdir -p "$(dirname "$INSTALL_DIR")"
    git clone "$REPO_URL" "$INSTALL_DIR"
    print_success "Repository cloned"
fi

# Run installer
cd "$INSTALL_DIR"
echo ""
print_info "Running installer..."
echo ""

if [ -x "./install.sh" ]; then
    ./install.sh
else
    chmod +x ./install.sh
    ./install.sh
fi

echo ""
echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${GREEN}${BOLD}✅ Quick install complete!${NC}"
echo ""
echo -e "Installation location: ${BOLD}$INSTALL_DIR${NC}"
echo ""
echo "To get started:"
echo -e "  ${BOLD}mqtt-wormhole --help${NC}"
echo ""
