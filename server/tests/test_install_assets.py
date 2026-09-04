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
import tempfile
import textwrap
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
PROVISION_SCRIPT = SERVER_DIR / "provision-ssh-key.sh"


def _write_executable(path: Path, source: str) -> None:
    """Write a tiny test-only command shim without touching the host system."""
    path.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
    path.chmod(0o700)


def _fake_command_bin(root: Path) -> Path:
    """Return a PATH prefix that makes root-only shell assets testable on macOS.

    The shipped server scripts intentionally use Linux paths and require root.
    These shims model only the OS boundary needed by the tests: account lookup,
    ownership reporting, and filesystem installation.  The scripts' own
    validation, input parser, awk filter, and cleanup code still execute.
    """
    command_bin = root / "bin"
    command_bin.mkdir()

    _write_executable(
        command_bin / "id",
        r'''
        # TEST_ACCOUNT=1 exposes a synthetic pre-existing rsshub-sync user.
        case "${1:-}" in
            -u)
                if [ "$#" -eq 1 ]; then
                    echo 0
                    exit 0
                fi
                [ "${2:-}" = rsshub-sync ] && [ "${TEST_ACCOUNT:-0}" = 1 ] && { echo 12345; exit 0; }
                exit 1
                ;;
            -g)
                [ "${2:-}" = rsshub-sync ] && [ "${TEST_ACCOUNT:-0}" = 1 ] && { echo 12345; exit 0; }
                exit 1
                ;;
            -gn|-Gn)
                [ "${2:-}" = rsshub-sync ] && [ "${TEST_ACCOUNT:-0}" = 1 ] && { echo rsshub-sync; exit 0; }
                exit 1
                ;;
            rsshub-sync)
                [ "${TEST_ACCOUNT:-0}" = 1 ]
                exit
                ;;
            *)
                exit 1
                ;;
        esac
        ''',
    )
    _write_executable(
        command_bin / "getent",
        r'''
        if [ "${1:-}" = passwd ] && [ "${2:-}" = rsshub-sync ] && [ "${TEST_ACCOUNT:-0}" = 1 ]; then
            printf 'rsshub-sync:x:12345:12345::%s:/bin/sh\n' "$TEST_SSH_HOME"
            exit 0
        fi
        if [ "${1:-}" = shadow ] && [ "${2:-}" = rsshub-sync ] && [ "${TEST_ACCOUNT:-0}" = 1 ]; then
            printf 'rsshub-sync:!:19000:0:99999:7:::\n'
            exit 0
        fi
        exit 2
        ''',
    )
    _write_executable(
        command_bin / "stat",
        r'''
        # GNU stat is used by the server scripts; the test host is macOS.
        [ "${1:-}" = -c ] || exit 1
        format=${2:-}
        shift 2
        [ "${1:-}" = -- ] && shift
        target=${1:-}
        case "$format" in
            %u) echo 0 ;;
            %U)
                case "$target" in
                    */authorized_keys|*/.ssh|*/rsshub-sync) echo rsshub-sync ;;
                    *) echo root ;;
                esac
                ;;
            %G) echo rsshub-sync ;;
            %A) /usr/bin/stat -f '%Sp' "$target" ;;
            %a) /usr/bin/stat -f '%Lp' "$target" ;;
            *) exit 1 ;;
        esac
        ''',
    )
    _write_executable(
        command_bin / "install",
        r'''
        # Drop Linux-only owner/group flags; the harness reports root ownership
        # through stat and keeps real mode/directory semantics from install(1).
        /usr/bin/python3 - "$@" <<'PY'
        import os
        import shutil
        import sys

        args = sys.argv[1:]
        directory = False
        mode = None
        cleaned = []
        index = 0
        while index < len(args):
            value = args[index]
            if value == "-d":
                directory = True
            elif value in ("-o", "-g"):
                index += 1
            elif value == "-m":
                index += 1
                mode = args[index]
            else:
                cleaned.append(value)
            index += 1
        if not cleaned:
            raise SystemExit("install shim: missing target")
        target = cleaned[-1]
        if directory:
            os.makedirs(target, exist_ok=True)
            if mode:
                os.chmod(target, int(mode, 8))
        else:
            source = cleaned[-2] if len(cleaned) >= 2 else None
            if source == "/dev/null":
                open(target, "wb").close()
            elif source:
                shutil.copy2(source, target)
            else:
                open(target, "wb").close()
            if mode:
                os.chmod(target, int(mode, 8))
        PY
        ''',
    )
    _write_executable(command_bin / "chown", "#!/bin/sh\nexit 0\n")
    _write_executable(command_bin / "usermod", "#!/bin/sh\nexit 0\n")
    _write_executable(
        command_bin / "userdel",
        r'''
        [ -n "${TEST_USERDEL_SENTINEL:-}" ] && : > "$TEST_USERDEL_SENTINEL"
        exit 0
        ''',
    )
    return command_bin


