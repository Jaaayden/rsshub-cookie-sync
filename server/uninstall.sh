#!/bin/sh
set -eu

# Remove only RSSHub Cookie Sync.  The RSSHub Compose file, running containers,
# volumes, and the live secret env file are deliberately outside this script's
# deletion set.  This command is safe to run repeatedly.

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LC_ALL=C
export LC_ALL

usage() {
    cat >&2 <<'EOF'
用法：
  rsshub-cookie-sync-uninstall [--yes]

默认会显示删除范围并从终端确认；--yes 用于已确认范围的无人值守卸载。
卸载只移除 Cookie Sync，不停止 RSSHub，也不删除 Compose 或 secrets/rsshub.env。
EOF
}

die() {
    echo "rsshub-cookie-sync uninstall: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "缺少必要命令：$1"
}

if [ "$(id -u)" -ne 0 ]; then
    die "uninstall.sh must run as root"
fi

for command_name in id getent userdel stat sed awk dirname basename install rm rmdir cp chmod chown mktemp cmp ls mv; do
    require_command "$command_name"
done
[ -x /usr/bin/python3 ] || die "缺少必要命令：/usr/bin/python3"

ASSUME_YES=0
while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes|--force)
            ASSUME_YES=1
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            usage
            die "未知参数：$1"
            ;;
    esac
done

SERVICE_NAME=rsshub-cookie-sync-monitor.service
TIMER_NAME=rsshub-cookie-sync-monitor.timer
SERVICE_UNIT=/etc/systemd/system/$SERVICE_NAME
TIMER_UNIT=/etc/systemd/system/$TIMER_NAME
DROPIN_DIR=/etc/systemd/system/rsshub-cookie-sync-monitor.service.d
DROPIN_FILE=$DROPIN_DIR/deployment.conf
SSH_CONFIG=/etc/ssh/sshd_config.d/rsshub-cookie-sync.conf
SSH_USER=rsshub-sync
SSH_HOME=/var/lib/rsshub-sync
AUTHORIZED_KEYS=$SSH_HOME/.ssh/authorized_keys
INSTALL_DIR=/usr/local/lib/rsshub-cookie-sync
CONFIG_DIR=/etc/rsshub-cookie-sync
CONFIG_FILE=$CONFIG_DIR/config.json
ACCOUNT_MARKER=$CONFIG_DIR/account-created
INSTALL_MARKER=$CONFIG_DIR/install-manifest
SSH_CONFIG_PERSISTENT_BACKUP=$CONFIG_DIR/sshd-config.backup
SSH_CONFIG_BACKUP_MARKER=$CONFIG_DIR/sshd-config.backup.present
STATE_DIR=/var/lib/rsshub-cookie-sync
STATE_FILE=$STATE_DIR/state.json
LOCK_FILE=$STATE_DIR/lock
ssh_config_backup=

SYSTEMD_AVAILABLE=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ]; then
    SYSTEMD_AVAILABLE=1
fi

assert_removable_file() {
    target=$1
    [ ! -L "$target" ] || die "拒绝删除符号链接：$target"
    [ -f "$target" ] || die "托管路径不是普通文件：$target"
    owner=$(stat -c '%u' -- "$target") || die "无法检查托管文件：$target"
    [ "$owner" = 0 ] || die "托管文件不是 root 所有：$target"
    mode=$(stat -c '%A' -- "$target") || die "无法检查托管文件权限：$target"
    case "$mode" in
        ?????w????|????????w?) die "托管文件不能被组或其他用户写入：$target" ;;
    esac
}

assert_removable_dir() {
    target=$1
    [ ! -L "$target" ] || die "拒绝删除符号链接目录：$target"
    [ -d "$target" ] || die "托管路径不是目录：$target"
    owner=$(stat -c '%u' -- "$target") || die "无法检查托管目录：$target"
    [ "$owner" = 0 ] || die "托管目录不是 root 所有：$target"
    mode=$(stat -c '%A' -- "$target") || die "托管目录不能被组或其他用户写入：$target"
    case "$mode" in
        ?????w????|????????w?) die "托管目录不能被组或其他用户写入：$target" ;;
    esac
}

