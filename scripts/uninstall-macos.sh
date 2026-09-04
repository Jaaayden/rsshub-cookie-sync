#!/bin/sh
set -eu

# Release convenience entry point.  The installer always writes a
# version-matched, self-contained uninstaller to the fixed path below; this
# wrapper validates that target and invokes it without touching any other Edge
# profile or SSH file.

PATH=/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH
LC_ALL=C
export LC_ALL

die() {
    echo "rsshub-cookie-sync macOS uninstall: $*" >&2
    exit 1
}

[ "$(id -u)" -ne 0 ] || die "请使用安装 Native Host 的普通 macOS 用户运行；不要使用 root"
[ "$(uname -s)" = Darwin ] || die "此卸载器仅支持 macOS (Darwin)"

INSTALLED_UNINSTALLER="$HOME/Library/Application Support/rsshub-cookie-sync/uninstall.sh"
[ -e "$INSTALLED_UNINSTALLER" ] || die "没有找到已安装的卸载器；如需清理旧版，请使用 install-macos.sh 的 uninstall 模式"
[ -f "$INSTALLED_UNINSTALLER" ] || die "已安装的卸载器不是普通文件"
[ ! -L "$INSTALLED_UNINSTALLER" ] || die "已安装的卸载器是符号链接"
[ -x "$INSTALLED_UNINSTALLER" ] || die "已安装的卸载器不可执行"

# curl | sh leaves stdin attached to the pipeline.  Use /dev/tty when it is
# available so the user still receives the safe key-retention prompt.
if [ -r /dev/tty ]; then
    "$INSTALLED_UNINSTALLER" "$@" < /dev/tty
else
    "$INSTALLED_UNINSTALLER" "$@"
fi
