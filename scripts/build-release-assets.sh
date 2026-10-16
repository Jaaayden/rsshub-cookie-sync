#!/usr/bin/env bash

# Build and verify the complete set of Release assets in one directory.
# Shared by the CI and Release workflows so both always check the same thing.
#
# Usage: scripts/build-release-assets.sh [output-dir]   (default: dist/release-assets)

set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PROJECT_ROOT=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)
RELEASE_DIR=${1:-"$PROJECT_ROOT/dist/release-assets"}

EXTENSION_ZIP=rsshub-cookie-sync-extension.zip
SOURCE_TARBALL=rsshub-cookie-sync-source.tar.gz

if command -v sha256sum >/dev/null 2>&1; then
    sha256_check() { sha256sum -c "$@"; }
    sha256_sum() { sha256sum "$@"; }
else
    sha256_check() { shasum -a 256 -c "$@"; }
    sha256_sum() { shasum -a 256 "$@"; }
fi

rm -rf "$RELEASE_DIR"
mkdir -p "$RELEASE_DIR"
RELEASE_DIR=$(CDPATH= cd -- "$RELEASE_DIR" && pwd)

"$SCRIPT_DIR/package-extension.sh" "$RELEASE_DIR/$EXTENSION_ZIP"

# The installers verify this tarball against SHA256SUMS before extracting it
# as root, so it must be exactly the tagged tree.  `gzip -n` keeps the output
# free of timestamps.
git -C "$PROJECT_ROOT" archive --format=tar --prefix=rsshub-cookie-sync-source/ HEAD \
    | gzip -n -9 > "$RELEASE_DIR/$SOURCE_TARBALL"

cp "$SCRIPT_DIR/install-server.sh" "$RELEASE_DIR/install-server.sh"
cp "$SCRIPT_DIR/install-macos.sh" "$RELEASE_DIR/install-macos.sh"
cp "$PROJECT_ROOT/server/uninstall.sh" "$RELEASE_DIR/uninstall-server.sh"
cp "$SCRIPT_DIR/uninstall-macos.sh" "$RELEASE_DIR/uninstall-macos.sh"
chmod 0755 \
    "$RELEASE_DIR/install-server.sh" \
    "$RELEASE_DIR/install-macos.sh" \
    "$RELEASE_DIR/uninstall-server.sh" \
    "$RELEASE_DIR/uninstall-macos.sh"

(
    cd "$RELEASE_DIR"
    sha256_sum \
        "$EXTENSION_ZIP" \
        "$SOURCE_TARBALL" \
        install-server.sh \
        install-macos.sh \
        uninstall-server.sh \
        uninstall-macos.sh > SHA256SUMS
    sha256_check SHA256SUMS
)

python3 - "$RELEASE_DIR" <<'PY'
import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path

release_dir = Path(sys.argv[1])
expected_assets = {
    "rsshub-cookie-sync-extension.zip",
    "rsshub-cookie-sync-source.tar.gz",
    "install-server.sh",
    "install-macos.sh",
    "uninstall-server.sh",
    "uninstall-macos.sh",
    "SHA256SUMS",
}
actual_assets = {path.name for path in release_dir.iterdir() if path.is_file()}
if actual_assets != expected_assets:
    raise SystemExit(f"unexpected release assets: {sorted(actual_assets)}")

manifest = json.loads(Path("extension/manifest.json").read_text(encoding="utf-8"))
if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", str(manifest.get("version", ""))):
    raise SystemExit(f"invalid extension version: {manifest.get('version')!r}")

expected_extension = {
    "background.js",
    "lib/cookies.js",
    "lib/native-config.js",
    "lib/popup-actions.js",
    "lib/protocol.js",
    "lib/state.js",
    "manifest.json",
    "options.css",
    "options.html",
    "options.js",
    "popup.css",
    "popup.html",
    "popup.js",
}
with zipfile.ZipFile(release_dir / "rsshub-cookie-sync-extension.zip") as archive:
    actual_extension = set(archive.namelist())
if actual_extension != expected_extension:
    raise SystemExit(f"unexpected extension archive entries: {sorted(actual_extension)}")

# Both installers locate their entry point with `find ... -path '*/x'`, so the
# tarball must contain them under a single top-level directory.
with tarfile.open(release_dir / "rsshub-cookie-sync-source.tar.gz") as archive:
    names = archive.getnames()
for required in ("server/install.sh", "server/uninstall.sh", "native-host/install.py", "native-host/uninstall.py"):
    if f"rsshub-cookie-sync-source/{required}" not in names:
        raise SystemExit(f"source tarball is missing {required}")
if any(not name.startswith("rsshub-cookie-sync-source") for name in names):
    raise SystemExit("source tarball has entries outside its top-level directory")
PY