assert_safe_dir_chain() {
    chain_path=$1
    while :; do
        assert_removable_dir "$chain_path"
        [ "$chain_path" = / ] && break
        next_path=$(dirname -- "$chain_path")
        [ "$next_path" != "$chain_path" ] || die "无法检查路径父目录：$chain_path"
        chain_path=$next_path
    done
}

assert_auth_file() {
    [ ! -L "$AUTHORIZED_KEYS" ] || die "拒绝操作符号链接 authorized_keys"
    [ -f "$AUTHORIZED_KEYS" ] || return 0
    auth_owner=$(stat -c '%U' -- "$AUTHORIZED_KEYS") || die "无法检查 authorized_keys"
    [ "$auth_owner" = "$SSH_USER" ] || die "authorized_keys 不是 $SSH_USER 所有"
    auth_mode=$(stat -c '%A' -- "$AUTHORIZED_KEYS") || die "无法检查 authorized_keys 权限"
    case "$auth_mode" in
        ?????w????|????????w?) die "authorized_keys 不能被组或其他用户写入" ;;
    esac
}

install_marker_is_valid() {
    /usr/bin/python3 - "$INSTALL_MARKER" <<'PY'
import pathlib
import sys

expected = "\n".join(
    (
        "rsshub-cookie-sync-installation=1",
        "service-unit=/etc/systemd/system/rsshub-cookie-sync-monitor.service",
        "timer-unit=/etc/systemd/system/rsshub-cookie-sync-monitor.timer",
        "service-dropin=/etc/systemd/system/rsshub-cookie-sync-monitor.service.d/deployment.conf",
        "ssh-config=/etc/ssh/sshd_config.d/rsshub-cookie-sync.conf",
        "ssh-user=rsshub-sync",
        "authorized-keys=/var/lib/rsshub-sync/.ssh/authorized_keys",
        "install-dir=/usr/local/lib/rsshub-cookie-sync",
        "apply-wrapper=/usr/local/sbin/rsshub-cookie-sync-apply",
        "provision-wrapper=/usr/local/sbin/rsshub-cookie-sync-provision-key",
        "uninstall-wrapper=/usr/local/sbin/rsshub-cookie-sync-uninstall",
        "sudoers=/etc/sudoers.d/rsshub-cookie-sync",
        "config-file=/etc/rsshub-cookie-sync/config.json",
        "state-dir=/var/lib/rsshub-cookie-sync",
    )
) + "\n"
try:
    actual = pathlib.Path(sys.argv[1]).read_text(encoding="utf-8")
except (OSError, UnicodeError):
    raise SystemExit(1)
raise SystemExit(0 if actual == expected else 1)
PY
}

confirm_uninstall() {
    [ "$ASSUME_YES" -eq 1 ] && return 0
    [ -r /dev/tty ] || die "无法从终端确认卸载；已确认时请使用 --yes"
    while :; do
        printf '%s' "确定卸载 RSSHub Cookie Sync 吗？[y/N]: " >&2
        IFS= read -r answer < /dev/tty || die "没有收到确认，卸载已取消"
        case "$answer" in
            y|Y|yes|YES|Yes) return 0 ;;
            n|N|no|NO|"") echo "卸载已取消。" >&2; exit 0 ;;
            *) echo "请输入 y 或 n。" >&2 ;;
        esac
    done
}

# Read deployment paths without ever printing the JSON (which may contain a
# Bark key).  The installer only accepts these path characters, and the
# relationship checks below prevent a malformed config from redirecting
# cleanup into an arbitrary secrets directory.
DEPLOYMENT_INFO=
COMPOSE_FILE=
LIVE_ENV=
CANDIDATE_DIR=
CONFIG_PRESENT=0
if [ -e "$CONFIG_DIR" ] || [ -L "$CONFIG_DIR" ]; then
    assert_removable_dir "$CONFIG_DIR"
