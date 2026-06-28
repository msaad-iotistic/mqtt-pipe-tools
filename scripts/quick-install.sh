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
            # Look for any path containing mqtt-pipe-tools
            install_location=$(grep -oE '/[^"]+mqtt-pipe-tools/[^"]*' "$cmd_path" 2>/dev/null | head -1 || echo "")
            if [ -n "$install_location" ]; then
                # Extract the directory containing mqtt-pipe-tools
                if [[ "$install_location" == *"/mqtt-pipe-tools"* ]]; then
                    install_location=$(echo "$install_location" | sed 's|/mqtt-pipe-tools/.*|/mqtt-pipe-tools|')
                fi
            fi
        fi
        
        if [ -n "$install_location" ]; then
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
        if git fetch origin 2>/dev/null && git reset --hard origin/main 2>/dev/null; then
            print_success "Repository updated"
        else
            print_error "Failed to update repository — cannot proceed"
            exit 1
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

# Remove broken symlinks and old wrappers from previous versions
remove_old_links() {
    local removed=0
    local bin_locations=("$HOME/.local/bin" "$HOME/bin" "/usr/local/bin")
    
    print_info "Checking for old mqtt-pipe-tools links and wrappers..."
    
    for bin_dir in "${bin_locations[@]}"; do
        if [ ! -d "$bin_dir" ]; then
            continue
        fi
        
        # Check all items in bin directory
        for item in "$bin_dir"/*; do
            if [ ! -e "$item" ] && [ ! -L "$item" ]; then
                continue
            fi
            
            # Check if it's a broken symlink pointing to mqtt-pipe-tools
            if [ -L "$item" ] && [ ! -e "$item" ]; then
                local target=$(readlink "$item" 2>/dev/null || echo "")
                if [[ "$target" == *"mqtt-pipe-tools"* ]]; then
                    if rm -f "$item" 2>/dev/null; then
                        print_success "Removed broken symlink: $(basename "$item")"
                        ((removed++))
                    else
                        print_warning "Could not remove broken symlink: $(basename "$item") (may need sudo)"
                    fi
                fi
            fi
            
            # Check if it's a wrapper script pointing to mqtt-pipe-tools
            if [ -f "$item" ] && grep -q "mqtt-pipe-tools" "$item" 2>/dev/null; then
                # Check if it points to old filenames (mqtt-wormhole.py or mqttcat.py)
                if grep -qE "mqtt-wormhole\.py|mqttcat\.py" "$item" 2>/dev/null; then
                    if rm -f "$item" 2>/dev/null; then
                        print_success "Removed old wrapper: $(basename "$item")"
                        ((removed++))
                    else
                        print_warning "Could not remove old wrapper: $(basename "$item") (may need sudo)"
                    fi
                fi
            fi
        done
    done
    
    if [ $removed -gt 0 ]; then
        print_success "Cleaned up $removed old link(s)/wrapper(s)"
    else
        print_success "No old links or wrappers found"
    fi
}

# Run cleanup
remove_old_links
echo ""

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
echo -e "  ${BOLD}mqtt-forward --help${NC}"
echo ""