def _rewrite_server_script(source: str, root: Path, command_bin: Path) -> str:
    """Relocate an implementation script into a disposable fake server root."""
    root_text = str(root)
    # Do the path substitutions before restoring the real command PATH.  This
    # keeps project targets under the fake root while system utilities remain
    # available to the harness.
    for old, new in (
        ("/usr/local", root_text + "/usr/local"),
        ("/etc", root_text + "/etc"),
        ("/var/lib", root_text + "/var/lib"),
        ("/run", root_text + "/run"),
    ):
        source = source.replace(old, new)
    # The awk/sed expressions escape path separators, so the plain string
    # substitution above intentionally does not reach them.  Relocate those
    # expressions too; otherwise the harness would test a different key path
    # from the one written by the script.
    source = source.replace(
        r"\/usr\/local",
        root_text.replace("/", r"\/") + r"\/usr\/local",
    )
    source = source.replace(
        '[ "$chain_path" = / ] && break',
        '[ "$chain_path" = "' + root_text + '" ] && break',
    )
    source = source.replace(
        "PATH="
        + root_text
        + "/usr/local/sbin:"
        + root_text
        + "/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "PATH=" + str(command_bin) + ":/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    )
    source = source.replace(
        "PATH=/usr/sbin:/usr/bin:/sbin:/bin",
        "PATH=" + str(command_bin) + ":/usr/sbin:/usr/bin:/sbin:/bin",
    )
    return source


def _run_script(source: str, root: Path, command_bin: Path, args=(), input_data=b"", env=None):
    script = root / "script.sh"
    script.write_text(source, encoding="utf-8")
    script.chmod(0o700)
    merged_env = os.environ.copy()
    merged_env.update(
        {
            "TEST_SSH_HOME": str(root / "var" / "lib" / "rsshub-sync"),
        }
    )
    if env:
        merged_env.update(env)
    return subprocess.run(
        ["sh", str(script), *args],
        input=input_data,
        capture_output=True,
        check=False,
        env=merged_env,
    )