fi
if [ -e "$CONFIG_FILE" ] || [ -L "$CONFIG_FILE" ]; then
    assert_removable_file "$CONFIG_FILE"
    CONFIG_PRESENT=1
fi
if [ -e "$INSTALL_MARKER" ] || [ -L "$INSTALL_MARKER" ]; then
    assert_removable_file "$INSTALL_MARKER"
    install_marker_is_valid \
        || die "安装管理标记无效；尚未删除任何内容，请先重新安装以恢复标记"
fi
if [ -f "$CONFIG_FILE" ] && [ -x /usr/bin/python3 ]; then
    DEPLOYMENT_INFO=$(/usr/bin/python3 - "$CONFIG_FILE" <<'PY' 2>/dev/null || true
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
    deployment = data.get("deployment")
    values = tuple(deployment.get(name) for name in ("compose_file", "live_env", "candidate_dir"))
    if not all(
        isinstance(value, str)
        and value.startswith("/")
        and value
        and not any(char in value for char in "|\x00\r\n")
        for value in values
    ):
        raise ValueError
    print("|".join(values))
except Exception:
    raise SystemExit(1)
PY
)
fi

PERSISTENT_SSH_BACKUP=0
if [ -e "$SSH_CONFIG_PERSISTENT_BACKUP" ] || [ -L "$SSH_CONFIG_PERSISTENT_BACKUP" ] \
    || [ -e "$SSH_CONFIG_BACKUP_MARKER" ] || [ -L "$SSH_CONFIG_BACKUP_MARKER" ]; then
    if [ -L "$SSH_CONFIG_PERSISTENT_BACKUP" ] || [ -L "$SSH_CONFIG_BACKUP_MARKER" ]; then
        die "SSH 配置备份路径是符号链接，已停止卸载"
    fi
    if [ ! -f "$SSH_CONFIG_PERSISTENT_BACKUP" ] || [ ! -f "$SSH_CONFIG_BACKUP_MARKER" ] \
        || [ "$(stat -c '%A' "$SSH_CONFIG_BACKUP_MARKER")" != "-rw-------" ] \
        || [ "$(sed -n '1p' "$SSH_CONFIG_BACKUP_MARKER")" != "rsshub-cookie-sync-sshd-backup=1" ]; then
        die "SSH 配置备份不完整，已停止卸载"
    fi
    assert_removable_file "$SSH_CONFIG_PERSISTENT_BACKUP"
    assert_removable_file "$SSH_CONFIG_BACKUP_MARKER"
    PERSISTENT_SSH_BACKUP=1
fi
if [ -n "$DEPLOYMENT_INFO" ]; then
    IFS='|' read -r COMPOSE_FILE LIVE_ENV CANDIDATE_DIR <<EOF
$DEPLOYMENT_INFO
EOF
fi

valid_path_chars() {
    case "$1" in
        /*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        *[!A-Za-z0-9._/-]*) return 1 ;;
        *//*|*/./*|*/../*|*/.|*/..|*/) return 1 ;;
    esac
    return 0
}

DEPLOYMENT_PATHS_VALID=0
if [ -n "$COMPOSE_FILE" ] && [ -n "$LIVE_ENV" ] && [ -n "$CANDIDATE_DIR" ] \
    && valid_path_chars "$COMPOSE_FILE" \
    && valid_path_chars "$LIVE_ENV" \
    && valid_path_chars "$CANDIDATE_DIR"; then
    candidate_parent=$(dirname -- "$CANDIDATE_DIR")
    if [ "$(basename -- "$CANDIDATE_DIR")" = candidates ] \
        && [ "$(basename -- "$candidate_parent")" = secrets ] \
        && [ "$LIVE_ENV" = "$candidate_parent/rsshub.env" ]; then
        DEPLOYMENT_PATHS_VALID=1
    fi
fi
if [ "$CONFIG_PRESENT" -eq 1 ] && [ -z "$DEPLOYMENT_INFO" ]; then
    die "服务端配置无法安全读取；尚未删除任何内容，请先修复配置或重新安装"
