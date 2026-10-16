#!/bin/sh
set -eu

# RSSHub Cookie Sync server bootstrapper.
#
# Intended usage:
#   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh
#   curl -fsSL https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download/install-server.sh | sh -s -- uninstall
#
# The bootstrap installs only the latest stable GitHub release.  It downloads
# the release's source tarball together with SHA256SUMS and refuses to extract
# anything whose checksum does not match.  It never silently falls back to the
# mutable main branch.  The downloaded installer
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
command -v awk >/dev/null 2>&1 || die "缺少 awk"
command -v sha256sum >/dev/null 2>&1 || die "缺少 sha256sum"

# Both files are fetched from the same "latest" release.  If a new release is
# published between the two requests the checksum simply fails to match and
# nothing is installed.  Using the redirecting download URL also avoids the
# unauthenticated GitHub API, which is rate limited per IP address.
RELEASE_URL=https://github.com/Jaaayden/rsshub-cookie-sync/releases/latest/download
SOURCE_NAME=rsshub-cookie-sync-source.tar.gz
# server/install.sh deliberately rejects source trees below a world-writable
# parent.  Keep the downloaded code below root's private home so that the
# one-command path passes the same source-integrity check as a manual install.
TEMP_DIR=$(mktemp -d /root/.rsshub-cookie-sync-install.XXXXXX)
trap 'rm -rf "$TEMP_DIR"' EXIT HUP INT TERM

ARCHIVE_FILE=$TEMP_DIR/$SOURCE_NAME
echo "正在下载 RSSHub Cookie Sync 安装程序。" >&2
curl -fsSL --proto '=https' --tlsv1.2 "$RELEASE_URL/$SOURCE_NAME" -o "$ARCHIVE_FILE" \
    || die "源码下载失败；请确认最新 Release 已发布"
curl -fsSL --proto '=https' --tlsv1.2 "$RELEASE_URL/SHA256SUMS" -o "$TEMP_DIR/SHA256SUMS" \
    || die "校验文件下载失败"

# Verify before the archive is even listed, let alone extracted as root.
EXPECTED_SHA=$(awk -v name="$SOURCE_NAME" '$2 == name { print $1; exit }' "$TEMP_DIR/SHA256SUMS")
case "$EXPECTED_SHA" in
    *[!0-9a-f]*|"") die "SHA256SUMS 中没有有效的源码校验值" ;;
esac
[ "${#EXPECTED_SHA}" -eq 64 ] || die "SHA256SUMS 中没有有效的源码校验值"
ACTUAL_SHA=$(sha256sum "$ARCHIVE_FILE" | awk '{ print $1 }')
[ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || die "源码归档校验失败，已停止安装"

# Refuse absolute paths, parent traversal, and links before extracting an
# archive as root.  The release source tarball contains only one top-level folder,
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
