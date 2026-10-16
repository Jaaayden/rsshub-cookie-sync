#!/usr/bin/env python3
"""Repository hygiene checks shared by the CI and Release workflows.

* Release entrypoints must be regular, executable, syntactically valid scripts.
* Every tracked text file is scanned (recursively) so deployment-specific
  addresses and credentials cannot slip into public source.

Test fixtures and documentation legitimately contain synthetic credentials, so
the stricter "assignment" and "IPv4" rules apply to runtime sources only; the
private-key and Bark-key rules apply everywhere (except test fixtures for the
private-key rule).
"""

import ipaddress
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

RELEASE_SCRIPTS = (
    "scripts/install-server.sh",
    "scripts/install-macos.sh",
    "scripts/uninstall-macos.sh",
    "server/uninstall.sh",
)

IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])")
PRIVATE_KEY = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")
BARK_KEY_URL = re.compile(r"https?://api\.day\.app/[A-Za-z0-9._~-]{16,}")
SECRET_ASSIGNMENT = re.compile(
    r"^\s*(?:export\s+)?(?:ZHIHU_COOKIES|WEIBO_COOKIES|TWITTER_AUTH_TOKEN|"
    r"BARK_DEVICE_KEY|BARK_KEY|DEVICE_KEY)"
    r"\s*=\s*(?P<value>.+?)\s*$"
)
ALLOWED_IPS = {"127.0.0.1", "0.0.0.0"}
# RFC 5737 documentation ranges are never a real machine.
DOC_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
SKIP_NAMES = {"LICENSE"}
RUNTIME_ROOTS = ("extension", "native-host", "server", "scripts")


def tracked_files():
    output = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, stdout=subprocess.PIPE
    ).stdout
    return [ROOT / item.decode() for item in output.split(b"\0") if item]


def is_test_file(path):
    parts = path.relative_to(ROOT).parts
    return path.name.startswith("test_") or "test" in parts or "tests" in parts


def is_runtime_source(path):
    parts = path.relative_to(ROOT).parts
    return parts[0] in RUNTIME_ROOTS and not is_test_file(path)


def check_entrypoints(errors):
    for relative in RELEASE_SCRIPTS:
        path = ROOT / relative
        if path.is_symlink() or not path.is_file():
            errors.append(f"{relative}: release entrypoint must be a regular file")
            continue
        if not path.stat().st_mode & 0o111:
            errors.append(f"{relative}: release entrypoint must be executable")
        if subprocess.run(["sh", "-n", str(path)]).returncode != 0:
            errors.append(f"{relative}: sh -n failed")


def is_documentation_ip(address):
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return True  # e.g. 999.1.1.1 or a version string: not an address
    return any(parsed in network for network in DOC_NETWORKS)


def scan(path, errors):
    relative = path.relative_to(ROOT)
    if path.name in SKIP_NAMES or path.is_symlink() or not path.is_file():
        return
    try:
        source = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return  # binary file
    if PRIVATE_KEY.search(source) and not is_test_file(path):
        errors.append(f"{relative}: private key material is embedded")
    if BARK_KEY_URL.search(source):
        errors.append(f"{relative}: Bark device key is embedded in a URL")
    if not is_runtime_source(path):
        return
    for address in IPV4.findall(source):
        if address not in ALLOWED_IPS and not is_documentation_ip(address):
            errors.append(f"{relative}: machine-specific IPv4 literal {address}")
    for number, line in enumerate(source.splitlines(), 1):
        match = SECRET_ASSIGNMENT.match(line)
        if not match:
            continue
        value = match.group("value").strip()
        # Parameter expansion and an explicitly empty value are safe; any
        # other literal would be a credential in source.
        if value not in {'""', "''"} and not value.startswith(("$", '"$', "'$")):
            errors.append(f"{relative}:{number}: literal secret assignment")


def main():
    errors = []
    check_entrypoints(errors)
    for path in tracked_files():
        scan(path, errors)
    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