fi
if [ "$CONFIG_PRESENT" -eq 1 ] && [ "$DEPLOYMENT_PATHS_VALID" -ne 1 ]; then
    die "服务端部署路径无法安全确认；尚未删除任何内容，请先修复配置或重新安装"
fi

# A live-env or Compose transaction may be the only way to recover the exact
# configuration currently used by RSSHub.  Uninstall promises not to modify
# RSSHub, so it refuses to discard those recovery files.  Re-running the
# installer/monitor completes recovery; a clean retry then has no such files.
if [ "$DEPLOYMENT_PATHS_VALID" -eq 1 ]; then
    for transaction_path in \
        "$COMPOSE_FILE.pre-cookie-sync" \
        "$COMPOSE_FILE.pre-cookie-sync.txn.json" \
        "$LIVE_ENV.pre-cookie-sync" \
        "$LIVE_ENV.prev" \
        "$LIVE_ENV.txn.json"; do
        if [ -e "$transaction_path" ] || [ -L "$transaction_path" ]; then
            die "检测到未完成的 RSSHub 事务：$transaction_path；请先重新运行安装器完成恢复，再卸载"
        fi
    done
fi

validate_candidate_contents() {
    /usr/bin/python3 - "$CANDIDATE_DIR" <<'PY'
import stat
import sys
from pathlib import Path

base = Path(sys.argv[1])
for child in base.iterdir():
    info = child.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
        raise SystemExit(1)
PY
}
if [ "$DEPLOYMENT_PATHS_VALID" -eq 1 ] \
    && { [ -e "$CANDIDATE_DIR" ] || [ -L "$CANDIDATE_DIR" ]; }; then
    assert_safe_dir_chain "$(dirname -- "$CANDIDATE_DIR")"
    assert_removable_dir "$CANDIDATE_DIR"
    validate_candidate_contents \
        || die "候选目录包含无法确认归属或权限不安全的文件；尚未删除任何内容"
fi

ACCOUNT_CREATED=0
if [ -e "$ACCOUNT_MARKER" ] || [ -L "$ACCOUNT_MARKER" ]; then
    if [ -L "$ACCOUNT_MARKER" ]; then
        echo "account-created marker 是符号链接，保留 rsshub-sync 账号。" >&2
    elif [ -f "$ACCOUNT_MARKER" ] && [ "$(stat -c '%u' -- "$ACCOUNT_MARKER" 2>/dev/null || echo 1)" = 0 ] \
        && [ "$(stat -c '%A' -- "$ACCOUNT_MARKER" 2>/dev/null || true)" = "-rw-------" ] \
        && [ "$(awk 'END { print NR }' "$ACCOUNT_MARKER" 2>/dev/null || echo 0)" -eq 1 ] \
        && [ "$(sed -n '1p' "$ACCOUNT_MARKER" 2>/dev/null || true)" = "rsshub-cookie-sync-account-created=1" ]; then
        ACCOUNT_CREATED=1
    else
        echo "account-created marker 无法确认来源，保留 rsshub-sync 账号。" >&2
    fi
fi

MANAGED_PRESENT=0
for managed_path in \
    "$SERVICE_UNIT" "$TIMER_UNIT" "$DROPIN_DIR" \
    /usr/local/sbin/rsshub-cookie-sync-apply \
    /usr/local/sbin/rsshub-cookie-sync-provision-key \
    /usr/local/sbin/rsshub-cookie-sync-uninstall \
    /etc/sudoers.d/rsshub-cookie-sync "$SSH_CONFIG" \
    "$INSTALL_DIR" "$CONFIG_FILE" "$INSTALL_MARKER" "$STATE_DIR"; do
    if [ -e "$managed_path" ] || [ -L "$managed_path" ]; then
        MANAGED_PRESENT=1
        break
    fi
done
if id "$SSH_USER" >/dev/null 2>&1 \
    && [ -f "$AUTHORIZED_KEYS" ] && [ ! -L "$AUTHORIZED_KEYS" ] \
    && awk '/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 / { found = 1 } END { exit(found ? 0 : 1) }' \
        "$AUTHORIZED_KEYS" 2>/dev/null; then
    MANAGED_PRESENT=1
