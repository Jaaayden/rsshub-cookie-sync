#!/bin/sh
set -eu

# RSSHub Cookie Sync server bootstrapper.
#
# Intended usage:
#   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh
#   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh -s -- uninstall
#
# The bootstrap installs only the latest stable GitHub release.  It never
# silently falls back to the mutable main branch.  The downloaded installer
# reads its questions from /dev/tty, so this also works when the bootstrap
# itself is supplied through a pipe.

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
export PATH
LC_ALL=C
export LC_ALL

die() {
    echo "rsshub-cookie-sync bootstrap: $*" >&2
    exit 1
}

[ "$(id -u)" -eq 0 ] || die "请使用 root 运行，例如：curl ... | sudo sh"
command -v curl >/dev/null 2>&1 || die "缺少 curl"
command -v tar >/dev/null 2>&1 || die "缺少 tar"
command -v find >/dev/null 2>&1 || die "缺少 find"
command -v mktemp >/dev/null 2>&1 || die "缺少 mktemp"
command -v sed >/dev/null 2>&1 || die "缺少 sed"
command -v head >/dev/null 2>&1 || die "缺少 head"
command -v awk >/dev/null 2>&1 || die "缺少 awk"

REPOSITORY=https://github.com/Jaaayden/rsshub-cookie-sync
API_URL=https://api.github.com/repos/Jaaayden/rsshub-cookie-sync/releases/latest
# server/install.sh deliberately rejects source trees below a world-writable
# parent.  Keep the downloaded code below root's private home so that the
# one-command path passes the same source-integrity check as a manual install.
TEMP_DIR=$(mktemp -d /root/.rsshub-cookie-sync-install.XXXXXX)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

ARCHIVE_URL=
LATEST_JSON=
if LATEST_JSON=$(curl -fsSL --proto '=https' --tlsv1.2 \
    -H 'Accept: application/vnd.github+json' "$API_URL" 2>/dev/null); then
    # The tag is constrained before it is interpolated into a URL.  Do not
    # trust arbitrary JSON fields as shell code or as a filesystem path.
    RELEASE_TAG=$(printf '%s\n' "$LATEST_JSON" | sed -n \
        's/^[[:space:]]*"tag_name"[[:space:]]*:[[:space:]]*"\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)"[[:space:]]*,\{0,1\}[[:space:]]*$/\1/p' | head -n 1)
    case "$RELEASE_TAG" in
        v[0-9]*) ARCHIVE_URL="$REPOSITORY/archive/refs/tags/$RELEASE_TAG.tar.gz" ;;
    esac
fi

[ -n "$ARCHIVE_URL" ] || die "无法确定最新稳定版本；为安全起见不会自动改用 main，请稍后重试"

ARCHIVE_FILE=$TEMP_DIR/source.tar.gz
echo "正在下载 RSSHub Cookie Sync 安装程序。" >&2
curl -fsSL --proto '=https' --tlsv1.2 "$ARCHIVE_URL" -o "$ARCHIVE_FILE" \
    || die "源码下载失败"

# Refuse absolute paths, parent traversal, and links before extracting an
# archive as root.  GitHub source archives contain only one top-level folder,
# regular files, and directories; anything else is unexpected here.
if ! tar -tzf "$ARCHIVE_FILE" | awk '
    BEGIN { ok = 1 }
    /^\// || /(^|\/)\.\.?(\/|$)/ || /\/\// { ok = 0 }
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
tar -xzf "$ARCHIVE_FILE" --no-same-owner --no-same-permissions -C "$TEMP_DIR" \
    || die "源码解压失败"

INSTALLER=$(find "$TEMP_DIR" -type f -path '*/server/install.sh' -print -quit)
[ -n "$INSTALLER" ] || die "下载内容缺少 server/install.sh"
chmod 0755 "$INSTALLER"
if [ "${1:-}" = uninstall ]; then
    shift
    UNINSTALLER=$(find "$TEMP_DIR" -type f -path '*/server/uninstall.sh' -print -quit)
    [ -n "$UNINSTALLER" ] || die "下载内容缺少 server/uninstall.sh"
    chmod 0755 "$UNINSTALLER"
    sh "$UNINSTALLER" "$@"
else
    [ "$#" -eq 0 ] || die "未知参数：$1（卸载请使用 uninstall）"
    sh "$INSTALLER"
fi