def _server_temporary_directory():
    """Use a disposable root whose parent is not a system symlink.

    The production uninstaller deliberately refuses to traverse symlinked or
    world-writable parent directories.  On macOS ``/var/folders`` and ``/tmp``
    commonly violate that policy, so the harness stops its synthetic chain at
    the temporary root instead (see ``_rewrite_server_script``).
    """
    base = Path("/private/tmp") if Path("/private/tmp").is_dir() else Path("/tmp")
    return tempfile.TemporaryDirectory(dir=str(base))


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

    def test_provision_rejects_oversize_nul_and_multiline_input_without_root(self):
        """The real provision parser rejects malformed stdin before file changes."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_bin = _fake_command_bin(root)
            home = root / "var" / "lib" / "rsshub-sync"
            (home / ".ssh").mkdir(parents=True)
            source = _rewrite_server_script(
                self._source(PROVISION_SCRIPT), root, command_bin
            )
            key_file = root / "client-key"
            generated = subprocess.run(
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_file)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode())
            public_key = key_file.with_name(key_file.name + ".pub").read_bytes().strip()

            malformed = (
                b"",
                b"x" * 8193,
                public_key + b"\x00\n",
                public_key + b"\n" + public_key + b"\n",
            )
            for payload in malformed:
                with self.subTest(size=len(payload), nul=b"\x00" in payload):
                    result = _run_script(
                        source,
                        root,
                        command_bin,
                        input_data=payload,
                        env={"TEST_ACCOUNT": "1"},
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(public_key, result.stdout)

    def test_provision_replaces_only_project_key_and_preserves_other_authorized_keys(self):
        """Provisioning an existing account leaves unrelated SSH keys intact."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_bin = _fake_command_bin(root)
            home = root / "var" / "lib" / "rsshub-sync"
            ssh_dir = home / ".ssh"
            ssh_dir.mkdir(parents=True)
            ssh_dir.chmod(0o700)
            source = _rewrite_server_script(
                self._source(PROVISION_SCRIPT), root, command_bin
            )
            key_file = root / "client-key"
            generated = subprocess.run(
                ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key_file)],
                capture_output=True,
                check=False,
            )
            self.assertEqual(generated.returncode, 0, generated.stderr.decode())
            public_key = key_file.with_name(key_file.name + ".pub").read_bytes().strip()

            unrelated = b"ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABAQCtest administrator\n"
            project_prefix = (
                b'restrict,command="sudo -n '
                + (root / "usr" / "local" / "sbin" / "rsshub-cookie-sync-apply").as_posix().encode()
                + b'" ssh-ed25519 AAAAold old-release\n'
            )
            authorized_keys = ssh_dir / "authorized_keys"
            authorized_keys.write_bytes(unrelated + project_prefix)
            authorized_keys.chmod(0o600)

            result = _run_script(
                source,
                root,
                command_bin,
                input_data=public_key + b"\n",
                env={"TEST_ACCOUNT": "1"},
            )
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            after = authorized_keys.read_bytes()
            self.assertIn(unrelated, after)
            self.assertNotIn(project_prefix, after)
            self.assertIn(
                b'restrict,command="sudo -n '
                + (root / "usr" / "local" / "sbin" / "rsshub-cookie-sync-apply").as_posix().encode()
                + b'" '
                + public_key
                + b"\n",
                after,
            )

    def _write_fake_deployment(self, root: Path, include_account: bool = False):
        """Create the minimum managed tree consumed by the uninstall harness."""
        config_dir = root / "etc" / "rsshub-cookie-sync"
        config_dir.mkdir(parents=True, mode=0o700)
        deployment_root = root / "deploy"
        secrets_dir = deployment_root / "secrets"
        secrets_dir.mkdir(parents=True)
        compose = deployment_root / "docker-compose.yml"
        live_env = secrets_dir / "rsshub.env"
        candidate_dir = secrets_dir / "candidates"
        compose.write_text("services:\n  rsshub:\n    image: test\n", encoding="utf-8")
        live_env.write_text("ZHIHU_COOKIES=keep-me\n", encoding="utf-8")
        state_dir = root / "var" / "lib" / "rsshub-cookie-sync"
        state_dir.mkdir(parents=True, mode=0o700)
        state_file = state_dir / "state.json"
        state_file.write_text("{}\n", encoding="utf-8")
        state_file.chmod(0o600)
        config = {
            "deployment": {
                "compose_file": str(compose),
                "live_env": str(live_env),
                "candidate_dir": str(candidate_dir),
                "state_file": str(state_file),
                "lock_file": str(state_dir / "lock"),
                "project": "rsshub",
                "service": "rsshub",
            }
        }
        config_file = config_dir / "config.json"
        import json

        config_file.write_text(json.dumps(config) + "\n", encoding="utf-8")
        config_file.chmod(0o600)

        if include_account:
            ssh_dir = root / "var" / "lib" / "rsshub-sync" / ".ssh"
            ssh_dir.mkdir(parents=True, mode=0o700)
            ssh_dir.chmod(0o700)
            managed_path = root / "usr" / "local" / "sbin" / "rsshub-cookie-sync-apply"
            managed = (
                'restrict,command="sudo -n '
                + managed_path.as_posix()
                + '" ssh-ed25519 AAAAmanaged project\n'
            )
            unrelated = "ssh-ed25519 AAAAunrelated administrator\n"
            authorized_keys = ssh_dir / "authorized_keys"
            authorized_keys.write_text(unrelated + managed, encoding="utf-8")
            authorized_keys.chmod(0o600)

        return {
            "config_file": config_file,
            "compose": compose,
            "live_env": live_env,
            "candidate_dir": candidate_dir,
            "state_dir": state_dir,
            "state_file": state_file,
        }

    def test_uninstall_refuses_pending_transaction_without_deleting_rsshub_state(self):
        """A recoverable migration marker is preserved instead of being discarded."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_bin = _fake_command_bin(root)
            source = _rewrite_server_script(self._source(UNINSTALL_SCRIPT), root, command_bin)
            deployment = self._write_fake_deployment(root)
            pending = Path(str(deployment["compose"]) + ".pre-cookie-sync")
            pending.write_text("rollback material\n", encoding="utf-8")

            result = _run_script(source, root, command_bin, args=("--yes",))

            self.assertNotEqual(result.returncode, 0)
            self.assertTrue(pending.exists())
            self.assertTrue(deployment["config_file"].exists())
            self.assertTrue(deployment["state_file"].exists())
            self.assertTrue(deployment["compose"].exists())
            self.assertTrue(deployment["live_env"].exists())

    def test_uninstall_is_idempotent_and_preserves_compose_and_live_env(self):
        """A clean uninstall succeeds twice and leaves RSSHub inputs untouched."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_bin = _fake_command_bin(root)
            source = _rewrite_server_script(self._source(UNINSTALL_SCRIPT), root, command_bin)
            deployment = self._write_fake_deployment(root)
            compose_before = deployment["compose"].read_bytes()
            live_before = deployment["live_env"].read_bytes()

            first = _run_script(source, root, command_bin, args=("--yes",))
            second = _run_script(source, root, command_bin, args=("--yes",))

            self.assertEqual(first.returncode, 0, first.stderr.decode())
            self.assertEqual(second.returncode, 0, second.stderr.decode())
            self.assertEqual(deployment["compose"].read_bytes(), compose_before)
            self.assertEqual(deployment["live_env"].read_bytes(), live_before)
            self.assertFalse(deployment["config_file"].exists())
            self.assertFalse(deployment["state_file"].exists())
            self.assertIn("已经卸载", second.stderr.decode())

    def test_uninstall_keeps_preexisting_account_and_unrelated_authorized_key(self):
        """Without the creation marker, only the project's exact key is removed."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            command_bin = _fake_command_bin(root)
            source = _rewrite_server_script(self._source(UNINSTALL_SCRIPT), root, command_bin)
            deployment = self._write_fake_deployment(root, include_account=True)
            authorized_keys = root / "var" / "lib" / "rsshub-sync" / ".ssh" / "authorized_keys"
            userdel_sentinel = root / "userdel-called"

            result = _run_script(
                source,
                root,
                command_bin,
                args=("--yes",),
                env={
                    "TEST_ACCOUNT": "1",
                    "TEST_USERDEL_SENTINEL": str(userdel_sentinel),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertTrue((root / "var" / "lib" / "rsshub-sync").exists())
            self.assertFalse(userdel_sentinel.exists())
            remaining = authorized_keys.read_text(encoding="utf-8")
            self.assertIn("AAAAunrelated", remaining)
            self.assertNotIn("AAAAmanaged", remaining)

    def test_install_final_validation_and_rollback_are_armed_before_success(self):
        """Installer success is reachable only after validation and rollback cleanup."""
        source = self._source(INSTALL_SCRIPT)
        success = source.rfind("RSSHub Cookie sync server installed")
        self.assertGreater(success, 0)
        for required in (
            "validate_existing_account\nvalidate_locked_account_password",
            "has_valid_managed_authorized_key || die",
            "visudo -cf /etc/sudoers.d/rsshub-cookie-sync",
            "sshd -t >/dev/null 2>&1 || die",
            "systemctl show -p LoadState --value rsshub-cookie-sync-monitor.service",
            "systemctl is-enabled --quiet rsshub-cookie-sync-monitor.timer",
            "systemctl is-active --quiet rsshub-cookie-sync-monitor.timer",
        ):
            with self.subTest(required=required):
                self.assertLess(source.find(required), success)
        self.assertLess(source.find("trap rollback_install EXIT"), source.find("# Freeze the existing scheduler"))
        rollback = source.find("rollback-migration --json")
        rollback_restore_config = source.find("if ! restore_config", rollback)
        self.assertGreaterEqual(rollback, 0)
        self.assertGreater(rollback_restore_config, rollback)
        self.assertGreater(source.find("trap - EXIT HUP INT TERM"), source.find("systemctl is-active --quiet rsshub-cookie-sync-monitor.timer"))

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

    def test_first_install_requires_or_reuses_a_restricted_public_key(self):
        source = self._source(INSTALL_SCRIPT)
        self.assertIn("--public-key-file", source)
        self.assertIn("< /dev/tty", source)
        self.assertIn("首次安装必须提供 SSH 公钥", source)
        self.assertIn("has_valid_managed_authorized_key", source)
        self.assertIn("authorized_keys_touched=1", source)
        self.assertIn("restore_authorized_keys", source)
        self.assertIn("rsshub-cookie-sync-account-created=1", source)

    def test_reinstall_does_not_backup_the_existing_project_sshd_rule(self):
        source = self._source(INSTALL_SCRIPT)
        self.assertIn("ssh_config_matches_project", source)
        self.assertIn(
            'if [ "$PERSISTENT_SSH_BACKUP_EXISTS" -eq 0 ] && ! ssh_config_matches_project; then',
            source,
        )
        self.assertIn("Treat semantically identical files as", source)

    def test_project_sshd_rule_matcher_distinguishes_legacy_and_custom_rules(self):
        source = self._source(INSTALL_SCRIPT)
        function = re.search(r"(?ms)^ssh_config_matches_project\(\) \{.*?^\}", source)
        self.assertIsNotNone(function)
        assert function is not None
        expected = self._source(SERVER_DIR / "sshd-rsshub-cookie-sync.conf")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "sshd-rsshub-cookie-sync.conf").write_text(expected, encoding="utf-8")
            current = root / "current.conf"
            harness = (
                f'SCRIPT_DIR="{root}"\n'
                f'SSH_CONFIG="{current}"\n'
                + function.group(0)
                + '\nssh_config_matches_project\n'
            )
            current.write_text("# old release comment\n" + "\n".join(
                line for line in expected.splitlines() if not line.lstrip().startswith("#") and line.strip()
            ) + "\n", encoding="utf-8")
            self.assertEqual(subprocess.run(["sh"], input=harness, text=True, check=False).returncode, 0)
            current.write_text(expected.replace("PermitTTY no", "PermitTTY yes", 1), encoding="utf-8")
            self.assertNotEqual(subprocess.run(["sh"], input=harness, text=True, check=False).returncode, 0)

    def test_server_bootstrap_can_download_the_matching_uninstaller(self):
        source = self._source(BOOTSTRAP_SCRIPT)
        self.assertIn('"${1:-}" = uninstall', source)
        self.assertIn("server/uninstall.sh", source)
        self.assertIn('sh "$UNINSTALLER" "$@"', source)

    def test_uninstaller_confirms_and_only_deletes_marked_accounts(self):
        source = self._source(UNINSTALL_SCRIPT)
        self.assertIn("confirm_uninstall", source)
        self.assertIn("--yes", source)
        self.assertIn("ACCOUNT_MARKER", source)
        self.assertIn("userdel --remove", source)
        self.assertIn("account-created marker 无法确认来源，保留 rsshub-sync 账号", source)
        self.assertIn("RSSHub docker-compose.yml", source)

    def test_uninstaller_validates_dynamic_candidate_path_before_cleanup(self):
        source = self._source(UNINSTALL_SCRIPT)
        self.assertIn("case \"$1\" in\n        /*) ;;\n        *) return 1 ;;", source)
        self.assertIn('basename -- "$CANDIDATE_DIR"', source)
        self.assertIn('basename -- "$candidate_parent"', source)
        self.assertIn('[ "$LIVE_ENV" = "$candidate_parent/rsshub.env" ]', source)
        self.assertIn('"$LIVE_ENV.pre-cookie-sync"', source)
        self.assertNotIn('remove_migration_file "$LIVE_ENV"', source)

    def test_uninstaller_path_validator_accepts_only_canonical_absolute_paths(self):
        source = self._source(UNINSTALL_SCRIPT)
        function = re.search(r"(?ms)^valid_path_chars\(\) \{.*?^\}", source)
        self.assertIsNotNone(function)
        assert function is not None
        harness = function.group(0) + '\nvalid_path_chars "$1"\n'
        valid = "/root/rsshub/secrets/candidates"
        invalid = (
            "relative/path",
            "/root/rsshub//secrets/candidates",
            "/root/rsshub/secrets/../other",
            "/root/rsshub/secrets/candidates/",
            "/root/rsshub/secrets/candidates with spaces",
        )
        self.assertEqual(
            subprocess.run(["sh", "-c", harness, "sh", valid], check=False).returncode,
            0,
        )
        for path in invalid:
            with self.subTest(path=path):
                self.assertNotEqual(
                    subprocess.run(["sh", "-c", harness, "sh", path], check=False).returncode,
                    0,
                )

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

    def test_uninstall_preserves_compose_and_live_secret(self):
        """Uninstall removes synchronizer state but leaves RSSHub live data."""
        source = self._source(UNINSTALL_SCRIPT)

        # Runtime state and verified candidate cookies belong to the
        # synchronizer and are removed.  The Compose deployment and its live
        # env remain outside the deletion set.
        self.assertIn("STATE_DIR=/var/lib/rsshub-cookie-sync", source)
        self.assertIn("remove_candidate_files", source)
        self.assertIn("rsshub.env", source)
        self.assertIn("Delete state and lock files", source)
        self.assertRegex(source, r"(?i)(?:compose|rsshub\.env).*?(?:retain|保留|未被修改)")

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
                # The live env is never a direct deletion target.  Candidate
                # and state cleanup are intentional and occur only after
                # their path/ownership checks.
                self.assertNotRegex(line, r"(?i)rm\s+-f\s+\"\$LIVE_ENV\"(?:\s|$)")


if __name__ == "__main__":
    unittest.main()