fi
if [ "$MANAGED_PRESENT" -eq 0 ]; then
    echo "RSSHub Cookie Sync 已经卸载；RSSHub 未被修改。" >&2
    exit 0
fi
if [ ! -f "$INSTALL_MARKER" ]; then
    echo "未找到新版安装管理标记；将按固定旧版路径执行兼容卸载，不会扩大删除范围。" >&2
fi

cat >&2 <<'EOF'
将删除：RSSHub Cookie Sync 的 systemd service/timer、监控状态、Bark 配置、同步程序、sudoers 规则，以及本项目写入的 rsshub-sync 受限公钥。
如果账号由本项目首次创建，也会删除 rsshub-sync 账号；无法确认来源的同名账号会保留。
将保留：RSSHub docker-compose.yml、所有 RSSHub 容器/volume、secrets/rsshub.env，以及其他未由本项目标记的文件。
EOF
confirm_uninstall

if [ "$SYSTEMD_AVAILABLE" -eq 1 ]; then
    if systemctl is-active --quiet "$TIMER_NAME"; then
        systemctl stop "$TIMER_NAME" >/dev/null 2>&1 || die "无法停止 monitor timer"
    fi
    if systemctl is-active --quiet "$SERVICE_NAME"; then
        systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || die "无法停止 monitor service"
    fi
    systemctl disable "$TIMER_NAME" >/dev/null 2>&1 || true
fi

for unit_path in "$SERVICE_UNIT" "$TIMER_UNIT"; do
    if [ -e "$unit_path" ] || [ -L "$unit_path" ]; then
        assert_removable_file "$unit_path"
        rm -f "$unit_path"
    fi
done
if [ -e "$DROPIN_DIR" ] || [ -L "$DROPIN_DIR" ]; then
    assert_removable_dir "$DROPIN_DIR"
    if [ -e "$DROPIN_FILE" ] || [ -L "$DROPIN_FILE" ]; then
        assert_removable_file "$DROPIN_FILE"
        rm -f "$DROPIN_FILE"
    fi
    rmdir "$DROPIN_DIR" 2>/dev/null || true
fi
if [ "$SYSTEMD_AVAILABLE" -eq 1 ]; then
    systemctl daemon-reload >/dev/null 2>&1 || true
fi

for managed_file in \
    /usr/local/sbin/rsshub-cookie-sync-apply \
    /usr/local/sbin/rsshub-cookie-sync-provision-key \
    /usr/local/sbin/rsshub-cookie-sync-uninstall \
    /etc/sudoers.d/rsshub-cookie-sync; do
    if [ -e "$managed_file" ] || [ -L "$managed_file" ]; then
        assert_removable_file "$managed_file"
        rm -f "$managed_file"
    fi
done

if [ -e "$SSH_CONFIG" ] || [ -L "$SSH_CONFIG" ] || [ "$PERSISTENT_SSH_BACKUP" -eq 1 ]; then
    if [ -e "$SSH_CONFIG" ] || [ -L "$SSH_CONFIG" ]; then
        assert_removable_file "$SSH_CONFIG"
        ssh_config_backup=$(mktemp /tmp/rsshub-cookie-sync-uninstall-sshd.XXXXXX)
        cp -p "$SSH_CONFIG" "$ssh_config_backup"
        chmod 0600 "$ssh_config_backup"
        rm -f "$SSH_CONFIG"
    else
        ssh_config_backup=
    fi
    if [ "$PERSISTENT_SSH_BACKUP" -eq 1 ]; then
        install -m 0644 "$SSH_CONFIG_PERSISTENT_BACKUP" "$SSH_CONFIG"
    fi
    if command -v sshd >/dev/null 2>&1 && ! sshd -t >/dev/null 2>&1; then
        rm -f "$SSH_CONFIG"
        if [ -n "${ssh_config_backup:-}" ]; then
            install -m 0644 "$ssh_config_backup" "$SSH_CONFIG"
        fi
        if [ -n "$ssh_config_backup" ]; then
            rm -f "$ssh_config_backup"
        fi
        die "删除同步器 sshd 配置后校验失败，已恢复原文件"
    fi
    if [ "$SYSTEMD_AVAILABLE" -eq 1 ]; then
        systemctl reload sshd >/dev/null 2>&1 || systemctl reload ssh >/dev/null 2>&1 || true
    fi
    if [ -n "$ssh_config_backup" ]; then
        rm -f "$ssh_config_backup"
    fi
    if [ "$PERSISTENT_SSH_BACKUP" -eq 1 ]; then
        rm -f "$SSH_CONFIG_PERSISTENT_BACKUP" "$SSH_CONFIG_BACKUP_MARKER"
    fi
