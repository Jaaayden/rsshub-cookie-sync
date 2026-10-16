#!/bin/sh
set -eu

# Install the macOS Native Messaging host from the latest tagged source.
#
# Intended usage:
#   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-macos.sh | sh
#
# This bootstrap is deliberately a normal-user operation.  It never asks for
# root, never reads a private key, and leaves only the public key as the final
# stdout line so callers may capture it without mixing in status messages.

PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LC_ALL=C
export LC_ALL

die() {
    echo "rsshub-cookie-sync macOS bootstrap: $*" >&2
    exit 1
}

[ "$(id -u)" -ne 0 ] || die "请使用普通 macOS 用户运行；不要使用 root"
command -v python3 >/dev/null 2>&1 || die "缺少 python3"
command -v curl >/dev/null 2>&1 || die "缺少 curl"
command -v tar >/dev/null 2>&1 || die "缺少 tar"
command -v find >/dev/null 2>&1 || die "缺少 find"
command -v mktemp >/dev/null 2>&1 || die "缺少 mktemp"
command -v awk >/dev/null 2>&1 || die "缺少 awk"
if command -v shasum >/dev/null 2>&1; then
    sha256_of() { shasum -a 256 "$1" | awk '{ print $1 }'; }
elif command -v sha256sum >/dev/null 2>&1; then
    sha256_of() { sha256sum "$1" | awk '{ print $1 }'; }
else
    die "缺少 shasum 或 sha256sum"
fi
command -v uname >/dev/null 2>&1 || die "缺少 uname"

[ "$(uname -s)" = "Darwin" ] || die "此安装器仅支持 macOS (Darwin)"

PYTHON=$(command -v python3)
if ! "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)'; then
    die "python3 版本必须不低于 3.9"
fi

ACTION=install
if [ "$#" -gt 0 ] && [ "$1" = uninstall ]; then
    ACTION=uninstall
    shift
fi

DEFAULT_APP_SUPPORT_DIR="$HOME/Library/Application Support/RSSHub Cookie Sync"
DEFAULT_EDGE_MANIFEST_DIR="$HOME/Library/Application Support/Microsoft Edge/NativeMessagingHosts"
DEFAULT_UNINSTALL_DIR="$HOME/Library/Application Support/rsshub-cookie-sync"
INSTALLED_UNINSTALLER=$DEFAULT_UNINSTALL_DIR/uninstall.sh

if [ "$ACTION" = uninstall ] && [ -e "$INSTALLED_UNINSTALLER" ]; then
    [ -f "$INSTALLED_UNINSTALLER" ] || die "已安装的卸载器不是普通文件"
    [ ! -L "$INSTALLED_UNINSTALLER" ] || die "已安装的卸载器是符号链接"
    [ -x "$INSTALLED_UNINSTALLER" ] || die "已安装的卸载器不可执行"
    "$INSTALLED_UNINSTALLER" "$@"
    exit $?
fi

# Both files come from the same "latest" release; if a new release appears
# between the two requests the checksum fails to match and nothing is
# installed.  The redirecting download URL also avoids the rate-limited
# unauthenticated GitHub API.
RELEASE_URL=https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download
SOURCE_NAME=rsshub-cookie-sync-source.tar.gz
TEMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/rsshub-cookie-sync.XXXXXX")
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

ARCHIVE_FILE=$TEMP_DIR/$SOURCE_NAME
echo "正在下载 RSSHub Cookie Sync。" >&2
curl -fsSL --proto '=https' --tlsv1.2 "$RELEASE_URL/$SOURCE_NAME" \
    -o "$ARCHIVE_FILE" || die "源码下载失败；请确认最新 Release 已发布"
curl -fsSL --proto '=https' --tlsv1.2 "$RELEASE_URL/SHA256SUMS" \
    -o "$TEMP_DIR/SHA256SUMS" || die "校验文件下载失败"

EXPECTED_SHA=$(awk -v name="$SOURCE_NAME" '$2 == name { print $1; exit }' "$TEMP_DIR/SHA256SUMS")
case "$EXPECTED_SHA" in
    *[!0-9a-f]*|"") die "SHA256SUMS 中没有有效的源码校验值" ;;
esac
[ "${#EXPECTED_SHA}" -eq 64 ] || die "SHA256SUMS 中没有有效的源码校验值"
[ "$(sha256_of "$ARCHIVE_FILE")" = "$EXPECTED_SHA" ] || die "源码归档校验失败，已停止安装"

