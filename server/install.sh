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
SSH_CONFIG=/etc/ssh/sshd_config.d/rsshub-cookie-sync.conf
SERVICE_UNIT=/etc/systemd/system/rsshub-cookie-sync-monitor.service
TIMER_UNIT=/etc/systemd/system/rsshub-cookie-sync-monitor.timer
DROPIN_DIR=/etc/systemd/system/rsshub-cookie-sync-monitor.service.d
DROPIN_FILE=$DROPIN_DIR/deployment.conf

COMPOSE_DIR=$(dirname -- "$COMPOSE_FILE")
SECRETS_DIR=$COMPOSE_DIR/secrets
LIVE_ENV=$SECRETS_DIR/rsshub.env
CANDIDATE_DIR=$SECRETS_DIR/candidates

for command_name in \
    id install useradd userdel usermod getent sshd visudo stat cp mktemp rm chmod chown \
    mv awk python3 docker dirname; do
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
}

if id "$SSH_USER" >/dev/null 2>&1; then
    validate_existing_account
elif [ -e "$SSH_HOME" ] || [ -L "$SSH_HOME" ]; then
    die "$SSH_HOME already exists without the managed account"
fi

install_ok=0
ssh_config_touched=0
ssh_config_existed=0
ssh_config_backup=
systemd_units_touched=0
timer_touched=0
timer_was_active=0
timer_was_enabled=0
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

    if [ -n "$dropin_tmp" ]; then
        rm -f "$dropin_tmp"
    fi
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

    echo >&2
    if ask_yes_no "现在安装 Edge Native Host 的 SSH 公钥吗"; then
        echo "请粘贴一整行 ssh-ed25519 公钥后按回车（仅接受一行，公钥不是私钥）：" >&2
        IFS= read -r public_key < /dev/tty || public_key=
        if [ -n "$public_key" ] && printf '%s\n' "$public_key" | \
            "$SBIN_DIR/rsshub-cookie-sync-provision-key" >/dev/null 2>&1; then
            echo "SSH 公钥已安装。" >&2
        else
            echo "SSH 公钥安装失败；安装已完成，可稍后运行 rsshub-cookie-sync-provision-key。" >&2
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
SSH_GROUP=$(id -gn "$SSH_USER")
if [ -L "$SSH_HOME/.ssh" ]; then
    die "$SSH_HOME/.ssh must not be a symlink"
fi
if [ -e "$SSH_HOME/.ssh" ] && [ ! -d "$SSH_HOME/.ssh" ]; then
    die "$SSH_HOME/.ssh is not a directory"
fi
install -d -o "$SSH_USER" -g "$SSH_GROUP" -m 0700 "$SSH_HOME/.ssh"

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
finish_interactive_setup
if [ "$INTERACTIVE" -eq 1 ]; then
    echo "RSSHub Cookie sync 服务端安装完成。" >&2
else
    echo "RSSHub Cookie sync server installed. Add a public key with rsshub-cookie-sync-provision-key, configure Bark through stdin, then run notify-test."
fi
