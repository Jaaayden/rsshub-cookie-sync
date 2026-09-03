"""Static checks for the server installation assets.

These checks intentionally inspect the shipped files instead of invoking the
installer.  Running an installer would require a real root/systemd/Docker
environment and could mutate the host; syntax and source-level invariants are
the useful, deterministic checks for these assets.
"""

import os
import re
import stat
import subprocess
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1]
PROJECT_DIR = SERVER_DIR.parent
INSTALL_SCRIPT = SERVER_DIR / "install.sh"
BOOTSTRAP_SCRIPT = PROJECT_DIR / "scripts" / "install-server.sh"
UNINSTALL_SCRIPT = SERVER_DIR / "uninstall.sh"
WRAPPER_SCRIPTS = (
    SERVER_DIR / "rsshub-cookie-sync",
    SERVER_DIR / "rsshub-cookie-sync-apply",
    SERVER_DIR / "provision-ssh-key.sh",
)
CONFIG_WRAPPERS = WRAPPER_SCRIPTS[:2]
SHELL_ASSETS = (INSTALL_SCRIPT, BOOTSTRAP_SCRIPT, UNINSTALL_SCRIPT, *WRAPPER_SCRIPTS)
SERVICE_UNIT = SERVER_DIR / "rsshub-cookie-sync-monitor.service"
MIGRATION_PENDING_PARSER = (
    'import json,sys; value=json.load(sys.stdin); '
    'pending=value.get("migration_pending"); '
    'type(pending) is bool or sys.exit(2); '
    'print("true" if pending else "false")'
)


