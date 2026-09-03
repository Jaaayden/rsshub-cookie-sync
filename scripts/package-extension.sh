#!/usr/bin/env bash

# Build a distributable MV3 extension archive.
#
# The archive is intentionally assembled from an explicit runtime allow-list.
# Documentation, tests, npm metadata, credentials accidentally placed in the
# source directory, and local build artefacts therefore cannot be included by
# an overly broad `zip -r` invocation.  The archive root is the extension root
# (manifest.json is at the top level), as required by Edge and Chrome's
# "load unpacked/extension" workflows.

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
EXTENSION_DIR="$PROJECT_ROOT/extension"

if [[ $# -gt 1 ]]; then
    printf '用法: %s [output.zip]\n' "$0" >&2
    exit 2
fi

if [[ $# -eq 1 ]]; then
    OUTPUT=$1
    if [[ "$OUTPUT" != /* ]]; then
        OUTPUT="$PROJECT_ROOT/$OUTPUT"
    fi
else
    OUTPUT="$PROJECT_ROOT/dist/rsshub-cookie-sync-extension.zip"
fi

if [[ ! -d "$EXTENSION_DIR" ]]; then
    printf '错误：找不到扩展目录: %s\n' "$EXTENSION_DIR" >&2
    exit 1
fi
if [[ ! -f "$EXTENSION_DIR/manifest.json" ]]; then
    printf '错误：扩展缺少 manifest.json\n' >&2
    exit 1
fi
if ! command -v zip >/dev/null 2>&1; then
    printf '错误：未找到 zip 命令，请先安装 zip。\n' >&2
    exit 1
fi

mkdir -p "$(dirname "$OUTPUT")"
if [[ -e "$OUTPUT" && ! -f "$OUTPUT" ]]; then
    printf '错误：输出路径不是普通文件: %s\n' "$OUTPUT" >&2
    exit 1
fi
rm -f "$OUTPUT"

STAGE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/rsshub-cookie-sync-extension.XXXXXX")
cleanup() {
    rm -rf "$STAGE_DIR"
}
trap cleanup EXIT HUP INT TERM

# Keep this list deliberately small.  Adding a new runtime asset requires an
# explicit review here, which is also a guard against packaging secrets.
RUNTIME_FILES=(
    manifest.json
    background.js
    popup.html
    popup.js
    popup.css
    lib/cookies.js
    lib/native-config.js
    lib/popup-actions.js
    lib/protocol.js
    lib/state.js
    options.html
    options.js
    options.css
)

for relative in "${RUNTIME_FILES[@]}"; do
    if [[ ! -f "$EXTENSION_DIR/$relative" || -L "$EXTENSION_DIR/$relative" ]]; then
        printf '错误：扩展运行文件缺失或不安全: %s\n' "$relative" >&2
        exit 1
    fi
    mkdir -p "$STAGE_DIR/$(dirname "$relative")"
    cp -p "$EXTENSION_DIR/$relative" "$STAGE_DIR/$relative"
done

if [[ ! -f "$STAGE_DIR/manifest.json" ]]; then
    printf '错误：筛选后没有保留根目录 manifest.json\n' >&2
    exit 1
fi

(
    cd "$STAGE_DIR"
    # Add files in a deterministic order. -X omits host-specific extra
    # attributes; timestamps still reflect the checked-out source files.
    zip -q -X "$OUTPUT" manifest.json
    for relative in "${RUNTIME_FILES[@]}"; do
        [[ "$relative" == "manifest.json" ]] && continue
        zip -q -X "$OUTPUT" "$relative"
    done
)

printf '已生成扩展包: %s\n' "$OUTPUT"