fi

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
    rmdir "$INSTALL_DIR" 2>/dev/null || true
fi

# Remove only the project's exact candidate directory.
# A valid deployment config proves that candidates and rsshub.env are siblings;
# the live env itself is never included in this list.
remove_candidate_files() {
    [ "$DEPLOYMENT_PATHS_VALID" -eq 1 ] || return 0
    assert_safe_dir_chain "$candidate_parent"
    if [ -e "$CANDIDATE_DIR" ] || [ -L "$CANDIDATE_DIR" ]; then
        assert_removable_dir "$CANDIDATE_DIR"
        validate_candidate_contents \
            || die "候选目录在卸载期间发生变化，已停止清理"
        /usr/bin/python3 - "$CANDIDATE_DIR" <<'PY'
import sys
from pathlib import Path

for child in Path(sys.argv[1]).iterdir():
    child.unlink()
PY
        rmdir "$CANDIDATE_DIR" 2>/dev/null || true
    fi
}
remove_candidate_files

# Delete state and lock files, but do not recursively remove an administrator's
# unrelated file from the state directory.
if [ -e "$STATE_DIR" ] || [ -L "$STATE_DIR" ]; then
    assert_removable_dir "$STATE_DIR"
    for state_path in "$STATE_FILE" "$LOCK_FILE"; do
        if [ -e "$state_path" ] || [ -L "$state_path" ]; then
            assert_removable_file "$state_path"
            rm -f "$state_path"
        fi
    done
    rmdir "$STATE_DIR" 2>/dev/null || true
fi

remove_managed_authorization() {
    if [ ! -e "$AUTHORIZED_KEYS" ] && [ ! -L "$AUTHORIZED_KEYS" ]; then
        return 0
    fi
    [ ! -L "$SSH_HOME" ] && [ -d "$SSH_HOME" ] \
        || die "无法安全撤销 rsshub-sync 授权：home 路径异常"
    [ ! -L "$SSH_HOME/.ssh" ] && [ -d "$SSH_HOME/.ssh" ] \
        || die "无法安全撤销 rsshub-sync 授权：.ssh 路径异常"
    account_group=$(id -gn "$SSH_USER")
    [ "$(stat -c '%U' -- "$SSH_HOME")" = "$SSH_USER" ] \
        && [ "$(stat -c '%G' -- "$SSH_HOME")" = "$account_group" ] \
        || die "无法安全撤销 rsshub-sync 授权：home 所有者异常"
    home_mode=$(stat -c '%A' -- "$SSH_HOME") || die "无法检查 rsshub-sync home 权限"
    case "$home_mode" in
        ?????w????|????????w?) die "无法安全撤销 rsshub-sync 授权：home 权限不安全" ;;
    esac
    [ "$(stat -c '%U' -- "$SSH_HOME/.ssh")" = "$SSH_USER" ] \
        && [ "$(stat -c '%G' -- "$SSH_HOME/.ssh")" = "$account_group" ] \
        && [ "$(stat -c '%a' -- "$SSH_HOME/.ssh")" = 700 ] \
        || die "无法安全撤销 rsshub-sync 授权：.ssh 所有者或权限异常"
    assert_auth_file
    auth_tmp=$(mktemp "$SSH_HOME/.ssh/.authorized_keys.uninstall.XXXXXX")
    chown "$SSH_USER:$(id -gn "$SSH_USER")" "$auth_tmp"
    chmod 0600 "$auth_tmp"
    awk '!/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 /' \
        "$AUTHORIZED_KEYS" > "$auth_tmp"
    if cmp -s "$AUTHORIZED_KEYS" "$auth_tmp"; then
        rm -f "$auth_tmp"
    elif [ ! -s "$auth_tmp" ]; then
        rm -f "$auth_tmp" "$AUTHORIZED_KEYS"
    else
        mv -f "$auth_tmp" "$AUTHORIZED_KEYS"
        chown "$SSH_USER:$(id -gn "$SSH_USER")" "$AUTHORIZED_KEYS"
        chmod 0600 "$AUTHORIZED_KEYS"
    fi
}

