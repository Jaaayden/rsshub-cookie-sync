#!/bin/sh
set -eu

# Install the RSSHub Cookie synchronizer on the RSSHub host.  This script is
# deliberately local-only: it never SSHes anywhere and never pulls a Docker
# image.  Cookie values are handled by the Python transaction, not by shell
# variables or command-line arguments.

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LC_ALL=C
export LC_ALL

usage() {
    cat >&2 <<'EOF'
Usage:
  install.sh [options]

Without options the installer looks for a Compose file in the current
directory, /opt/rsshub, and /root/rsshub.  It asks for confirmation, lets
Compose resolve its project, and uses service rsshub plus
http://127.0.0.1:1200 unless the deployment is non-standard.

Options:
  --compose-file ABSOLUTE_PATH   Compose file (normally discovered automatically)
  --project-name NAME            Advanced: override the detected Compose project
  --service-name NAME            Advanced: service to recreate (default: rsshub)
  --rsshub-base-url URL          Advanced: local RSSHub URL (default: http://127.0.0.1:1200)
  --public-key-file ABSOLUTE_PATH
                                 Install this local ssh-ed25519 key (advanced/non-interactive)
  --replace-deployment           Allow replacing a different saved deployment
  --help                         Show this help
EOF
}

die() {
    echo "rsshub-cookie-sync install: $*" >&2
    exit 1
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || die "required command is missing: $1"
}

if [ "$(id -u)" -ne 0 ]; then
    die "install.sh must run as root"
fi

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

COMPOSE_FILE=
PROJECT=rsshub
PROJECT_EXPLICIT=0
SERVICE=rsshub
RSSHUB_BASE_URL=http://127.0.0.1:1200
PUBLIC_KEY_FILE=
REPLACE_DEPLOYMENT=0
INTERACTIVE=0
ORIGINAL_ARGC=$#

ask_yes_no() {
    question=$1
    default_answer=${2:-no}
    [ -r /dev/tty ] || return 1
    while :; do
        if [ "$default_answer" = yes ]; then
            printf '%s' "$question [Y/n]: " >&2
        else
            printf '%s' "$question [y/N]: " >&2
        fi
        IFS= read -r answer < /dev/tty || return 1
        case "$answer" in
            y|Y|yes|YES|Yes) return 0 ;;
            n|N|no|NO|No) return 1 ;;
            "") [ "$default_answer" = yes ] && return 0; return 1 ;;
            *) echo "请输入 y 或 n。" >&2 ;;
        esac
    done
}

# The one-command installer is intentionally small: the common RSSHub
# layouts all use the same file and service names.  Keep the unusual layout
# escape hatch in the argument parser below, but do not make every user learn
# Compose's internal project/service terminology.
discover_compose_file() {
    for candidate in \
        "$PWD/docker-compose.yml" "$PWD/docker-compose.yaml" \
        "$PWD/compose.yml" "$PWD/compose.yaml" \
        /opt/rsshub/docker-compose.yml /opt/rsshub/docker-compose.yaml \
        /opt/rsshub/compose.yml /opt/rsshub/compose.yaml \
        /root/rsshub/docker-compose.yml /root/rsshub/docker-compose.yaml \
        /root/rsshub/compose.yml /root/rsshub/compose.yaml; do
        if [ -f "$candidate" ] && [ ! -L "$candidate" ]; then
            COMPOSE_FILE=$candidate
            echo "检测到 RSSHub Compose 文件：$COMPOSE_FILE" >&2
            if ask_yes_no "将检查并迁移这个文件，继续吗" yes; then
                return 0
            fi
            COMPOSE_FILE=
        fi
    done

    [ -r /dev/tty ] || die "未自动找到 docker-compose.yml；请使用 --compose-file 指定绝对路径"
    printf '%s' "未找到常见位置的 docker-compose.yml，请输入绝对路径（直接回车退出）： " >&2
    IFS= read -r COMPOSE_FILE < /dev/tty || die "无法从终端读取 Compose 路径"
    [ -n "$COMPOSE_FILE" ] || die "没有提供 Compose 路径"
}

prompt_for_service_if_needed() {
    services=$1
    if printf '%s\n' "$services" | awk -v expected="$SERVICE" '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }'; then
        return 0
    fi
    if [ "$INTERACTIVE" -ne 1 ] || [ "$SERVICE" != rsshub ]; then
        die "Compose 中没有找到 RSSHub 服务 '$SERVICE'；如服务名称不同，请使用 --service-name 指定"
    fi

    printf '%s\n' "Compose 中没有找到默认服务 rsshub。可输入实际的 RSSHub 服务名；常见官方文件无需修改。" >&2
    printf '%s\n' "当前文件定义了这些服务：" >&2
    printf '%s\n' "$services" | awk '{ print "  - " $0 }' >&2
    printf '%s' "RSSHub 服务名（直接回车退出）： " >&2
    IFS= read -r selected_service < /dev/tty || die "无法从终端读取服务名"
    [ -n "$selected_service" ] || die "没有提供 RSSHub 服务名"
    SERVICE=$selected_service
    if ! printf '%s\n' "$services" | awk -v expected="$SERVICE" '$0 == expected { found = 1 } END { exit(found ? 0 : 1) }'; then
        die "Compose 中没有找到服务 '$SERVICE'"
    fi
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --compose-file)
            [ "$#" -ge 2 ] || die "--compose-file requires an argument"
            COMPOSE_FILE=$2
            shift 2
            ;;
        --project-name)
            [ "$#" -ge 2 ] || die "--project-name requires an argument"
            PROJECT=$2
            PROJECT_EXPLICIT=1
            shift 2
            ;;
        --service-name)
            [ "$#" -ge 2 ] || die "--service-name requires an argument"
            SERVICE=$2
            shift 2
            ;;
        --rsshub-base-url)
            [ "$#" -ge 2 ] || die "--rsshub-base-url requires an argument"
            RSSHUB_BASE_URL=$2
            shift 2
            ;;
        --public-key-file)
            [ "$#" -ge 2 ] || die "--public-key-file requires an argument"
            PUBLIC_KEY_FILE=$2
            shift 2
            ;;
        --replace-deployment)
            REPLACE_DEPLOYMENT=1
            shift
            ;;
        --help)
            usage
            exit 0
            ;;
        *)
            usage
            die "unknown option or argument: $1"
            ;;
    esac
done

if [ "$ORIGINAL_ARGC" -eq 0 ]; then
    INTERACTIVE=1
fi
if [ -z "$COMPOSE_FILE" ]; then
    if [ "$INTERACTIVE" -eq 1 ]; then
        discover_compose_file
    else
        usage
        die "--compose-file is required when using options; omit all options for automatic discovery"
    fi
fi

# Keep the shell-side checks intentionally stricter than the minimum needed by
# Docker.  This prevents path confusion and makes the generated systemd
# ReadWritePaths line unambiguous.  The Python layer repeats the security
# checks before it writes deployment configuration.
validate_abs_path() {
    path_value=$1
    path_label=$2
    case "$path_value" in
        /*) ;;
        *) die "$path_label must be an absolute path" ;;
    esac
    case "$path_value" in
        ""|*[!A-Za-z0-9._/-]*) die "$path_label contains unsupported characters" ;;
        *//*|*/./*|*/../*|*/.|*/..|*/) die "$path_label is not a canonical path" ;;
    esac
}

validate_abs_path "$COMPOSE_FILE" "--compose-file"

