#!/bin/sh
set -eu

# Remove the synchronizer only.  The selected RSSHub Compose deployment, its
# live secret env file, candidate directory, and monitor state are deliberately
# retained so RSSHub keeps running and a later reinstall can recover them.

PATH=/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LC_ALL=C
export LC_ALL

die() {
    echo "rsshub-cookie-sync uninstall: $*" >&2
    exit 1
}

if [ "$(id -u)" -ne 0 ]; then
    die "uninstall.sh must run as root"
fi

SYSTEMD_AVAILABLE=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    SYSTEMD_AVAILABLE=1
fi

SERVICE_UNIT=/etc/systemd/system/rsshub-cookie-sync-monitor.service
TIMER_UNIT=/etc/systemd/system/rsshub-cookie-sync-monitor.timer
DROPIN_DIR=/etc/systemd/system/rsshub-cookie-sync-monitor.service.d
DROPIN_FILE=$DROPIN_DIR/deployment.conf
SSH_CONFIG=/etc/ssh/sshd_config.d/rsshub-cookie-sync.conf
SSH_USER=rsshub-sync
SSH_HOME=/var/lib/rsshub-sync

assert_removable_file() {
    target=$1
    [ ! -L "$target" ] || die "refusing to remove symlink: $target"
    [ -f "$target" ] || die "managed path is not a regular file: $target"
    if command -v stat >/dev/null 2>&1; then
        owner=$(stat -c '%u' -- "$target") || die "cannot inspect managed file: $target"
        [ "$owner" = 0 ] || die "managed file is not root-owned: $target"
    fi
}

assert_removable_dir() {
    target=$1
    [ ! -L "$target" ] || die "refusing to remove symlink: $target"
    [ -d "$target" ] || die "managed path is not a directory: $target"
    if command -v stat >/dev/null 2>&1; then
        owner=$(stat -c '%u' -- "$target") || die "cannot inspect managed directory: $target"
        [ "$owner" = 0 ] || die "managed directory is not root-owned: $target"
    fi
}

if [ "$SYSTEMD_AVAILABLE" -eq 1 ]; then
    systemctl disable --now rsshub-cookie-sync-monitor.timer >/dev/null 2>&1 || true
    systemctl stop rsshub-cookie-sync-monitor.service >/dev/null 2>&1 || true

    for unit_path in "$SERVICE_UNIT" "$TIMER_UNIT"; do
        if [ -L "$unit_path" ]; then
            die "refusing to remove symlink: $unit_path"
        fi
        if [ -e "$unit_path" ]; then
            assert_removable_file "$unit_path"
            rm -f "$unit_path"
        fi
    done

    if [ -L "$DROPIN_DIR" ]; then
        die "refusing to inspect symlinked systemd drop-in directory"
    fi
    if [ -e "$DROPIN_DIR" ]; then
        assert_removable_dir "$DROPIN_DIR"
        if [ -e "$DROPIN_FILE" ] || [ -L "$DROPIN_FILE" ]; then
            assert_removable_file "$DROPIN_FILE"
            rm -f "$DROPIN_FILE"
        fi
        # Do not remove unrelated drop-ins in this directory.
        rmdir "$DROPIN_DIR" 2>/dev/null || true
    fi
    systemctl daemon-reload >/dev/null 2>&1 || true
fi

for managed_file in \
    /usr/local/sbin/rsshub-cookie-sync-apply \
    /usr/local/sbin/rsshub-cookie-sync-provision-key \
    /etc/sudoers.d/rsshub-cookie-sync; do
    if [ -e "$managed_file" ] || [ -L "$managed_file" ]; then
        assert_removable_file "$managed_file"
        rm -f "$managed_file"
    fi
done

if [ -e "$SSH_CONFIG" ] || [ -L "$SSH_CONFIG" ]; then
    assert_removable_file "$SSH_CONFIG"
    rm -f "$SSH_CONFIG"
    if command -v sshd >/dev/null 2>&1 && sshd -t >/dev/null 2>&1; then
        if [ "$SYSTEMD_AVAILABLE" -eq 1 ]; then
            systemctl reload sshd >/dev/null 2>&1 || systemctl reload ssh >/dev/null 2>&1 || true
        fi
    else
        echo "rsshub-cookie-sync uninstall: sshd validation failed after removing its managed rule" >&2
    fi
fi

INSTALL_DIR=/usr/local/lib/rsshub-cookie-sync
if [ -e "$INSTALL_DIR" ] || [ -L "$INSTALL_DIR" ]; then
    assert_removable_dir "$INSTALL_DIR"
    for managed_file in \
        "$INSTALL_DIR/rsshub_cookie_sync.py" \
        "$INSTALL_DIR/rsshub-cookie-sync"; do
        if [ -e "$managed_file" ] || [ -L "$managed_file" ]; then
            assert_removable_file "$managed_file"
            rm -f "$managed_file"
        fi
    done
    # Preserve the directory if an administrator placed unrelated files in
    # it; the uninstaller owns only the exact filenames above.
    rmdir "$INSTALL_DIR" 2>/dev/null || true
fi

CONFIG_DIR=/etc/rsshub-cookie-sync
CONFIG_FILE=$CONFIG_DIR/config.json
if [ -e "$CONFIG_DIR" ] || [ -L "$CONFIG_DIR" ]; then
    assert_removable_dir "$CONFIG_DIR"
    if [ -e "$CONFIG_FILE" ] || [ -L "$CONFIG_FILE" ]; then
        assert_removable_file "$CONFIG_FILE"
        rm -f "$CONFIG_FILE"
    fi
    rmdir "$CONFIG_DIR" 2>/dev/null || true
fi

if id "$SSH_USER" >/dev/null 2>&1; then
    sync_home=$(getent passwd "$SSH_USER" | awk -F: 'NR == 1 { print $6 }')
    if [ -z "$sync_home" ] || [ "$sync_home" = / ] || [ ! -d "$sync_home" ] || [ -L "$sync_home" ]; then
        die "cannot safely determine $SSH_USER home; refusing to remove the account"
    fi
    if [ "$sync_home" != "$SSH_HOME" ]; then
        die "$SSH_USER home is unexpected; refusing to remove the account"
    fi
    if [ -L "$sync_home/.ssh" ]; then
        die "$SSH_USER .ssh path is a symlink; refusing to remove the account"
    fi
    if [ -e "$sync_home/.ssh" ] && [ ! -d "$sync_home/.ssh" ]; then
        die "$SSH_USER .ssh path is not a directory; refusing to remove the account"
    fi
    rm -f "$sync_home/.ssh/authorized_keys"
    if ! userdel --remove "$SSH_USER"; then
        die "failed to remove $SSH_USER account; RSSHub was left untouched"
    fi
fi

echo "RSSHub Cookie sync server uninstalled; RSSHub, live secrets, candidates, and state were left untouched."
