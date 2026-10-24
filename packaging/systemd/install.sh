#!/usr/bin/env bash
# Install vibe-watcher as a systemd service (system or user mode).
#
# Usage:
#   sudo ./packaging/systemd/install.sh --system [--install-dir /opt/vibe-watcher]
#   ./packaging/systemd/install.sh --user
#
set -euo pipefail

MODE="${1:---system}"
INSTALL_DIR="${2:-}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(dirname "$SCRIPT_DIR")"

if [[ "$MODE" == "--system" ]]; then
    if [[ $EUID -ne 0 ]]; then
        echo "ERROR: --system mode must run as root (sudo)." >&2
        exit 1
    fi
    INSTALL_DIR="${INSTALL_DIR:-/opt/vibe-watcher}"

    echo "==> Installing project files to $INSTALL_DIR"
    mkdir -p "$INSTALL_DIR"
    rsync -a --exclude '.git' --exclude 'db.sqlite3' "$REPO_DIR"/ "$INSTALL_DIR"/

    echo "==> Creating user/group vibe-watcher (configurable in the unit file)"
    id vibe-watcher &>/dev/null || useradd --system --create-home --shell /usr/sbin/nologin vibe-watcher

    echo "==> Writing environment file /etc/vibe-watcher/env"
    install -d -m 0750 -o root -g vibe-watcher /etc/vibe-watcher
    if [[ ! -f /etc/vibe-watcher/env ]]; then
        install -m 0640 -o root -g vibe-watcher "$SCRIPT_DIR/env.example" /etc/vibe-watcher/env
    fi

    echo "==> Installing unit file"
    install -m 0644 "$SCRIPT_DIR/vibe-watcher.service" /etc/systemd/system/vibe-watcher.service

    echo "==> Reloading systemd and enabling service"
    systemctl daemon-reload
    systemctl enable --now vibe-watcher.service

    echo "==> Done. Check status/logs with:"
    echo "    systemctl status vibe-watcher"
    echo "    journalctl -u vibe-watcher -f"
    echo "Remember: edit User=/Group= in the unit to a user that can read your repos."

elif [[ "$MODE" == "--user" ]]; then
    CONFIG_DIR="$HOME/.config/vibe-watcher"

    echo "==> Writing environment file $CONFIG_DIR/env"
    mkdir -p "$CONFIG_DIR"
    if [[ ! -f "$CONFIG_DIR/env" ]]; then
        cp "$SCRIPT_DIR/env.example" "$CONFIG_DIR/env"
    fi

    echo "==> Installing user unit to $HOME/.config/systemd/user/"
    mkdir -p "$HOME/.config/systemd/user"
    install -m 0644 "$SCRIPT_DIR/vibe-watcher-user.service" \
        "$HOME/.config/systemd/user/vibe-watcher.service"

    # Allow the unit to keep running after logout:
    loginctl enable-linger "$USER" 2>/dev/null || true

    echo "==> Reloading user systemd and enabling service"
    systemctl --user daemon-reload
    systemctl --user enable --now vibe-watcher.service

    echo "==> Done. Check status/logs with:"
    echo "    systemctl --user status vibe-watcher"
    echo "    journalctl --user -u vibe-watcher -f"
    echo "Make sure the code is at $HOME/vibe-watcher or adjust ExecStart/WorkingDirectory."

else
    echo "Usage: $0 [--system|--user]" >&2
    exit 1
fi