INSTALL_DIR=/usr/local/lib/rsshub-cookie-sync
SBIN_DIR=/usr/local/sbin
CONFIG_DIR=/etc/rsshub-cookie-sync
CONFIG_FILE=$CONFIG_DIR/config.json
STATE_DIR=/var/lib/rsshub-cookie-sync
STATE_FILE=$STATE_DIR/state.json
LOCK_FILE=$STATE_DIR/lock
SSH_USER=rsshub-sync
SSH_HOME=/var/lib/rsshub-sync
AUTHORIZED_KEYS=$SSH_HOME/.ssh/authorized_keys
SSH_CONFIG=/etc/ssh/sshd_config.d/rsshub-cookie-sync.conf
SERVICE_UNIT=/etc/systemd/system/rsshub-cookie-sync-monitor.service
TIMER_UNIT=/etc/systemd/system/rsshub-cookie-sync-monitor.timer
DROPIN_DIR=/etc/systemd/system/rsshub-cookie-sync-monitor.service.d
DROPIN_FILE=$DROPIN_DIR/deployment.conf
ACCOUNT_MARKER=$CONFIG_DIR/account-created
INSTALL_MARKER=$CONFIG_DIR/install-manifest
SSH_CONFIG_PERSISTENT_BACKUP=$CONFIG_DIR/sshd-config.backup
SSH_CONFIG_BACKUP_MARKER=$CONFIG_DIR/sshd-config.backup.present
PERSISTENT_SSH_BACKUP_EXISTS=0

COMPOSE_DIR=$(dirname -- "$COMPOSE_FILE")
SECRETS_DIR=$COMPOSE_DIR/secrets
LIVE_ENV=$SECRETS_DIR/rsshub.env
CANDIDATE_DIR=$SECRETS_DIR/candidates

for command_name in \
    id install useradd userdel usermod getent sshd ssh-keygen visudo stat cp mktemp rm chmod chown \
    mv awk sed dd wc cmp python3 docker dirname; do
    require_command "$command_name"
done
[ -x /usr/bin/python3 ] || die "/usr/bin/python3 is missing or not executable"
require_command systemctl
[ -d /run/systemd/system ] || die "systemd is required"
if ! docker compose version >/dev/null 2>&1; then
    die "docker compose is unavailable"
fi

assert_root_dir() {
    check_path=$1
    [ ! -L "$check_path" ] || die "path must not be a symlink: $check_path"
    [ -d "$check_path" ] || die "directory is missing: $check_path"
    check_owner=$(stat -c '%u' -- "$check_path") || die "cannot inspect directory: $check_path"
    [ "$check_owner" = 0 ] || die "directory is not root-owned: $check_path"
    check_mode=$(stat -c '%A' -- "$check_path") || die "cannot inspect directory permissions: $check_path"
    case "$check_mode" in
        ?????w????|????????w?) die "directory must not be group/world writable: $check_path" ;;
    esac
}

assert_root_file() {
    check_path=$1
    [ ! -L "$check_path" ] || die "path must not be a symlink: $check_path"
    [ -f "$check_path" ] || die "regular file is missing: $check_path"
    check_owner=$(stat -c '%u' -- "$check_path") || die "cannot inspect file: $check_path"
    [ "$check_owner" = 0 ] || die "file is not root-owned: $check_path"
    check_mode=$(stat -c '%A' -- "$check_path") || die "cannot inspect file permissions: $check_path"
    case "$check_mode" in
        ?????w????|????????w?) die "file must not be group/world writable: $check_path" ;;
    esac
}

assert_source_file() {
    check_path=$1
    [ ! -L "$check_path" ] || die "source file must not be a symlink: $check_path"
    [ -f "$check_path" ] || die "source file is missing: $check_path"
    source_owner=$(stat -c '%u' -- "$check_path") || die "cannot inspect source file: $check_path"
    [ "$source_owner" = 0 ] || die "source file must be root-owned: $check_path"
    source_mode=$(stat -c '%A' -- "$check_path") || die "cannot inspect source file permissions: $check_path"
    case "$source_mode" in
        ?????w????|????????w?) die "source file must not be group/world writable: $check_path" ;;
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

write_install_marker() {
    marker_tmp=$(mktemp /tmp/rsshub-cookie-sync-install-manifest.XXXXXX)
    chmod 0600 "$marker_tmp"
    printf '%s\n' \
        'rsshub-cookie-sync-installation=1' \
        'service-unit=/etc/systemd/system/rsshub-cookie-sync-monitor.service' \
        'timer-unit=/etc/systemd/system/rsshub-cookie-sync-monitor.timer' \
        'service-dropin=/etc/systemd/system/rsshub-cookie-sync-monitor.service.d/deployment.conf' \
        'ssh-config=/etc/ssh/sshd_config.d/rsshub-cookie-sync.conf' \
        'ssh-user=rsshub-sync' \
        'authorized-keys=/var/lib/rsshub-sync/.ssh/authorized_keys' \
        'install-dir=/usr/local/lib/rsshub-cookie-sync' \
        'apply-wrapper=/usr/local/sbin/rsshub-cookie-sync-apply' \
        'provision-wrapper=/usr/local/sbin/rsshub-cookie-sync-provision-key' \
        'uninstall-wrapper=/usr/local/sbin/rsshub-cookie-sync-uninstall' \
        'sudoers=/etc/sudoers.d/rsshub-cookie-sync' \
        'config-file=/etc/rsshub-cookie-sync/config.json' \
        'state-dir=/var/lib/rsshub-cookie-sync' \
        > "$marker_tmp"
    mv -f "$marker_tmp" "$INSTALL_MARKER"
    marker_tmp=
    install_marker_created=1
    chmod 0600 "$INSTALL_MARKER"
}

# Check every existing parent.  In addition to the Compose file itself, this
# rejects a symlinked or user-owned directory that could redirect secrets or
# the generated systemd write allowance.
assert_root_chain() {
    chain_path=$1
    while :; do
        assert_root_dir "$chain_path"
        [ "$chain_path" = / ] && break
        next_path=$(dirname -- "$chain_path")
        [ "$next_path" != "$chain_path" ] || die "cannot walk path parents"
        chain_path=$next_path
    done
}

assert_root_chain "$SCRIPT_DIR"
for source_file in \
    install.sh uninstall.sh rsshub_cookie_sync.py rsshub-cookie-sync rsshub-cookie-sync-apply \
    provision-ssh-key.sh sudoers.example sshd-rsshub-cookie-sync.conf \
    config.example.json rsshub-cookie-sync-monitor.service \
    rsshub-cookie-sync-monitor.timer; do
    assert_source_file "$SCRIPT_DIR/$source_file"
done

assert_root_chain "$COMPOSE_DIR"
assert_root_file "$COMPOSE_FILE"
if [ -e "$SECRETS_DIR" ] || [ -L "$SECRETS_DIR" ]; then
    assert_root_dir "$SECRETS_DIR"
fi
if [ -e "$LIVE_ENV" ] || [ -L "$LIVE_ENV" ]; then
    assert_root_file "$LIVE_ENV"
fi
if [ -e "$CANDIDATE_DIR" ] || [ -L "$CANDIDATE_DIR" ]; then
    assert_root_dir "$CANDIDATE_DIR"
fi

for managed_dir in "$INSTALL_DIR" "$CONFIG_DIR" "$STATE_DIR"; do
    if [ -e "$managed_dir" ] || [ -L "$managed_dir" ]; then
        assert_root_dir "$managed_dir"
    fi
done
if [ -e "$CONFIG_FILE" ] || [ -L "$CONFIG_FILE" ]; then
    assert_root_file "$CONFIG_FILE"