class InstallAssetTests(unittest.TestCase):
    @staticmethod
    def _source(path: Path) -> str:
        return path.read_text(encoding="utf-8")

    def test_shell_assets_have_valid_sh_syntax(self):
        """Every installer, uninstaller, and shell wrapper parses as POSIX sh."""
        for path in SHELL_ASSETS:
            with self.subTest(asset=path.name):
                completed = subprocess.run(
                    ["sh", "-n", str(path)],
                    cwd=SERVER_DIR,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    completed.returncode,
                    0,
                    msg=f"{path.name} failed sh -n:\n{completed.stderr}",
                )

    def test_shell_assets_are_executable(self):
        """The files copied or used as command entry points are executable."""
        for path in SHELL_ASSETS:
            with self.subTest(asset=path.name):
                mode = path.stat().st_mode
                self.assertTrue(
                    mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH),
                    f"{path.name} must have an executable bit",
                )
                self.assertTrue(os.access(path, os.X_OK), f"{path.name} is not executable")

    def test_install_exposes_explicit_deployment_options(self):
        """The target deployment is selected through the install CLI."""
        source = self._source(INSTALL_SCRIPT)
        options = (
            "--compose-file",
            "--project-name",
            "--service-name",
            "--rsshub-base-url",
            "--replace-deployment",
        )
        for option in options:
            with self.subTest(option=option):
                # Keep both the user-facing contract and the parser branch
                # covered; a help-only option is not enough here.
                self.assertIn(option, source)
                self.assertRegex(
                    source,
                    rf"(?m)^\s*{re.escape(option)}(?:\s|$)",
                    msg=f"{option} is missing from install.sh usage",
                )
                self.assertRegex(
                    source,
                    rf"(?m)^\s*{re.escape(option)}\)",
                    msg=f"{option} is missing from install.sh argument parsing",
                )

    def test_install_has_no_machine_specific_deployment_defaults(self):
        """The installer must not point at a private RSSHub deployment."""
        source = self._source(INSTALL_SCRIPT)
        # /root/rsshub is a documented discovery location, not a deployment
        # identity.  The installer must still not embed any operator-specific
        # server address or RSSHub URL.
        self.assertIn("/root/rsshub/docker-compose.yml", source)
        ipv4_literals = re.findall(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9.])", source)
        self.assertEqual(set(ipv4_literals), {"127.0.0.1"})

        # A loopback default is intentionally safe and is the only URL literal
        # allowed in this local-only installer.  Any public/private host or
        # deployment hostname must arrive through --rsshub-base-url.
        # Match URL-shaped literals only; ``http://*)`` in a shell case
        # pattern is syntax, not an embedded address.
        url_literals = re.findall(
            r"https?://(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?",
            source,
        )
        for literal in url_literals:
            self.assertRegex(
                literal,
                r"^http://(?:127\.0\.0\.1|localhost|\[?::1\]?)(?::[0-9]{1,5})?$",
                msg=f"machine-specific URL embedded in install.sh: {literal}",
            )

    def test_install_has_simple_interactive_discovery_defaults(self):
        """No-argument installs discover common files and use safe defaults."""
        source = self._source(INSTALL_SCRIPT)
        for location in (
            '"$PWD/docker-compose.yml"',
            '"$PWD/compose.yml"',
            '"/opt/rsshub/docker-compose.yml"',
            '"/root/rsshub/docker-compose.yml"',
        ):
            with self.subTest(location=location):
                expected = location if location.startswith('"$PWD') else location[1:-1]
                self.assertIn(expected, source)
        self.assertIn("ORIGINAL_ARGC=$#", source)
        self.assertIn('INTERACTIVE=1', source)
        self.assertIn('< /dev/tty', source)
        self.assertIn('PROJECT=rsshub', source)
        self.assertIn('SERVICE=rsshub', source)
        self.assertIn('RSSHUB_BASE_URL=http://127.0.0.1:1200', source)
        self.assertNotIn('die "--compose-file is required"', source)

    def test_server_bootstrap_downloads_source_and_keeps_prompts_on_tty(self):
        """The curl|sudo entry point must fetch code and preserve interaction."""
        source = self._source(BOOTSTRAP_SCRIPT)
        self.assertIn("api.github.com/repos/Jaaayden/rsshub-cookie-sync/releases/latest", source)
        self.assertNotIn("archive/refs/heads/main.tar.gz", source)
        self.assertIn("mktemp -d /root/.rsshub-cookie-sync-install.", source)
        self.assertNotIn("mktemp -d /tmp/", source)
        self.assertIn("源码归档包含不安全路径", source)
        self.assertIn("/dev/tty", self._source(INSTALL_SCRIPT))
        self.assertIn("server/install.sh", source)

    def test_interactive_install_resolves_the_real_compose_project(self):
        """The default path detects projects and preserves saved custom values."""
        source = self._source(INSTALL_SCRIPT)
        self.assertIn("PROJECT_EXPLICIT=0", source)
        self.assertIn('config --format json', source)
        self.assertIn('value=json.load(sys.stdin).get("name")', source)
        self.assertIn('PROJECT_EXPLICIT=1', source)
        self.assertIn('saved_deployment=', source)
        self.assertIn('PROJECT=$saved_project', source)
        self.assertIn('SERVICE=$saved_service', source)
        self.assertIn('RSSHUB_BASE_URL=$saved_base_url', source)

    def test_installer_provides_a_stable_uninstall_command(self):
        source = self._source(INSTALL_SCRIPT)
        uninstall = self._source(UNINSTALL_SCRIPT)
        fixed_path = "/usr/local/sbin/rsshub-cookie-sync-uninstall"
        self.assertIn('"$SBIN_DIR/rsshub-cookie-sync-uninstall"', source)
        self.assertIn(fixed_path, uninstall)

    def test_units_and_wrappers_pass_explicit_config(self):
        """Runtime entry points do not depend on an ambient config path."""
        assets = (SERVICE_UNIT, *CONFIG_WRAPPERS)
        for path in assets:
            with self.subTest(asset=path.name):
                source = self._source(path)
                self.assertRegex(source, r"(?:^|[\s\\])--config(?:[\s\\]|$)")
                self.assertIn("--config /etc/rsshub-cookie-sync/config.json", source)

    def test_base_service_unit_has_no_fixed_read_write_paths(self):
        """Writable deployment paths belong in the generated drop-in."""
        source = self._source(SERVICE_UNIT)
        self.assertNotRegex(source, r"(?mi)^\s*ReadWritePaths\s*=")
        self.assertNotIn("ReadWritePaths=", source)

    def test_install_bootstraps_only_when_migration_is_pending(self):
        """Reinstalling an already migrated deployment must not recreate RSSHub."""
        source = self._source(INSTALL_SCRIPT)
        bootstrap_calls = list(re.finditer(r"\bbootstrap\s+--json\b", source))
        self.assertEqual(
            len(bootstrap_calls),
            1,
            "install.sh should have exactly one bootstrap invocation",
        )

        migration_guard = re.search(
            r"(?ms)^\s*if\s+\[\s+\"\$migration_pending\"\s*=\s*true\s+\];\s*then\n(?P<body>.*?)^\s*fi\s*$",
            source,
        )
        self.assertIsNotNone(
            migration_guard,
            "bootstrap must be guarded by migration_pending=true",
        )
        assert migration_guard is not None
        self.assertIn("bootstrap --json", migration_guard.group("body"))
        self.assertLess(
            source.find("migration_pending=$("),
            migration_guard.start(),
            "migration_pending must be computed before bootstrap",
        )

    def test_migration_pending_parser_handles_reinstall_result(self):
        """The shell parser must accept both pending and already-migrated JSON."""
        source = self._source(INSTALL_SCRIPT)
        self.assertIn(MIGRATION_PENDING_PARSER, source)

        cases = (
            ('{"migration_pending":true}\n', 0, "true\n"),
            ('{"migration_pending":false}\n', 0, "false\n"),
            ('{}\n', 2, ""),
            ('{"migration_pending":"false"}\n', 2, ""),
        )
        for payload, expected_code, expected_output in cases:
            with self.subTest(payload=payload):
                completed = subprocess.run(
                    ["python3", "-c", MIGRATION_PENDING_PARSER],
                    input=payload,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(completed.returncode, expected_code)
                self.assertEqual(completed.stdout, expected_output)

    def test_uninstall_preserves_state_and_compose_secrets(self):
        """Uninstall removes the synchronizer but leaves rollback/runtime data."""
        source = self._source(UNINSTALL_SCRIPT)

        # The uninstall path is intentionally independent of the selected
        # Compose directory, so it must not introduce deletion targets for
        # either the persistent state directory or Compose-managed secrets.
        self.assertNotIn("/var/lib/rsshub-cookie-sync", source)
        self.assertNotRegex(source, r"(?i)\b(?:STATE_DIR|SECRETS_DIR|LIVE_ENV|CANDIDATE_DIR)\b")
        self.assertRegex(source, r"(?i)state.*(?:retain|left untouched|untouched)")
        self.assertRegex(source, r"(?i)(?:secret|secrets).*?(?:retain|left untouched|untouched)")

        deletion_lines = []
        for line in source.splitlines():
            stripped = line.lstrip()
            if stripped.startswith("#"):
                continue
            if re.search(r"\b(?:rm|rmdir)\b", stripped):
                deletion_lines.append(stripped)
        self.assertTrue(deletion_lines, "uninstall.sh should have explicit managed cleanup")
        for line in deletion_lines:
            with self.subTest(deletion=line):
                self.assertNotRegex(
                    line,
                    r"(?i)(?:rsshub\.env|/secrets(?:/|\b)|candidates|/var/lib/rsshub-cookie-sync)",
                )


if __name__ == "__main__":
    unittest.main()