account_home_is_project_only() {
    /usr/bin/python3 - "$SSH_HOME" <<'PY'
import os
import stat
import sys
from pathlib import Path

home = Path(sys.argv[1])
children = list(home.iterdir())
if any(child.name != ".ssh" for child in children):
    raise SystemExit(1)
ssh_dir = home / ".ssh"
if not os.path.lexists(ssh_dir):
    raise SystemExit(0)
info = ssh_dir.lstat()
if not stat.S_ISDIR(info.st_mode):
    raise SystemExit(1)
if any(child.name != "authorized_keys" for child in ssh_dir.iterdir()):
    raise SystemExit(1)
PY
    [ ! -f "$AUTHORIZED_KEYS" ] && return 0
    total_lines=$(awk 'END { print NR + 0 }' "$AUTHORIZED_KEYS") || return 1
    managed_lines=$(awk '/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 / { count++ } END { print count + 0 }' \
        "$AUTHORIZED_KEYS") || return 1
    [ "$total_lines" -eq 1 ] && [ "$managed_lines" -eq 1 ]
}

account_paths_are_safe() {
    safe_group=$(id -gn "$SSH_USER") || return 1
    [ ! -L "$SSH_HOME" ] && [ -d "$SSH_HOME" ] || return 1
    [ "$(stat -c '%U' -- "$SSH_HOME" 2>/dev/null || true)" = "$SSH_USER" ] || return 1
    [ "$(stat -c '%G' -- "$SSH_HOME" 2>/dev/null || true)" = "$safe_group" ] || return 1
    safe_home_mode=$(stat -c '%A' -- "$SSH_HOME" 2>/dev/null || true)
    [ -n "$safe_home_mode" ] || return 1
    case "$safe_home_mode" in
        ?????w????|????????w?) return 1 ;;
    esac
    if [ -e "$SSH_HOME/.ssh" ] || [ -L "$SSH_HOME/.ssh" ]; then
        [ ! -L "$SSH_HOME/.ssh" ] && [ -d "$SSH_HOME/.ssh" ] || return 1
        [ "$(stat -c '%U' -- "$SSH_HOME/.ssh" 2>/dev/null || true)" = "$SSH_USER" ] || return 1
        [ "$(stat -c '%G' -- "$SSH_HOME/.ssh" 2>/dev/null || true)" = "$safe_group" ] || return 1
        [ "$(stat -c '%a' -- "$SSH_HOME/.ssh" 2>/dev/null || true)" = 700 ] || return 1
    fi
    return 0
}