fi
if [ -e "$INSTALL_MARKER" ] || [ -L "$INSTALL_MARKER" ]; then
    assert_root_file "$INSTALL_MARKER"
    install_marker_is_valid || die "install ownership manifest is invalid"
fi
if [ -e "$SSH_CONFIG_PERSISTENT_BACKUP" ] || [ -L "$SSH_CONFIG_PERSISTENT_BACKUP" ]; then
    assert_root_file "$SSH_CONFIG_PERSISTENT_BACKUP"
    PERSISTENT_SSH_BACKUP_EXISTS=1
fi
if [ -e "$SSH_CONFIG_BACKUP_MARKER" ] || [ -L "$SSH_CONFIG_BACKUP_MARKER" ]; then
    assert_root_file "$SSH_CONFIG_BACKUP_MARKER"
    [ "$(sed -n '1p' "$SSH_CONFIG_BACKUP_MARKER")" = "rsshub-cookie-sync-sshd-backup=1" ] \
        || die "SSH 配置备份标记无效"
    [ "$PERSISTENT_SSH_BACKUP_EXISTS" -eq 1 ] \
        || die "SSH 配置备份标记存在但备份文件缺失"
fi

# A no-argument reinstall should preserve the target that was already
# selected, including a non-default service or loopback port.  Read only the
# four non-secret deployment fields from the root-only config, and only reuse
# them when they refer to the same Compose file the operator just confirmed.
# Bark credentials and Cookie material are never emitted by this parser.
if [ "$INTERACTIVE" -eq 1 ] && [ -f "$CONFIG_FILE" ]; then
    saved_deployment=$(/usr/bin/python3 -c '
import json, sys
with open(sys.argv[1], "r", encoding="utf-8") as handle:
    data = json.load(handle)
deployment = data.get("deployment")
rsshub = data.get("rsshub")
if not isinstance(deployment, dict) or not isinstance(rsshub, dict):
    raise SystemExit(2)
values = (
    deployment.get("compose_file"),
    deployment.get("project"),
    deployment.get("service"),
    rsshub.get("base_url"),
)
if not all(isinstance(value, str) and value and not any(char in value for char in "|\x00\r\n") for value in values):
    raise SystemExit(2)
print("|".join(values))
' "$CONFIG_FILE" 2>/dev/null || true)
    if [ -n "$saved_deployment" ]; then
        saved_compose_file=
        saved_project=
        saved_service=
        saved_base_url=
        IFS='|' read -r saved_compose_file saved_project saved_service saved_base_url <<EOF
$saved_deployment
EOF
        if [ "$saved_compose_file" = "$COMPOSE_FILE" ]; then
            PROJECT=$saved_project
            PROJECT_EXPLICIT=1
            SERVICE=$saved_service
            RSSHUB_BASE_URL=$saved_base_url
            echo "检测到已安装实例，将保留原有 service 和健康地址。" >&2
        fi
    fi
fi

case "$SERVICE" in
    [A-Za-z0-9]*) ;;
    *) die "--service-name must start with a letter or digit" ;;
esac
case "$SERVICE" in
    *[!A-Za-z0-9_.-]*) die "--service-name contains unsupported characters" ;;
esac

case "$RSSHUB_BASE_URL" in
    http://*) ;;
    *) die "--rsshub-base-url must use http://" ;;
esac

# Let Compose resolve its real project name from a top-level `name:`, the
# Compose directory, or COMPOSE_PROJECT_NAME.  Do this only after verifying
# that the complete Compose path is root-owned and not writable by other
# users.  The canonical JSON is consumed by a pipe and never printed because
# it may contain rendered environment values.
if [ "$PROJECT_EXPLICIT" -eq 0 ]; then
    PROJECT=$(docker compose -f "$COMPOSE_FILE" config --format json 2>/dev/null | \
        /usr/bin/python3 -c 'import json,sys; value=json.load(sys.stdin).get("name"); isinstance(value,str) and value or sys.exit(2); print(value)') \
        || die "cannot resolve the Compose project name"
fi
case "$PROJECT" in
    [a-z0-9]*|[a-z0-9]) ;;
    *) die "Compose project name must start with a lowercase letter or digit" ;;
esac
case "$PROJECT" in
    *[!a-z0-9_-]*) die "Compose project name contains unsupported characters" ;;
esac

# Validate Compose without ever emitting its rendered configuration.  The
# service check uses the exact line returned by `config --services`, so a name
# that merely contains the requested name is not accepted.  In the common
# interactive path a non-standard service name is requested once, rather than
# making the operator learn --service-name.
if ! docker compose -p "$PROJECT" -f "$COMPOSE_FILE" config --quiet >/dev/null 2>&1; then
    die "existing Compose configuration is invalid"
fi
compose_services=$(docker compose -p "$PROJECT" -f "$COMPOSE_FILE" config --services 2>/dev/null) || die "cannot list Compose services"
prompt_for_service_if_needed "$compose_services"
if [ "$INTERACTIVE" -eq 1 ]; then
    echo "已识别 Compose 项目：$PROJECT" >&2
    echo "RSSHub 服务：$SERVICE" >&2
    echo "本机健康地址：$RSSHUB_BASE_URL" >&2
fi
if ! visudo -cf "$SCRIPT_DIR/sudoers.example" >/dev/null 2>&1; then
    die "sudoers source failed visudo validation"
fi

# Reject a pre-existing synchronizer account unless its identity and group
# boundary are exactly the one this installer expects.  Supplementary groups
# could otherwise grant access to Docker or other unrelated resources.
validate_existing_account() {
    passwd_line=$(getent passwd "$SSH_USER" 2>/dev/null) || die "cannot read $SSH_USER account"
    account_home=$(printf '%s\n' "$passwd_line" | awk -F: 'NR == 1 { print $6 }')
    account_shell=$(printf '%s\n' "$passwd_line" | awk -F: 'NR == 1 { print $7 }')
    [ "$account_home" = "$SSH_HOME" ] || die "existing $SSH_USER has an unexpected home"
    [ "$account_shell" = /bin/sh ] || die "existing $SSH_USER has an unexpected shell"
    [ "$(id -u "$SSH_USER")" -ne 0 ] || die "$SSH_USER must not have uid 0"
    [ "$(id -g "$SSH_USER")" -ne 0 ] || die "$SSH_USER must not have gid 0"
    primary_group=$(id -gn "$SSH_USER")
    all_groups=$(id -Gn "$SSH_USER")
    [ -n "$primary_group" ] || die "cannot determine $SSH_USER primary group"
    [ "$all_groups" = "$primary_group" ] || die "$SSH_USER has supplementary groups"
    [ ! -L "$account_home" ] || die "$SSH_USER home must not be a symlink"
    [ -d "$account_home" ] || die "$SSH_USER home is missing"
    [ "$(stat -c '%U' -- "$account_home")" = "$SSH_USER" ] \
        || die "$SSH_USER home has an unexpected owner"
    [ "$(stat -c '%G' -- "$account_home")" = "$primary_group" ] \
        || die "$SSH_USER home has an unexpected group"
    home_mode=$(stat -c '%A' -- "$account_home") \
        || die "cannot inspect $SSH_USER home permissions"
    case "$home_mode" in
        ?????w????|????????w?) die "$SSH_USER home must not be writable by group or others" ;;
    esac
    if [ -e "$account_home/.ssh" ] || [ -L "$account_home/.ssh" ]; then
        [ ! -L "$account_home/.ssh" ] && [ -d "$account_home/.ssh" ] \
            || die "$SSH_USER .ssh must be a regular directory"
        [ "$(stat -c '%U' -- "$account_home/.ssh")" = "$SSH_USER" ] \
            || die "$SSH_USER .ssh has an unexpected owner"
        [ "$(stat -c '%G' -- "$account_home/.ssh")" = "$primary_group" ] \
            || die "$SSH_USER .ssh has an unexpected group"
        [ "$(stat -c '%a' -- "$account_home/.ssh")" = 700 ] \
            || die "$SSH_USER .ssh permissions must be 0700"
    fi
}