# Refuse absolute paths, traversal, duplicate separators, and links/special
# files before extracting an archive supplied by the network.
if ! tar -tzf "$ARCHIVE_FILE" | awk '
    BEGIN { ok = 1 }
    /^\// || /(^|\/)\.\.?($|\/)/ || /\/\// { ok = 0 }
    END { exit(ok ? 0 : 1) }
'; then
    die "源码归档包含不安全路径"
fi
if ! tar -tvzf "$ARCHIVE_FILE" | awk '
    substr($1, 1, 1) != "-" && substr($1, 1, 1) != "d" { bad = 1 }
    END { exit(bad ? 1 : 0) }
'; then
    die "源码归档包含不支持的链接或特殊文件"
fi
tar -xzf "$ARCHIVE_FILE" -C "$TEMP_DIR" \
    || die "源码解压失败"

INSTALLER=$(find "$TEMP_DIR" -type f -path '*/native-host/install.py' -print -quit)
[ -n "$INSTALLER" ] || die "下载内容缺少 native-host/install.py"

if [ "$ACTION" = uninstall ]; then
    UNINSTALLER=$(find "$TEMP_DIR" -type f -path '*/native-host/uninstall.py' -print -quit)
    [ -n "$UNINSTALLER" ] || die "下载内容缺少 native-host/uninstall.py"
    # A curl | sh pipeline does not leave stdin attached to the terminal.
    # Reattach it when possible so the uninstaller can still ask whether the
    # dedicated key should be removed; a headless run keeps the safe default.
    if [ -r /dev/tty ]; then
        if "$PYTHON" "$UNINSTALLER" \
            --app-support-dir "$DEFAULT_APP_SUPPORT_DIR" \
            --edge-manifest-dir "$DEFAULT_EDGE_MANIFEST_DIR" \
            --uninstall-dir "$DEFAULT_UNINSTALL_DIR" "$@" < /dev/tty; then
            uninstall_status=0
        else
            uninstall_status=$?
        fi
    else
        if "$PYTHON" "$UNINSTALLER" \
            --app-support-dir "$DEFAULT_APP_SUPPORT_DIR" \
            --edge-manifest-dir "$DEFAULT_EDGE_MANIFEST_DIR" \
            --uninstall-dir "$DEFAULT_UNINSTALL_DIR" "$@"; then
            uninstall_status=0
        else
            uninstall_status=$?
        fi
    fi
    if [ "$uninstall_status" -eq 0 ]; then
        exit 0
    else
        exit "$uninstall_status"
    fi
fi

OUTPUT_FILE=$TEMP_DIR/install.stdout
STATUS_FILE=$TEMP_DIR/install.status
KEY_FILE=$TEMP_DIR/install.public-key
if ! "$PYTHON" "$INSTALLER" "$@" >"$OUTPUT_FILE"; then
    cat "$OUTPUT_FILE" >&2
    die "Native Host 安装失败"
fi

# The Python installer writes the public key as its only stdout payload.  The
# split is still performed here so an older tagged installer cannot accidentally
# leak status text to a caller expecting a single public-key line.
if ! awk -v status="$STATUS_FILE" -v key="$KEY_FILE" '
    NF { lines[++count] = $0 }
    END {
        if (count < 1) exit 1
        for (i = 1; i < count; i++) print lines[i] > status
        print lines[count] > key
    }
' "$OUTPUT_FILE"; then
    cat "$OUTPUT_FILE" >&2
    die "安装器没有输出公钥"
fi
cat "$STATUS_FILE" >&2 2>/dev/null || true

# Validate the shape before emitting it.  ssh-keygen on the target host will
# perform the authoritative key validation again when the operator provisions
# it, but this prevents status/path text from becoming the final stdout line.
if ! awk '
    NR != 1 { bad = 1 }
    NF < 2 { bad = 1 }
    $1 != "ssh-ed25519" { bad = 1 }
    $2 !~ /^[A-Za-z0-9+\/=]+$/ { bad = 1 }
    END { exit(bad ? 1 : 0) }
' "$KEY_FILE"; then
    cat "$KEY_FILE" >&2
    die "安装器输出的不是有效 Ed25519 公钥"
fi
cat "$KEY_FILE"