if id "$SSH_USER" >/dev/null 2>&1; then
    account_home=$(getent passwd "$SSH_USER" | awk -F: 'NR == 1 { print $6 }')
    if [ "$account_home" != "$SSH_HOME" ] || [ -z "$account_home" ] || [ -L "$account_home" ]; then
        echo "rsshub-sync home 路径已被管理员修改；为避免操作错误目录，将保留账号和旧文件。项目 SSH 配置与 root wrapper 已移除，因此旧 forced-command 授权无法再调用同步器。" >&2
    elif [ "$ACCOUNT_CREATED" -eq 1 ]; then
        account_shell=$(getent passwd "$SSH_USER" | awk -F: 'NR == 1 { print $7 }')
        account_primary_group=$(id -gn "$SSH_USER")
        account_all_groups=$(id -Gn "$SSH_USER")
        account_uid=$(id -u "$SSH_USER")
        account_gid=$(id -g "$SSH_USER")
        if [ "$account_shell" != /bin/sh ] || [ "$account_uid" -eq 0 ] || [ "$account_gid" -eq 0 ] \
            || [ "$account_all_groups" != "$account_primary_group" ] \
            || [ -L "$SSH_HOME/.ssh" ] || { [ -e "$SSH_HOME/.ssh" ] && [ ! -d "$SSH_HOME/.ssh" ]; } \
            || ! account_paths_are_safe \
            || ! account_home_is_project_only; then
            echo "rsshub-sync 账号属性已被修改，保留账号，但仍将撤销本项目 forced-command 授权。" >&2
            remove_managed_authorization
        else
            userdel --remove "$SSH_USER" || die "删除 rsshub-sync 账号失败；RSSHub 未被修改"
        fi
    else
        remove_managed_authorization
        if [ -d "$SSH_HOME/.ssh" ] && [ -z "$(ls -A "$SSH_HOME/.ssh" 2>/dev/null || true)" ]; then
            rmdir "$SSH_HOME/.ssh" 2>/dev/null || true
        fi
    fi
fi

if [ -e "$CONFIG_DIR" ] || [ -L "$CONFIG_DIR" ]; then
    assert_removable_dir "$CONFIG_DIR"
    for config_path in "$CONFIG_FILE" "$ACCOUNT_MARKER" "$INSTALL_MARKER"; do
        if [ -e "$config_path" ] || [ -L "$config_path" ]; then
            assert_removable_file "$config_path"
            rm -f "$config_path"
        fi
    done
    rmdir "$CONFIG_DIR" 2>/dev/null || true
fi

# Verify the user-visible uninstall contract before declaring success.  A
# retained administrator-created account is allowed, but the exact project
# authorization must be gone so it can no longer invoke the root wrapper.
for removed_path in \
    "$SERVICE_UNIT" "$TIMER_UNIT" "$DROPIN_FILE" \
    /usr/local/sbin/rsshub-cookie-sync-apply \
    /usr/local/sbin/rsshub-cookie-sync-provision-key \
    /usr/local/sbin/rsshub-cookie-sync-uninstall \
    /etc/sudoers.d/rsshub-cookie-sync \
    "$INSTALL_DIR" "$CONFIG_FILE" "$INSTALL_MARKER" "$STATE_FILE" "$LOCK_FILE"; do
    if [ -e "$removed_path" ] || [ -L "$removed_path" ]; then
        die "卸载验证失败，仍存在托管路径：$removed_path"
    fi
done
if [ "$SYSTEMD_AVAILABLE" -eq 1 ]; then
    if systemctl is-active --quiet "$TIMER_NAME" || systemctl is-active --quiet "$SERVICE_NAME"; then
        die "卸载验证失败：monitor service/timer 仍在运行"
    fi
    if systemctl is-enabled --quiet "$TIMER_NAME" >/dev/null 2>&1; then
        die "卸载验证失败：monitor timer 仍处于启用状态"
    fi
fi
if id "$SSH_USER" >/dev/null 2>&1; then
    verification_home=$(getent passwd "$SSH_USER" | awk -F: 'NR == 1 { print $6 }')
    if [ "$verification_home" = "$SSH_HOME" ]; then
        if [ -L "$AUTHORIZED_KEYS" ]; then
            die "卸载验证失败：authorized_keys 变成了符号链接"
        fi
        if [ -f "$AUTHORIZED_KEYS" ] \
            && awk '/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 / { found = 1 } END { exit(found ? 0 : 1) }' \
                "$AUTHORIZED_KEYS"; then
            die "卸载验证失败：项目 SSH 授权仍然存在"
        fi
    fi
fi

echo "RSSHub Cookie Sync 已卸载。RSSHub Compose、容器和 live secrets/rsshub.env 已保留。" >&2