validate_locked_account_password() {
    shadow_line=$(getent shadow "$SSH_USER" 2>/dev/null) \
        || die "cannot read $SSH_USER password state"
    password_field=$(printf '%s\n' "$shadow_line" | awk -F: 'NR == 1 { print $2 }')
    case "$password_field" in
        '!'*|'*'*) ;;
        *) die "existing $SSH_USER password must already be locked" ;;
    esac
}

if id "$SSH_USER" >/dev/null 2>&1; then
    validate_existing_account
    # Reusing a deliberately prepared account is supported, but the installer
    # must not silently lock an administrator's ordinary account and then be
    # unable to restore that password state during uninstall.
    validate_locked_account_password
elif [ -e "$SSH_HOME" ] || [ -L "$SSH_HOME" ]; then
    die "$SSH_HOME already exists without the managed account"
fi

# The server side never needs to know the private key.  A public key is only
# required when a fresh account has no usable project authorization.  On a
# reinstall, keep the exact existing authorized_keys line unless the operator
# explicitly supplies --public-key-file.
validate_public_key_file() {
    key_path=$1
    [ -n "$key_path" ] || return 1
    case "$key_path" in
        /*) ;;
        *) echo "公钥文件路径必须是绝对路径。" >&2; return 1 ;;
    esac
    [ ! -L "$key_path" ] || { echo "公钥文件不能是符号链接。" >&2; return 1; }
    [ -f "$key_path" ] || { echo "公钥文件不存在或不是普通文件。" >&2; return 1; }
    key_size=$(wc -c < "$key_path") || return 1
    [ "$key_size" -le 8192 ] || { echo "公钥文件过大。" >&2; return 1; }
    key_lines=$(awk 'END { print NR }' "$key_path") || return 1
    [ "$key_lines" -eq 1 ] || { echo "公钥必须只有一行。" >&2; return 1; }
    if ! /usr/bin/python3 -c 'import pathlib,sys; raise SystemExit(1 if b"\x00" in pathlib.Path(sys.argv[1]).read_bytes() else 0)' "$key_path"; then
        echo "公钥包含不允许的控制字符。" >&2
        return 1
    fi
    key_line=$(sed -n '1p' "$key_path") || return 1
    case "$key_line" in
        ssh-ed25519\ *) ;;
        *) echo "公钥必须是 ssh-ed25519 格式。" >&2; return 1 ;;
    esac
    case "$key_line" in
        *"$(printf '\r')"*)
            echo "公钥包含不允许的控制字符。" >&2
            return 1
            ;;
    esac
    if ! printf '%s\n' "$key_line" | ssh-keygen -lf - >/dev/null 2>&1; then
        echo "公钥不是有效的 OpenSSH 公钥。" >&2
        return 1
    fi
    return 0
}

assert_authorized_keys_path() {
    if [ -L "$AUTHORIZED_KEYS" ]; then
        die "$AUTHORIZED_KEYS must not be a symlink"
    fi
    if [ -e "$AUTHORIZED_KEYS" ] && [ ! -f "$AUTHORIZED_KEYS" ]; then
        die "$AUTHORIZED_KEYS is not a regular file"
    fi
}

# Return success only for the exact restricted line written by
# provision-ssh-key.sh.  The raw line is deliberately left untouched on a
# reinstall; this check is only a gate for deciding whether a new key is
# required.
has_valid_managed_authorized_key() {
    assert_authorized_keys_path
    [ -f "$AUTHORIZED_KEYS" ] || return 1
    auth_owner=$(stat -c '%U' -- "$AUTHORIZED_KEYS" 2>/dev/null || true)
    [ "$auth_owner" = "$SSH_USER" ] || return 1
    auth_mode=$(stat -c '%A' -- "$AUTHORIZED_KEYS" 2>/dev/null || true)
    case "$auth_mode" in
        ?????w????|????????w?) return 1 ;;
    esac
    managed_lines=$(awk '/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 / { count++ } END { print count + 0 }' \
        "$AUTHORIZED_KEYS" 2>/dev/null || echo 0)
    [ "$managed_lines" -eq 1 ] || return 1
    auth_line=$(sed -n '/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" ssh-ed25519 /p' \
        "$AUTHORIZED_KEYS" 2>/dev/null || true)
    case "$auth_line" in
        'restrict,command="sudo -n /usr/local/sbin/rsshub-cookie-sync-apply" ssh-ed25519 '* )
            auth_key=$(printf '%s\n' "$auth_line" | sed -n \
                's/^restrict,command="sudo -n \/usr\/local\/sbin\/rsshub-cookie-sync-apply" \(ssh-ed25519 [^ ]*\).*$/\1/p')
            [ -n "$auth_key" ] || return 1
            printf '%s\n' "$auth_key" | ssh-keygen -lf - >/dev/null 2>&1
            ;;
        *) return 1 ;;
    esac
}

read_public_key_from_tty() {
    [ -r /dev/tty ] || die "首次安装必须提供 SSH 公钥；请在交互式终端运行，或使用 --public-key-file"
    while :; do
        printf '%s' "请粘贴本机安装器输出的一整行 ssh-ed25519 公钥（输入 q 取消）： " >&2
        IFS= read -r key_line < /dev/tty || die "没有读取到 SSH 公钥，安装已取消"
        [ "$key_line" = q ] && die "安装已取消"
        key_tmp_input=$(mktemp /tmp/rsshub-cookie-sync-public-key.XXXXXX)
        chmod 0600 "$key_tmp_input"
        printf '%s\n' "$key_line" > "$key_tmp_input"
        if validate_public_key_file "$key_tmp_input" >/dev/null 2>&1; then
            return 0
        fi
        rm -f "$key_tmp_input"
        key_tmp_input=
        echo "公钥无效，请重新粘贴；不要粘贴私钥。" >&2
    done
}

stage_public_key() {
    if [ -n "$PUBLIC_KEY_FILE" ]; then
        validate_public_key_file "$PUBLIC_KEY_FILE" || die "公钥文件无效，安装已停止"
        key_tmp_input=$(mktemp /tmp/rsshub-cookie-sync-public-key.XXXXXX)
        chmod 0600 "$key_tmp_input"
        # The key is public, but keeping a private temporary mode avoids
        # surprising exposure in shared temporary directories.
        cp "$PUBLIC_KEY_FILE" "$key_tmp_input" || die "无法读取公钥文件"
        validate_public_key_file "$key_tmp_input" || die "公钥文件在读取时发生变化"
    elif [ "$existing_managed_key" -eq 0 ]; then
        if [ "$INTERACTIVE" -eq 1 ]; then
            read_public_key_from_tty
        else
            die "首次安装或现有授权无效；请使用 --public-key-file 提供 ssh-ed25519 公钥"
        fi
    fi
}

existing_managed_key=0
if id "$SSH_USER" >/dev/null 2>&1 && has_valid_managed_authorized_key; then
    existing_managed_key=1
fi

# A v1.1.2 installation predates the marker.  It is safe to migrate the
# marker only when all distinctive project assets already exist and the
# account has the exact forced-command key.  Otherwise the account remains
# pre-existing and the uninstaller will never delete it.
legacy_account_candidate=0
legacy_assets_match() {
    [ -f "$CONFIG_FILE" ] && [ -f "$SSH_CONFIG" ] \
        && [ -f "$SERVICE_UNIT" ] && [ -f "$TIMER_UNIT" ] \
        && [ -f "$INSTALL_DIR/rsshub_cookie_sync.py" ] \
        && [ -f "$INSTALL_DIR/rsshub-cookie-sync" ] \
        && [ -f "$SBIN_DIR/rsshub-cookie-sync-apply" ] \
        && [ -f "$SBIN_DIR/rsshub-cookie-sync-provision-key" ] \
        && [ -f /etc/sudoers.d/rsshub-cookie-sync ] || return 1
    cmp -s "$SCRIPT_DIR/rsshub_cookie_sync.py" "$INSTALL_DIR/rsshub_cookie_sync.py" || return 1
    cmp -s "$SCRIPT_DIR/rsshub-cookie-sync" "$INSTALL_DIR/rsshub-cookie-sync" || return 1
    cmp -s "$SCRIPT_DIR/rsshub-cookie-sync-apply" "$SBIN_DIR/rsshub-cookie-sync-apply" || return 1
    cmp -s "$SCRIPT_DIR/provision-ssh-key.sh" "$SBIN_DIR/rsshub-cookie-sync-provision-key" || return 1
    cmp -s "$SCRIPT_DIR/sudoers.example" /etc/sudoers.d/rsshub-cookie-sync || return 1
    cmp -s "$SCRIPT_DIR/sshd-rsshub-cookie-sync.conf" "$SSH_CONFIG" || return 1
    cmp -s "$SCRIPT_DIR/rsshub-cookie-sync-monitor.service" "$SERVICE_UNIT" || return 1
    cmp -s "$SCRIPT_DIR/rsshub-cookie-sync-monitor.timer" "$TIMER_UNIT" || return 1
    /usr/bin/python3 - "$CONFIG_FILE" <<'PY'
import json
import sys

try:
    with open(sys.argv[1], "r", encoding="utf-8") as handle:
        data = json.load(handle)
    deployment = data.get("deployment")
    required = {"compose_file", "live_env", "candidate_dir", "state_file", "lock_file", "project", "service"}
    if not isinstance(deployment, dict) or not required.issubset(deployment):
        raise ValueError
except Exception:
    raise SystemExit(1)
PY
}

if id "$SSH_USER" >/dev/null 2>&1 && [ ! -e "$ACCOUNT_MARKER" ] && [ "$existing_managed_key" -eq 1 ] \
    && legacy_assets_match; then
    legacy_account_candidate=1
fi

if [ -e "$ACCOUNT_MARKER" ] || [ -L "$ACCOUNT_MARKER" ]; then
    assert_root_file "$ACCOUNT_MARKER"
    marker_value=$(sed -n '1p' "$ACCOUNT_MARKER")
    [ "$marker_value" = "rsshub-cookie-sync-account-created=1" ] \
        || die "account-created marker is invalid"
fi

# Older releases used the same Match/ForceCommand boundary but may have
# changed comments or whitespace.  Treat semantically identical files as
# project-owned, so upgrading v1.1.2 does not create a backup that uninstall
# would later restore as if it were an administrator's SSH configuration.
ssh_config_matches_project() {
    [ -f "$SSH_CONFIG" ] || return 1
    if cmp -s "$SSH_CONFIG" "$SCRIPT_DIR/sshd-rsshub-cookie-sync.conf"; then
        return 0
    fi
    current_ssh_rules=$(sed '/^[[:space:]]*#/d;/^[[:space:]]*$/d' "$SSH_CONFIG") || return 1
    expected_ssh_rules=$(sed '/^[[:space:]]*#/d;/^[[:space:]]*$/d' \
        "$SCRIPT_DIR/sshd-rsshub-cookie-sync.conf") || return 1
    [ "$current_ssh_rules" = "$expected_ssh_rules" ]
}

install_ok=0
ssh_config_touched=0
ssh_config_existed=0
ssh_config_backup=
ssh_config_persistent_backup_created=0
systemd_units_touched=0
timer_touched=0
timer_was_active=0
timer_was_enabled=0
service_was_active=0
service_unit_existed=0
timer_unit_existed=0
dropin_existed=0
dropin_dir_existed=0
unit_backup_dir=
config_existed=0
config_backup=
config_prepared=0
migration_started=0
dropin_tmp=
asset_backup_dir=
account_created=0
account_marker_touched=0
account_marker_existed=0
install_marker_created=0
marker_tmp=
authorized_keys_touched=0
authorized_keys_existed=0
authorized_keys_backup=

backup_managed_asset() {
    asset_target=$1
    asset_name=$2
    if [ -e "$asset_target" ] || [ -L "$asset_target" ]; then
        assert_root_file "$asset_target"
        cp -p "$asset_target" "$asset_backup_dir/$asset_name"
        install -m 0600 /dev/null "$asset_backup_dir/$asset_name.existed"
    fi
    install -m 0600 /dev/null "$asset_backup_dir/$asset_name.checked"
}

restore_managed_asset() {
    asset_target=$1
    asset_name=$2
    [ -f "$asset_backup_dir/$asset_name.checked" ] || return 0
    rm -f "$asset_target"
    if [ -f "$asset_backup_dir/$asset_name.existed" ]; then
        cp -p "$asset_backup_dir/$asset_name" "$asset_target"
    fi
}

restore_installed_assets() {
    [ -n "$asset_backup_dir" ] || return 0
    restore_managed_asset "$INSTALL_DIR/rsshub_cookie_sync.py" python
    restore_managed_asset "$INSTALL_DIR/rsshub-cookie-sync" launcher
    restore_managed_asset "$SBIN_DIR/rsshub-cookie-sync-apply" apply
    restore_managed_asset "$SBIN_DIR/rsshub-cookie-sync-provision-key" provision
    restore_managed_asset "$SBIN_DIR/rsshub-cookie-sync-uninstall" uninstall
    restore_managed_asset /etc/sudoers.d/rsshub-cookie-sync sudoers
    rmdir "$INSTALL_DIR" 2>/dev/null || true
}

backup_authorized_keys() {
    assert_authorized_keys_path
    if [ -e "$AUTHORIZED_KEYS" ]; then
        auth_owner=$(stat -c '%U' -- "$AUTHORIZED_KEYS") || die "cannot inspect $AUTHORIZED_KEYS"
        [ "$auth_owner" = "$SSH_USER" ] || die "$AUTHORIZED_KEYS is not owned by $SSH_USER"
        auth_mode=$(stat -c '%A' -- "$AUTHORIZED_KEYS") || die "cannot inspect $AUTHORIZED_KEYS permissions"
        case "$auth_mode" in
            ?????w????|????????w?) die "$AUTHORIZED_KEYS must not be writable by group or others" ;;
        esac
        authorized_keys_existed=1
        authorized_keys_backup=$(mktemp /tmp/rsshub-cookie-sync-authorized-keys.XXXXXX)
        cp -p "$AUTHORIZED_KEYS" "$authorized_keys_backup"
        chmod 0600 "$authorized_keys_backup"
    fi
}

restore_authorized_keys() {
    [ "$authorized_keys_touched" -eq 1 ] || return 0
    rm -f "$AUTHORIZED_KEYS"
    if [ "$authorized_keys_existed" -eq 1 ]; then
        cp -p "$authorized_keys_backup" "$AUTHORIZED_KEYS"
        chown "$SSH_USER:$SSH_GROUP" "$AUTHORIZED_KEYS"
        chmod 0600 "$AUTHORIZED_KEYS"
    fi
}

write_account_marker() {
    [ "$account_created" -eq 1 ] || [ "$legacy_account_candidate" -eq 1 ] || return 0
    if [ -e "$ACCOUNT_MARKER" ]; then
        account_marker_existed=1
        return 0
    fi
    install -m 0600 /dev/null "$ACCOUNT_MARKER"
    printf '%s\n' "rsshub-cookie-sync-account-created=1" > "$ACCOUNT_MARKER"
    account_marker_touched=1
}

restore_account_marker() {
    [ "$account_marker_touched" -eq 1 ] || return 0
    if [ "$account_marker_existed" -eq 0 ]; then
        rm -f "$ACCOUNT_MARKER"
    fi
}

restore_install_marker() {
    [ "$install_marker_created" -eq 1 ] || return 0
    rm -f "$INSTALL_MARKER"
}

restore_ssh_config() {
    [ "$ssh_config_touched" -eq 1 ] || return 0
    rm -f "$SSH_CONFIG"
    if [ "$ssh_config_existed" -eq 1 ]; then
        install -m 0644 "$ssh_config_backup" "$SSH_CONFIG"
    fi
    if systemctl reload sshd >/dev/null 2>&1; then
        :
    elif systemctl reload ssh >/dev/null 2>&1; then
        :
    else
        echo "rsshub-cookie-sync install: failed to reload restored sshd configuration" >&2
        return 1
    fi
}

restore_unit_file() {
    restore_target=$1
    restore_existed=$2
    restore_backup=$3
    rm -f "$restore_target"
    if [ "$restore_existed" -eq 1 ]; then
        install -m 0644 "$restore_backup" "$restore_target"
    fi
}

restore_systemd_units() {
    [ "$systemd_units_touched" -eq 1 ] || return 0
    restore_unit_file "$SERVICE_UNIT" "$service_unit_existed" "$unit_backup_dir/service"
    restore_unit_file "$TIMER_UNIT" "$timer_unit_existed" "$unit_backup_dir/timer"
    rm -f "$DROPIN_FILE"
    if [ "$dropin_existed" -eq 1 ]; then
        install -m 0600 "$unit_backup_dir/dropin" "$DROPIN_FILE"
    fi
    if [ "$dropin_dir_existed" -eq 0 ]; then
        rmdir "$DROPIN_DIR" 2>/dev/null || true
    fi
    systemctl daemon-reload >/dev/null 2>&1 || return 1
}

restore_config() {
    [ "$config_prepared" -eq 1 ] || return 0
    if [ "$config_existed" -eq 1 ]; then
        install -m 0600 "$config_backup" "$CONFIG_FILE"
    else
        rm -f "$CONFIG_FILE"
    fi
}

rollback_install() {
    rollback_failed=0

    # Stop the scheduler before restoring the migration transaction.  This
    # prevents a monitor invocation from racing with rollback.
    if [ "$timer_touched" -eq 1 ]; then
        if ! systemctl disable --now rsshub-cookie-sync-monitor.timer >/dev/null 2>&1; then
            rollback_failed=1
            echo "rsshub-cookie-sync install: failed to stop monitor timer during rollback" >&2
        fi
    fi
    if ! restore_systemd_units; then
        rollback_failed=1
        echo "rsshub-cookie-sync install: failed to restore systemd units" >&2
    fi
    if ! restore_ssh_config; then
        rollback_failed=1
    fi

    # The deployment config must remain in place until rollback-migration has
    # located the transaction marker.  The Python command emits no secret.
    if [ "$migration_started" -eq 1 ]; then
        if ! /usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
            --config "$CONFIG_FILE" rollback-migration --json >/dev/null 2>&1; then
            rollback_failed=1
            echo "rsshub-cookie-sync install: RSSHub migration rollback failed" >&2
        fi
    fi
    if ! restore_config; then
        rollback_failed=1
        echo "rsshub-cookie-sync install: failed to restore deployment config" >&2
    fi
    if ! restore_installed_assets; then
        rollback_failed=1
        echo "rsshub-cookie-sync install: failed to restore installed program files" >&2
    fi
    if ! restore_authorized_keys; then
        rollback_failed=1
        echo "rsshub-cookie-sync install: failed to restore authorized_keys" >&2
    fi
    restore_account_marker
    restore_install_marker
    if [ "$account_created" -eq 1 ] && id "$SSH_USER" >/dev/null 2>&1; then
        if ! userdel --remove "$SSH_USER" >/dev/null 2>&1; then
            rollback_failed=1
            echo "rsshub-cookie-sync install: failed to remove the newly created SSH account" >&2
        fi
    fi

    if [ "$timer_was_enabled" -eq 1 ]; then
        if ! systemctl enable rsshub-cookie-sync-monitor.timer >/dev/null 2>&1; then
            rollback_failed=1
            echo "rsshub-cookie-sync install: failed to restore timer enablement" >&2
        fi
    fi
    if [ "$timer_was_active" -eq 1 ]; then
        if ! systemctl start rsshub-cookie-sync-monitor.timer >/dev/null 2>&1; then
            rollback_failed=1
            echo "rsshub-cookie-sync install: failed to restore monitor timer" >&2
        fi
    fi
    if [ "$service_was_active" -eq 1 ]; then
        if ! systemctl start rsshub-cookie-sync-monitor.service >/dev/null 2>&1; then
            rollback_failed=1
            echo "rsshub-cookie-sync install: failed to restart monitor service" >&2
        fi
    fi

    if [ -n "$dropin_tmp" ]; then
        rm -f "$dropin_tmp"
    fi
    if [ -n "$marker_tmp" ]; then
        rm -f "$marker_tmp"
    fi
    if [ -n "$unit_backup_dir" ]; then
        rm -rf "$unit_backup_dir"
    fi
    if [ -n "$ssh_config_backup" ]; then
        rm -f "$ssh_config_backup"
    fi
    if [ "$ssh_config_persistent_backup_created" -eq 1 ]; then
        rm -f "$SSH_CONFIG_PERSISTENT_BACKUP" "$SSH_CONFIG_BACKUP_MARKER"
    fi
    if [ -n "$config_backup" ]; then
        rm -f "$config_backup"
    fi
    if [ -n "$asset_backup_dir" ]; then
        rm -rf "$asset_backup_dir"
    fi
    if [ -n "$authorized_keys_backup" ]; then
        rm -f "$authorized_keys_backup"
    fi
    if [ -n "$key_tmp_input" ]; then
        rm -f "$key_tmp_input"
    fi
    if [ "$rollback_failed" -ne 0 ]; then
        echo "rsshub-cookie-sync install: rollback was incomplete; inspect the host before retrying" >&2
    fi
}

handle_signal() {
    # Let the EXIT trap perform exactly one rollback.  Calling the rollback
    # function directly from both a signal trap and EXIT could otherwise run
    # destructive cleanup twice.
    trap - HUP INT TERM
    exit 1
}

trap rollback_install EXIT
trap handle_signal HUP INT TERM

# Validate and stage the public key before any deployment mutation.  The EXIT
# trap is already active so a cancelled/failed prompt cannot leave a temporary
# key file behind.
key_tmp_input=
stage_public_key

finish_interactive_setup() {
    [ "$INTERACTIVE" -eq 1 ] || return 0
    [ -r /dev/tty ] || return 0

    echo >&2
    if ask_yes_no "现在配置 Bark 通知吗"; then
        # configure-bark reads from the terminal itself and uses getpass, so
        # the Device Key is neither an argument nor an echoed shell input.
        if /usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
            --config "$CONFIG_FILE" configure-bark < /dev/tty >/dev/null; then
            echo "Bark 已配置。" >&2
        else
            echo "Bark 配置失败；安装已完成，可稍后重新运行 configure-bark。" >&2
        fi
    fi

}

# Freeze the existing scheduler before touching config, Compose, or its
# secret env.  Install the rollback trap first so even a partial systemctl
# failure restores the timer's previous enablement and active state.  Stopping
# the timer alone does not cancel a oneshot it already started, so wait for
# that service to stop as well before replacing its config or acquiring the
# migration lock.
if systemctl is-active --quiet rsshub-cookie-sync-monitor.timer; then
    timer_was_active=1
fi
if systemctl is-enabled --quiet rsshub-cookie-sync-monitor.timer; then
    timer_was_enabled=1
fi
if [ "$timer_was_active" -eq 1 ] || [ "$timer_was_enabled" -eq 1 ]; then
    timer_touched=1
    if ! systemctl disable --now rsshub-cookie-sync-monitor.timer >/dev/null 2>&1; then
        die "failed to stop existing monitor timer"
    fi
fi
if systemctl is-active --quiet rsshub-cookie-sync-monitor.service; then
    service_was_active=1
    if ! systemctl stop rsshub-cookie-sync-monitor.service >/dev/null 2>&1; then
        die "failed to stop the running monitor service"
    fi
fi

# Install the application before the first Python command.  These paths are
# root-owned and contain no runtime Cookie values.
asset_backup_dir=$(mktemp -d /tmp/rsshub-cookie-sync-assets.XXXXXX)
backup_managed_asset "$INSTALL_DIR/rsshub_cookie_sync.py" python
backup_managed_asset "$INSTALL_DIR/rsshub-cookie-sync" launcher
backup_managed_asset "$SBIN_DIR/rsshub-cookie-sync-apply" apply
backup_managed_asset "$SBIN_DIR/rsshub-cookie-sync-provision-key" provision
backup_managed_asset "$SBIN_DIR/rsshub-cookie-sync-uninstall" uninstall
backup_managed_asset /etc/sudoers.d/rsshub-cookie-sync sudoers
install -d -m 0755 "$INSTALL_DIR" "$SBIN_DIR"
install -d -m 0700 "$CONFIG_DIR" "$STATE_DIR"
install -d -m 0700 "$SECRETS_DIR" "$CANDIDATE_DIR"
install -m 0755 "$SCRIPT_DIR/rsshub_cookie_sync.py" "$INSTALL_DIR/rsshub_cookie_sync.py"
install -m 0755 "$SCRIPT_DIR/rsshub-cookie-sync" "$INSTALL_DIR/rsshub-cookie-sync"
install -m 0755 "$SCRIPT_DIR/rsshub-cookie-sync-apply" "$SBIN_DIR/rsshub-cookie-sync-apply"
install -m 0755 "$SCRIPT_DIR/provision-ssh-key.sh" "$SBIN_DIR/rsshub-cookie-sync-provision-key"
install -m 0755 "$SCRIPT_DIR/uninstall.sh" "$SBIN_DIR/rsshub-cookie-sync-uninstall"

# Preserve the existing configuration while configure-deployment merges the
# target fields.  The backup is removed on both success and rollback.
if [ -e "$CONFIG_FILE" ]; then
    config_existed=1
    config_backup=$(mktemp /tmp/rsshub-cookie-sync-config.XXXXXX)
    cp -p "$CONFIG_FILE" "$config_backup"
    chmod 0600 "$config_backup"
else
    install -m 0600 "$SCRIPT_DIR/config.example.json" "$CONFIG_FILE"
fi
config_prepared=1
chmod 0600 "$CONFIG_FILE"

# Pass only non-secret deployment metadata to the config command.  The
# explicit --config makes a reinstall independent of process environment.
if [ "$REPLACE_DEPLOYMENT" -eq 1 ]; then
    /usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
        --config "$CONFIG_FILE" configure-deployment \
        --compose-file "$COMPOSE_FILE" \
        --live-env "$LIVE_ENV" \
        --candidate-dir "$CANDIDATE_DIR" \
        --state-file "$STATE_FILE" \
        --lock-file "$LOCK_FILE" \
        --project "$PROJECT" \
        --service "$SERVICE" \
        --rsshub-base-url "$RSSHUB_BASE_URL" \
        --replace-deployment >/dev/null
else
    /usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
        --config "$CONFIG_FILE" configure-deployment \
        --compose-file "$COMPOSE_FILE" \
        --live-env "$LIVE_ENV" \
        --candidate-dir "$CANDIDATE_DIR" \
        --state-file "$STATE_FILE" \
        --lock-file "$LOCK_FILE" \
        --project "$PROJECT" \
        --service "$SERVICE" \
        --rsshub-base-url "$RSSHUB_BASE_URL" >/dev/null
fi

if ! id "$SSH_USER" >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir "$SSH_HOME" \
        --shell /bin/sh "$SSH_USER"
    account_created=1
fi
validate_existing_account
usermod --shell /bin/sh --lock "$SSH_USER"
validate_locked_account_password
SSH_GROUP=$(id -gn "$SSH_USER")
if [ -L "$SSH_HOME/.ssh" ]; then
    die "$SSH_HOME/.ssh must not be a symlink"
fi
if [ -e "$SSH_HOME/.ssh" ] && [ ! -d "$SSH_HOME/.ssh" ]; then
    die "$SSH_HOME/.ssh is not a directory"
fi
install -d -o "$SSH_USER" -g "$SSH_GROUP" -m 0700 "$SSH_HOME/.ssh"

# Keep the prior authorized_keys file available until the whole installation
# transaction has succeeded.  A supplied --public-key-file is an explicit key
# replacement; otherwise a valid existing restricted key is preserved byte for
# byte.  Fresh installs and legacy installs without a valid key must have one.
backup_authorized_keys
if [ -n "$key_tmp_input" ]; then
    authorized_keys_touched=1
    if ! "$SCRIPT_DIR/provision-ssh-key.sh" < "$key_tmp_input" >/dev/null 2>&1; then
        die "SSH 公钥安装失败"
    fi
fi
write_account_marker

install -m 0440 "$SCRIPT_DIR/sudoers.example" /etc/sudoers.d/rsshub-cookie-sync
if ! visudo -cf /etc/sudoers.d/rsshub-cookie-sync >/dev/null 2>&1; then
    die "installed sudoers file failed visudo validation"
fi

if [ -L "$SSH_CONFIG" ]; then
    die "$SSH_CONFIG must not be a symlink"
fi
if [ -e "$SSH_CONFIG" ]; then
    [ -f "$SSH_CONFIG" ] || die "$SSH_CONFIG is not a regular file"
    ssh_config_existed=1
    ssh_config_backup=$(mktemp /tmp/rsshub-cookie-sync-sshd.XXXXXX)
    cp -p "$SSH_CONFIG" "$ssh_config_backup"
    chmod 0600 "$ssh_config_backup"
    if [ "$PERSISTENT_SSH_BACKUP_EXISTS" -eq 0 ] && ! ssh_config_matches_project; then
        if [ -e "$SSH_CONFIG_PERSISTENT_BACKUP" ] || [ -L "$SSH_CONFIG_PERSISTENT_BACKUP" ] \
            || [ -e "$SSH_CONFIG_BACKUP_MARKER" ] || [ -L "$SSH_CONFIG_BACKUP_MARKER" ]; then
            die "SSH 配置备份路径已被占用，无法安全保存原文件"
        fi
        install -m 0600 "$ssh_config_backup" "$SSH_CONFIG_PERSISTENT_BACKUP"
        install -m 0600 /dev/null "$SSH_CONFIG_BACKUP_MARKER"
        printf '%s\n' "rsshub-cookie-sync-sshd-backup=1" > "$SSH_CONFIG_BACKUP_MARKER"
        ssh_config_persistent_backup_created=1
    fi
fi
install -m 0644 "$SCRIPT_DIR/sshd-rsshub-cookie-sync.conf" "$SSH_CONFIG"
ssh_config_touched=1
if ! sshd -t >/dev/null 2>&1; then
    die "sshd configuration validation failed"
fi
if systemctl reload sshd >/dev/null 2>&1; then
    :
elif systemctl reload ssh >/dev/null 2>&1; then
    :
else
    die "sshd reload failed"
fi

# Back up both unit files and this installer-owned drop-in before replacing
# them.  Unrelated drop-ins in the same directory are preserved.
unit_backup_dir=$(mktemp -d /tmp/rsshub-cookie-sync-units.XXXXXX)
if [ -L "$SERVICE_UNIT" ] || [ -L "$TIMER_UNIT" ]; then
    die "systemd unit target must not be a symlink"
fi
if [ -e "$SERVICE_UNIT" ]; then
    [ -f "$SERVICE_UNIT" ] || die "$SERVICE_UNIT is not a regular file"
    service_unit_existed=1
    cp -p "$SERVICE_UNIT" "$unit_backup_dir/service"
fi
if [ -e "$TIMER_UNIT" ]; then
    [ -f "$TIMER_UNIT" ] || die "$TIMER_UNIT is not a regular file"
    timer_unit_existed=1
    cp -p "$TIMER_UNIT" "$unit_backup_dir/timer"
fi
if [ -L "$DROPIN_DIR" ]; then
    die "$DROPIN_DIR must not be a symlink"
fi
if [ -e "$DROPIN_DIR" ]; then
    assert_root_dir "$DROPIN_DIR"
    dropin_dir_existed=1
fi
if [ -e "$DROPIN_FILE" ] || [ -L "$DROPIN_FILE" ]; then
    assert_root_file "$DROPIN_FILE"
    dropin_existed=1
    cp -p "$DROPIN_FILE" "$unit_backup_dir/dropin"
fi
systemd_units_touched=1
install -m 0644 "$SCRIPT_DIR/rsshub-cookie-sync-monitor.service" "$SERVICE_UNIT"
install -m 0644 "$SCRIPT_DIR/rsshub-cookie-sync-monitor.timer" "$TIMER_UNIT"
install -d -m 0755 "$DROPIN_DIR"
dropin_tmp=$(mktemp /tmp/rsshub-cookie-sync-deployment.XXXXXX)
umask 022
printf '%s\n' \
    '[Service]' \
    "ReadWritePaths=$SECRETS_DIR /var/lib/rsshub-cookie-sync" \
    >"$dropin_tmp"
install -m 0600 "$dropin_tmp" "$DROPIN_FILE"
rm -f "$dropin_tmp"
dropin_tmp=
systemctl daemon-reload

# The migration command returns a small, fixed-shape JSON object.  Capture it
# rather than printing it so even future safe metadata cannot clutter install
# output.  No rendered Compose configuration is requested.
migration_started=1
migration_json=$(/usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
    --config "$CONFIG_FILE" migrate-compose --json)
migration_pending=$(printf '%s\n' "$migration_json" | /usr/bin/python3 -c \
    'import json,sys; value=json.load(sys.stdin); pending=value.get("migration_pending"); type(pending) is bool or sys.exit(2); print("true" if pending else "false")')
chmod 0600 "$COMPOSE_FILE" "$LIVE_ENV"

# A finalized installation reports already_migrated and must not recreate
# RSSHub.  Only a real pending migration performs the one-time bootstrap and
# finalizes its temporary rollback files.
if [ "$migration_pending" = true ]; then
    /usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
        --config "$CONFIG_FILE" bootstrap --json >/dev/null
fi

# Validate the post-migration document without printing it.  This catches a
# Compose implementation that accepted the original file but rejects raw
# env_file syntax, before the timer is enabled.
if ! docker compose -p "$PROJECT" -f "$COMPOSE_FILE" config --quiet >/dev/null 2>&1; then
    die "migrated Compose configuration is invalid"
fi

timer_touched=1
systemctl enable --now rsshub-cookie-sync-monitor.timer

if [ ! -e "$INSTALL_MARKER" ]; then
    write_install_marker
fi

# Do not print a success message merely because the mutation commands returned
# zero.  Verify every externally visible boundary the installer promises:
# account/key restrictions, SSH and sudo syntax, loaded units, active timer,
# and the root-only deployment configuration.
validate_existing_account
validate_locked_account_password
has_valid_managed_authorized_key || die "installed rsshub-sync authorization failed validation"
visudo -cf /etc/sudoers.d/rsshub-cookie-sync >/dev/null 2>&1 \
    || die "installed sudoers validation failed"
sshd -t >/dev/null 2>&1 || die "installed sshd configuration validation failed"
assert_root_file "$CONFIG_FILE"
assert_root_file "$INSTALL_MARKER"
install_marker_is_valid || die "install ownership manifest failed validation"
[ "$(systemctl show -p LoadState --value rsshub-cookie-sync-monitor.service)" = loaded ] \
    || die "monitor service is not loaded"
[ "$(systemctl show -p LoadState --value rsshub-cookie-sync-monitor.timer)" = loaded ] \
    || die "monitor timer is not loaded"
systemctl is-enabled --quiet rsshub-cookie-sync-monitor.timer \
    || die "monitor timer is not enabled"
systemctl is-active --quiet rsshub-cookie-sync-monitor.timer \
    || die "monitor timer is not active"

# Finalization deletes the temporary Compose/env backups.  Keep it as the last
# fallible installation step so every validation failure above can still use
# those backups through rollback-migration.
if [ "$migration_pending" = true ]; then
    /usr/bin/python3 "$INSTALL_DIR/rsshub_cookie_sync.py" \
        --config "$CONFIG_FILE" finalize-migration --json >/dev/null
fi

install_ok=1
trap - EXIT HUP INT TERM
if [ -n "$unit_backup_dir" ]; then
    rm -rf "$unit_backup_dir"
fi
if [ -n "$ssh_config_backup" ]; then
    rm -f "$ssh_config_backup"
fi
if [ -n "$config_backup" ]; then
    rm -f "$config_backup"
fi
if [ -n "$asset_backup_dir" ]; then
    rm -rf "$asset_backup_dir"
fi
if [ -n "$authorized_keys_backup" ]; then
    rm -f "$authorized_keys_backup"
fi
if [ -n "$key_tmp_input" ]; then
    rm -f "$key_tmp_input"
fi
finish_interactive_setup
if [ "$INTERACTIVE" -eq 1 ]; then
    if [ "$existing_managed_key" -eq 1 ] && [ "$authorized_keys_touched" -eq 0 ]; then
        echo "已保留现有的 rsshub-sync SSH 公钥。" >&2
    else
        echo "SSH 公钥已安装到 rsshub-sync。" >&2
    fi
    echo "RSSHub Cookie sync 服务端安装完成。" >&2
else
    echo "RSSHub Cookie sync server installed. The rsshub-sync key is configured; configure Bark through stdin, then run notify-test."
fi
