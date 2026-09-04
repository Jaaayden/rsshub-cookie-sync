#!/usr/bin/env python3
"""Local-only tests for Native Messaging host installation helpers."""

from __future__ import annotations

import json
import io
import os
import stat
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import install
import uninstall


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.app_dir = self.root / "Application Support" / "RSSHub Cookie Sync"
        self.edge_dir = self.root / "Microsoft Edge" / "NativeMessagingHosts"
        self.extension_id = "abcdefghijklmnop" * 2
        self.server_host = "rsshub.example.test"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_manifest_has_exact_extension_origin(self) -> None:
        manifest = install.build_manifest(self.app_dir / "native_host.py", self.extension_id)
        self.assertEqual(manifest["name"], "com.jayden.rsshub_cookie_sync")
        self.assertEqual(manifest["type"], "stdio")
        self.assertEqual(
            manifest["allowed_origins"], [f"chrome-extension://{self.extension_id}/"]
        )
        with self.assertRaises(install.InstallError):
            install.build_manifest(self.app_dir / "native_host.py", "z" * 32)

    def test_known_hosts_only_checks_local_file_safety(self) -> None:
        source = self.root / "known_hosts.source"
        source.write_bytes("# 注释\n|1|hashed-host ssh-rsa AAAA\n".encode("utf-8"))
        source.chmod(0o600)
        self.assertEqual(install._read_known_hosts_source(source), source.read_bytes())
        nul = self.root / "known_hosts.nul"
        nul.write_bytes(b"host ssh-ed25519 AAAA\x00\n")
        nul.chmod(0o600)
        with self.assertRaises(install.InstallError):
            install._read_known_hosts_source(nul)

    def test_install_with_existing_key_is_local_and_atomic(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "rsshub-cookie-sync"
        identity.write_text("private", encoding="ascii")
        identity.with_name(identity.name + ".pub").write_text(
            "ssh-ed25519 AAAA test\n", encoding="ascii"
        )
        identity.chmod(0o600)
        source_known_hosts = self.root / "known_hosts.source"
        source_known_hosts.write_text(
            f"[{self.server_host}]:22 ssh-ed25519 AAAA\n", encoding="ascii"
        )

        def fake_derive(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            self.assertEqual(argv[:3], ["/usr/bin/ssh-keygen", "-y", "-f"])
            self.assertFalse(kwargs["shell"])
            return CompletedProcess(argv, 0, stdout=b"ssh-ed25519 AAAA test\n")

        with mock.patch.object(install.subprocess, "run", side_effect=fake_derive) as run:
            paths = install.install(
                extension_id=self.extension_id,
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                known_hosts_source=source_known_hosts,
                server_host=self.server_host,
                identity_file=identity,
                no_generate_key=True,
                allowed_root=self.root,
            )
        run.assert_called_once()
        self.assertTrue(paths["known_hosts_ready"])
        self.assertEqual(stat.S_IMODE(paths["config_path"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["manifest_path"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["host_path"].stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths["launcher_path"].stat().st_mode), 0o700)
        self.assertTrue(paths["uninstall_script"].is_file())
        self.assertTrue(paths["uninstall_python"].is_file())
        self.assertTrue(paths["uninstall_host"].is_file())
        self.assertEqual(
            paths["uninstall_script"],
            self.root / "Library" / "Application Support" / "rsshub-cookie-sync" / "uninstall.sh",
        )
        self.assertEqual(stat.S_IMODE(paths["uninstall_script"].stat().st_mode), 0o700)
        self.assertIn(str(paths["app_support_dir"]), paths["uninstall_script"].read_text())
        installed_host = paths["host_path"].read_text(encoding="utf-8")
        self.assertTrue(installed_host.startswith("#!/usr/bin/env python3\n\"\"\"Microsoft Edge Native Messaging host"))
        self.assertIn("def run_host(", installed_host)
        self.assertNotIn("def install(", installed_host)
        config = json.loads(paths["config_path"].read_text(encoding="utf-8"))
        self.assertEqual(config["host_name"], "com.jayden.rsshub_cookie_sync")
        self.assertEqual(config["server"]["host"], self.server_host)
        self.assertNotIn("proxy", config)
        self.assertEqual(config["ssh"]["identity_file"], str(identity))
        self.assertEqual(config["ssh"]["known_hosts_file"], str(self.root / ".ssh" / "known_hosts"))
        manifest = json.loads(paths["manifest_path"].read_text(encoding="utf-8"))
        self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{self.extension_id}/"])
        self.assertEqual(manifest["path"], str(paths["launcher_path"]))
        launcher = paths["launcher_path"].read_text(encoding="utf-8")
        self.assertIn(str(Path(os.sys.executable).expanduser().absolute()), launcher)
        self.assertIn('"$@"', launcher)

    def test_key_generation_uses_ssh_keygen_without_shell(self) -> None:
        def fake_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            self.assertEqual(argv[:5], ["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-f"])
            self.assertFalse(kwargs["shell"])
            key_path = Path(argv[argv.index("-f") + 1])
            key_path.write_text("private", encoding="ascii")
            key_path.with_name(key_path.name + ".pub").write_text(
                "ssh-ed25519 AAAA test\n", encoding="ascii"
            )
            return CompletedProcess(argv, 0)

        known_hosts = self.root / "known_hosts.source"
        known_hosts.write_text(
            f"[{self.server_host}]:22 ssh-ed25519 AAAA\n", encoding="ascii"
        )
        with mock.patch.object(install.subprocess, "run", side_effect=fake_keygen) as run:
            install.install(
                extension_id=self.extension_id,
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                known_hosts_source=known_hosts,
                server_host=self.server_host,
                allowed_root=self.root,
            )
        run.assert_called_once()
        call = run.call_args
        self.assertFalse(call.kwargs["shell"])
        self.assertEqual(stat.S_IMODE((self.root / ".ssh" / "rsshub-cookie-sync").stat().st_mode), 0o600)

    def test_real_ssh_keygen_recovers_pub_and_reuses_private_without_leakage(self) -> None:
        """Exercise the macOS install path with the system ssh-keygen binary."""

        ssh_keygen = Path("/usr/bin/ssh-keygen")
        if not ssh_keygen.is_file():
            self.skipTest("/usr/bin/ssh-keygen 不存在")

        # Run the CLI once so the assertion covers the user-visible public-key
        # output, while redirecting the module defaults into this temporary
        # home.  The private key is read only for a negative leakage check.
        output = io.StringIO()
        errors = io.StringIO()
        default_app_dir = self.root / "Library" / "Application Support" / "RSSHub Cookie Sync"
        default_edge_dir = self.root / "Library" / "Application Support" / "Microsoft Edge" / "NativeMessagingHosts"
        default_uninstall_dir = self.root / "Library" / "Application Support" / "rsshub-cookie-sync"
        with (
            mock.patch.object(install.Path, "home", return_value=self.root),
            mock.patch.object(install, "DEFAULT_APP_SUPPORT_DIR", default_app_dir),
            mock.patch.object(install, "DEFAULT_EDGE_MANIFEST_DIR", default_edge_dir),
            mock.patch.object(install, "DEFAULT_UNINSTALL_DIR", default_uninstall_dir),
            redirect_stdout(output),
            redirect_stderr(errors),
        ):
            result = install.main([])
        self.assertEqual(result, 0)

        identity = self.root / ".ssh" / "rsshub-cookie-sync"
        public = identity.with_name("rsshub-cookie-sync.pub")
        private_before = identity.read_bytes()
        public_before = public.read_bytes()
        identity_stat_before = identity.stat()
        self.assertEqual(stat.S_IMODE(identity_stat_before.st_mode), 0o600)
        self.assertIn(b"ssh-ed25519 ", public_before)
        self.assertNotIn(b"PRIVATE KEY", output.getvalue().encode())
        self.assertNotIn(b"PRIVATE KEY", errors.getvalue().encode())
        self.assertNotIn(private_before, output.getvalue().encode())
        self.assertNotIn(private_before, errors.getvalue().encode())
        self.assertIn("ssh-ed25519 ", output.getvalue())

        # Deleting the sidecar is a supported repair operation.  It must be
        # reconstructed from the private key, and reinstall must not replace
        # the private key (including its inode).
        public.unlink()
        paths = install.install(
            app_support_dir=default_app_dir,
            edge_manifest_dir=default_edge_dir,
            allowed_root=self.root,
        )
        self.assertEqual(paths["identity_file"], identity)
        self.assertTrue(public.is_file())
        self.assertEqual(
            install._public_key_identity(public.read_bytes()),
            install._public_key_identity(public_before),
        )
        self.assertEqual(identity.read_bytes(), private_before)
        self.assertEqual(identity.stat().st_ino, identity_stat_before.st_ino)

        # A second reinstall with an existing sidecar keeps the same private
        # key as well; this catches accidental regeneration on routine runs.
        install.install(
            app_support_dir=default_app_dir,
            edge_manifest_dir=default_edge_dir,
            allowed_root=self.root,
        )
        self.assertEqual(identity.read_bytes(), private_before)
        self.assertEqual(identity.stat().st_ino, identity_stat_before.st_ino)

    def test_uninstall_only_removes_managed_files(self) -> None:
        ssh_dir = self.app_dir / "ssh"
        ssh_dir.mkdir(parents=True)
        for name in ("id_ed25519", "id_ed25519.pub", "known_hosts"):
            (ssh_dir / name).write_text("x", encoding="ascii")
        (self.app_dir / "native_host.py").write_text("x", encoding="ascii")
        (self.app_dir / "native_host").write_text("x", encoding="ascii")
        # Even a modified installer config must not redirect uninstall to
        # another file under Application Support.
        (self.app_dir / "config.json").write_text(
            json.dumps({"ssh": {"identity_file": str(self.app_dir / "keep.txt")}}),
            encoding="ascii",
        )
        self.edge_dir.mkdir(parents=True)
        manifest = self.edge_dir / "com.jayden.rsshub_cookie_sync.json"
        manifest.write_text("{}", encoding="ascii")
        uninstall_dir = self.root / "Library" / "Application Support" / "rsshub-cookie-sync"
        uninstall_dir.mkdir(parents=True)
        for name in ("uninstall.sh", "uninstall.py", "native_host.py"):
            (uninstall_dir / name).write_text("managed", encoding="ascii")
        home_ssh = self.root / ".ssh"
        home_ssh.mkdir(parents=True)
        dedicated = home_ssh / "rsshub-cookie-sync"
        dedicated.write_text("dedicated", encoding="ascii")
        dedicated.with_name("rsshub-cookie-sync.pub").write_text("public", encoding="ascii")
        generic = home_ssh / "id_ed25519"
        generic.write_text("generic", encoding="ascii")
        unrelated = self.app_dir / "keep.txt"
        unrelated.write_text("keep", encoding="ascii")

        removed = uninstall.uninstall(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            uninstall_dir=uninstall_dir,
            allowed_root=self.root,
        )
        self.assertEqual(len(removed), 7)
        self.assertTrue(unrelated.exists())
        self.assertTrue(dedicated.exists())
        self.assertTrue(dedicated.with_name("rsshub-cookie-sync.pub").exists())
        self.assertTrue(generic.exists())
        self.assertTrue((ssh_dir / "id_ed25519").exists())
        self.assertTrue((ssh_dir / "known_hosts").exists())
        self.assertFalse(manifest.exists())
        self.assertFalse((self.app_dir / "native_host.py").exists())
        self.assertFalse((self.app_dir / "native_host").exists())
        self.assertFalse(uninstall_dir.exists())

    def test_uninstall_purge_key_removes_only_project_keys(self) -> None:
        ssh_dir = self.app_dir / "ssh"
        ssh_dir.mkdir(parents=True)
        for name in ("id_ed25519", "id_ed25519.pub"):
            (ssh_dir / name).write_text("legacy", encoding="ascii")
        home_ssh = self.root / ".ssh"
        home_ssh.mkdir(parents=True)
        dedicated = home_ssh / "rsshub-cookie-sync"
        dedicated.write_text("dedicated", encoding="ascii")
        dedicated.with_name("rsshub-cookie-sync.pub").write_text("public", encoding="ascii")
        generic = home_ssh / "id_ed25519"
        generic.write_text("generic", encoding="ascii")

        uninstall.uninstall(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            allowed_root=self.root,
            purge_key=True,
        )
        self.assertFalse(dedicated.exists())
        self.assertFalse(dedicated.with_name("rsshub-cookie-sync.pub").exists())
        self.assertFalse((ssh_dir / "id_ed25519").exists())
        self.assertFalse((ssh_dir / "id_ed25519.pub").exists())
        self.assertTrue(generic.exists())

    def test_real_uninstall_is_idempotent_and_preserves_shared_ssh_material(self) -> None:
        """Use a real install to verify exact files are removed and shared files survive."""

        if not Path("/usr/bin/ssh-keygen").is_file():
            self.skipTest("/usr/bin/ssh-keygen 不存在")

        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        known_hosts = ssh_dir / "known_hosts"
        known_hosts.write_text("other.example ssh-ed25519 AAAA\n", encoding="ascii")
        known_hosts.chmod(0o600)
        generic = ssh_dir / "id_ed25519"
        generic.write_text("generic private", encoding="ascii")
        generic.chmod(0o600)
        generic.with_name("id_ed25519.pub").write_text(
            "ssh-ed25519 AAAA generic\n", encoding="ascii"
        )
        other_manifest = self.edge_dir / "unrelated-extension.json"
        other_manifest.parent.mkdir(parents=True)
        other_manifest.write_text("{\"name\":\"unrelated\"}\n", encoding="utf-8")

        paths = install.install(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            allowed_root=self.root,
        )
        dedicated = self.root / ".ssh" / "rsshub-cookie-sync"
        dedicated_public = dedicated.with_name("rsshub-cookie-sync.pub")
        known_hosts_before = known_hosts.read_bytes()

        removed = uninstall.uninstall(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            uninstall_dir=paths["uninstall_dir"],
            allowed_root=self.root,
        )
        self.assertTrue(removed)
        self.assertFalse(paths["manifest_path"].exists())
        self.assertTrue(other_manifest.exists())
        self.assertTrue(known_hosts.exists())
        self.assertEqual(known_hosts.read_bytes(), known_hosts_before)
        self.assertTrue(dedicated.exists())
        self.assertTrue(dedicated_public.exists())
        self.assertTrue(generic.exists())

        # A second ordinary uninstall is a no-op and must not turn unrelated
        # files into an error condition.
        self.assertEqual(
            uninstall.uninstall(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                uninstall_dir=paths["uninstall_dir"],
                allowed_root=self.root,
            ),
            [],
        )
        self.assertTrue(other_manifest.exists())
        self.assertTrue(known_hosts.exists())
        self.assertTrue(generic.exists())

        # Reinstall, then explicitly purge: only the project-dedicated pair
        # may disappear; the user's normal SSH key, known_hosts, and unrelated
        # Edge manifest remain available for other tools.
        paths = install.install(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            allowed_root=self.root,
        )
        uninstall.uninstall(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            uninstall_dir=paths["uninstall_dir"],
            allowed_root=self.root,
            purge_key=True,
        )
        self.assertFalse(dedicated.exists())
        self.assertFalse(dedicated_public.exists())
        self.assertTrue(generic.exists())
        self.assertTrue(known_hosts.exists())
        self.assertEqual(known_hosts.read_bytes(), known_hosts_before)
        self.assertTrue(other_manifest.exists())

        # Purge is idempotent too, including after the dedicated key has gone.
        self.assertEqual(
            uninstall.uninstall(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                uninstall_dir=paths["uninstall_dir"],
                allowed_root=self.root,
                purge_key=True,
            ),
            [],
        )
        self.assertTrue(generic.exists())

    def test_uninstall_cli_explains_extension_and_retention(self) -> None:
        home_ssh = self.root / ".ssh"
        home_ssh.mkdir(parents=True)
        (home_ssh / "rsshub-cookie-sync").write_text("dedicated", encoding="ascii")
        (home_ssh / "rsshub-cookie-sync.pub").write_text("public", encoding="ascii")
        output = io.StringIO()
        with mock.patch.object(uninstall.Path, "home", return_value=self.root), redirect_stdout(output):
            result = uninstall.main(
                [
                    "--app-support-dir",
                    str(self.app_dir),
                    "--edge-manifest-dir",
                    str(self.edge_dir),
                    "--uninstall-dir",
                    str(self.root / "Library" / "Application Support" / "rsshub-cookie-sync"),
                ]
            )
        self.assertEqual(result, 0)
        message = output.getvalue()
        self.assertIn("项目专用 SSH 密钥已保留", message)
        self.assertIn("~/.ssh/known_hosts 保留", message)
        self.assertIn("edge://extensions", message)
        self.assertTrue((home_ssh / "rsshub-cookie-sync").exists())

    def test_config_has_no_proxy_section(self) -> None:
        config = install.build_config(
            identity_file=self.root / ".ssh" / "rsshub-cookie-sync",
            known_hosts_file=self.root / ".ssh" / "known_hosts",
            server_host=self.server_host,
        )
        self.assertNotIn("proxy", config)

    def test_installer_rejects_non_sync_server_user(self) -> None:
        with self.assertRaises(install.InstallError):
            install.build_config(
                identity_file=self.root / ".ssh" / "rsshub-cookie-sync",
                known_hosts_file=self.root / ".ssh" / "known_hosts",
                server_host=self.server_host,
                server_user="root",
            )

    def test_installer_rejects_non_ed25519_identity(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "id_rsa"
        identity.write_text("private", encoding="ascii")
        identity.chmod(0o600)
        identity.with_name("id_rsa.pub").write_text(
            "ssh-rsa AAAA unsupported\n", encoding="ascii"
        )
        with self.assertRaises(install.InstallError):
            install._ensure_identity(identity, generate=False)

    def test_missing_public_sidecar_is_recovered_for_dedicated_key(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "rsshub-cookie-sync"
        identity.write_text("private", encoding="ascii")
        identity.chmod(0o600)

        def fake_public_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            self.assertEqual(argv, ["/usr/bin/ssh-keygen", "-y", "-f", str(identity)])
            self.assertFalse(kwargs["shell"])
            self.assertEqual(kwargs["stdin"], install.subprocess.DEVNULL)
            return CompletedProcess(argv, 0, stdout=b"ssh-ed25519 AAAA recovered\n")

        with mock.patch.object(install.subprocess, "run", side_effect=fake_public_keygen) as run:
            install._ensure_identity(identity, generate=False)
        run.assert_called_once()
        public = identity.with_name("rsshub-cookie-sync.pub")
        self.assertEqual(public.read_bytes(), b"ssh-ed25519 AAAA recovered\n")
        self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o644)

    def test_missing_public_sidecar_is_not_recovered_for_generic_key(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "id_ed25519"
        identity.write_text("private", encoding="ascii")
        identity.chmod(0o600)
        with mock.patch.object(install.subprocess, "run") as run:
            with self.assertRaises(install.InstallError):
                install._ensure_identity(identity, generate=False)
        run.assert_not_called()

    def test_existing_public_sidecar_mismatch_is_rejected_without_overwrite(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "rsshub-cookie-sync"
        public = identity.with_name("rsshub-cookie-sync.pub")
        identity.write_text("private", encoding="ascii")
        identity.chmod(0o600)
        public.write_text("ssh-ed25519 AAAA old\n", encoding="ascii")
        original = public.read_bytes()

        def fake_public_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            return CompletedProcess(argv, 0, stdout=b"ssh-ed25519 BBBB new\n")

        with mock.patch.object(install.subprocess, "run", side_effect=fake_public_keygen):
            with self.assertRaises(install.InstallError):
                install._ensure_identity(identity, generate=False)
        self.assertEqual(public.read_bytes(), original)

    def test_private_key_algorithm_is_authoritative(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "rsshub-cookie-sync"
        public = identity.with_name("rsshub-cookie-sync.pub")
        identity.write_text("rsa private", encoding="ascii")
        identity.chmod(0o600)
        public.write_text("ssh-ed25519 AAAA forged\n", encoding="ascii")

        def fake_public_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            return CompletedProcess(argv, 0, stdout=b"ssh-rsa AAAA actual\n")

        with mock.patch.object(install.subprocess, "run", side_effect=fake_public_keygen):
            with self.assertRaises(install.InstallError):
                install._ensure_identity(identity, generate=False)
        self.assertEqual(public.read_text(encoding="ascii"), "ssh-ed25519 AAAA forged\n")

    def test_invalid_identity_does_not_replace_an_installed_host(self) -> None:
        self.app_dir.mkdir(parents=True)
        old_host = self.app_dir / "native_host.py"
        old_launcher = self.app_dir / "native_host"
        old_host.write_text("old host\n", encoding="ascii")
        old_launcher.write_text("old launcher\n", encoding="ascii")
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir()
        identity = ssh_dir / "id_rsa"
        identity.write_text("private", encoding="ascii")
        identity.chmod(0o600)
        identity.with_name("id_rsa.pub").write_text(
            "ssh-rsa AAAA unsupported\n", encoding="ascii"
        )

        with self.assertRaises(install.InstallError):
            install.install(
                extension_id=self.extension_id,
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                identity_file=identity,
                no_generate_key=True,
                allowed_root=self.root,
            )

        self.assertEqual(old_host.read_text(encoding="ascii"), "old host\n")
        self.assertEqual(old_launcher.read_text(encoding="ascii"), "old launcher\n")

    def test_zero_argument_cli_uses_public_defaults(self) -> None:
        args = install._parse_args([])
        self.assertEqual(args.extension_id, install.DEFAULT_EXTENSION_ID)
        self.assertIsNone(args.server_host)
        self.assertIsNone(args.server_port)
        self.assertIsNone(args.server_user)
        self.assertIsNone(args.identity_file)
        self.assertFalse(args.prepare_dedicated_key)
        self.assertFalse(args.activate_dedicated_key)

        args = install._parse_args(
            ["--extension-id", self.extension_id, "--server-host", self.server_host]
        )
        self.assertEqual(args.server_host, self.server_host)
        self.assertIsNone(args.identity_file)

        prepared = install._parse_args(["--prepare-dedicated-key"])
        self.assertTrue(prepared.prepare_dedicated_key)
        with self.assertRaises(SystemExit):
            install._parse_args(
                ["--prepare-dedicated-key", "--activate-dedicated-key"]
            )

    def test_zero_argument_install_creates_default_key_and_unconfigured_server(self) -> None:
        def fake_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            key_path = Path(argv[argv.index("-f") + 1])
            key_path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="ascii")
            key_path.with_name(f"{key_path.name}.pub").write_text(
                "ssh-ed25519 AAAA test\n", encoding="ascii"
            )
            return CompletedProcess(argv, 0)

        with mock.patch.object(install.subprocess, "run", side_effect=fake_keygen):
            paths = install.install(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                allowed_root=self.root,
            )
        self.assertEqual(paths["identity_file"], self.root / ".ssh" / "rsshub-cookie-sync")
        self.assertFalse(paths["server_configured"])
        config = json.loads(paths["config_path"].read_text(encoding="utf-8"))
        self.assertIsNone(config["server"]["host"])
        self.assertNotIn("proxy", config)
        self.assertEqual(
            json.loads(paths["manifest_path"].read_text(encoding="utf-8"))["allowed_origins"],
            [f"chrome-extension://{install.DEFAULT_EXTENSION_ID}/"],
        )
        self.assertEqual(stat.S_IMODE(paths["identity_file"].stat().st_mode), 0o600)

    def test_fresh_install_never_falls_back_to_generic_id_ed25519(self) -> None:
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir(parents=True)
        generic = ssh_dir / "id_ed25519"
        generic.write_text("generic private", encoding="ascii")
        generic.chmod(0o600)
        generic.with_name("id_ed25519.pub").write_text(
            "ssh-ed25519 AAAA generic\n", encoding="ascii"
        )

        def fake_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            key_path = Path(argv[argv.index("-f") + 1])
            self.assertEqual(key_path, ssh_dir / "rsshub-cookie-sync")
            key_path.write_text("dedicated private", encoding="ascii")
            key_path.with_name(f"{key_path.name}.pub").write_text(
                "ssh-ed25519 AAAA dedicated\n", encoding="ascii"
            )
            return CompletedProcess(argv, 0)

        with mock.patch.object(install.subprocess, "run", side_effect=fake_keygen):
            paths = install.install(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                allowed_root=self.root,
            )
        self.assertEqual(paths["identity_file"], ssh_dir / "rsshub-cookie-sync")
        self.assertEqual(generic.read_text(encoding="ascii"), "generic private")

    def test_reinstall_does_not_regenerate_missing_configured_key(self) -> None:
        self.app_dir.mkdir(parents=True)
        config_path = self.app_dir / "config.json"
        dedicated = self.root / ".ssh" / "rsshub-cookie-sync"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "server": {"host": None, "port": 22, "user": "rsshub-sync"},
                    "ssh": {
                        "identity_file": str(dedicated),
                        "known_hosts_file": str(self.root / ".ssh" / "known_hosts"),
                    },
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        old_host = self.app_dir / "native_host.py"
        old_host.write_text("old host\n", encoding="ascii")

        with mock.patch.object(install.subprocess, "run") as run:
            with self.assertRaises(install.InstallError):
                install.install(
                    app_support_dir=self.app_dir,
                    edge_manifest_dir=self.edge_dir,
                    allowed_root=self.root,
                )
        run.assert_not_called()
        self.assertFalse(dedicated.exists())
        self.assertEqual(old_host.read_text(encoding="ascii"), "old host\n")

    def test_reinstall_never_silently_retains_generic_configured_key(self) -> None:
        self.app_dir.mkdir(parents=True)
        ssh_dir = self.root / ".ssh"
        ssh_dir.mkdir()
        generic = ssh_dir / "id_ed25519"
        generic.write_text("generic private", encoding="ascii")
        generic.chmod(0o600)
        generic.with_name("id_ed25519.pub").write_text(
            "ssh-ed25519 AAAA generic\n", encoding="ascii"
        )
        config_path = self.app_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "server": {
                        "host": self.server_host,
                        "port": 22,
                        "user": "rsshub-sync",
                    },
                    "ssh": {
                        "identity_file": str(generic),
                        "known_hosts_file": str(ssh_dir / "known_hosts"),
                    },
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        original = config_path.read_bytes()
        old_host = self.app_dir / "native_host.py"
        old_launcher = self.app_dir / "native_host"
        old_host.write_text("old host\n", encoding="ascii")
        old_launcher.write_text("old launcher\n", encoding="ascii")

        with mock.patch.object(install.subprocess, "run") as run:
            with self.assertRaises(install.InstallError):
                install.install(
                    app_support_dir=self.app_dir,
                    edge_manifest_dir=self.edge_dir,
                    allowed_root=self.root,
                )
        run.assert_not_called()
        self.assertEqual(config_path.read_bytes(), original)
        self.assertEqual(old_host.read_text(encoding="ascii"), "old host\n")
        self.assertEqual(old_launcher.read_text(encoding="ascii"), "old launcher\n")
        self.assertFalse((ssh_dir / "rsshub-cookie-sync").exists())

    def test_legacy_identity_requires_prepare_provision_then_explicit_activation(self) -> None:
        legacy_ssh = self.app_dir / "ssh"
        legacy_ssh.mkdir(parents=True)
        legacy_key = legacy_ssh / "id_ed25519"
        legacy_key.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\n", encoding="ascii")
        legacy_key.with_name("id_ed25519.pub").write_text(
            "ssh-ed25519 AAAA legacy\n", encoding="ascii"
        )
        legacy_key.chmod(0o600)
        legacy_known_hosts = legacy_ssh / "known_hosts"
        legacy_known_hosts.write_text(
            f"[{self.server_host}]:2222 ssh-ed25519 AAAA\n", encoding="ascii"
        )
        legacy_known_hosts.chmod(0o600)
        standard_known_hosts = self.root / ".ssh" / "known_hosts"
        standard_known_hosts.parent.mkdir(parents=True, exist_ok=True)
        standard_known_hosts.write_text(
            f"[{self.server_host}]:2222 ssh-ed25519 AAAA\n", encoding="ascii"
        )
        standard_known_hosts.chmod(0o600)
        config_path = self.app_dir / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "host_name": install.HOST_NAME,
                    "server": {"host": self.server_host, "port": 2222, "user": "old-user"},
                    "proxy": {"type": "socks5", "host": "old-proxy", "port": 1080},
                    "ssh": {
                        "identity_file": str(legacy_key),
                        "known_hosts_file": str(legacy_known_hosts),
                    },
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        old_host = self.app_dir / "native_host.py"
        old_launcher = self.app_dir / "native_host"
        old_host.write_text("old host\n", encoding="ascii")
        old_launcher.write_text("old launcher\n", encoding="ascii")
        original_config = config_path.read_bytes()

        with mock.patch.object(install.subprocess, "run") as run:
            with self.assertRaises(install.InstallError):
                install.install(
                    app_support_dir=self.app_dir,
                    edge_manifest_dir=self.edge_dir,
                    allowed_root=self.root,
                    no_generate_key=True,
                )
        run.assert_not_called()
        self.assertEqual(config_path.read_bytes(), original_config)
        self.assertEqual(old_host.read_text(encoding="ascii"), "old host\n")
        self.assertEqual(old_launcher.read_text(encoding="ascii"), "old launcher\n")
        self.assertFalse((self.root / ".ssh" / "rsshub-cookie-sync").exists())

        def fake_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            key_path = Path(argv[argv.index("-f") + 1])
            key_path.write_text("private", encoding="ascii")
            key_path.with_name(f"{key_path.name}.pub").write_text(
                "ssh-ed25519 AAAA dedicated\n", encoding="ascii"
            )
            return CompletedProcess(argv, 0)

        with mock.patch.object(install.subprocess, "run", side_effect=fake_keygen):
            prepared = install.prepare_dedicated_identity(allowed_root=self.root)
        self.assertEqual(prepared, self.root / ".ssh" / "rsshub-cookie-sync")
        self.assertEqual(config_path.read_bytes(), original_config)

        rejected_probe = mock.Mock(return_value=CompletedProcess([], 255))
        with self.assertRaises(install.InstallError):
            install.install(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                allowed_root=self.root,
                no_generate_key=True,
                activate_dedicated_key=True,
                activation_runner=rejected_probe,
            )
        self.assertEqual(config_path.read_bytes(), original_config)
        self.assertEqual(old_host.read_text(encoding="ascii"), "old host\n")
        self.assertEqual(old_launcher.read_text(encoding="ascii"), "old launcher\n")

        accepted_probe = mock.Mock(return_value=CompletedProcess([], 1))
        def fake_derive(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            return CompletedProcess(argv, 0, stdout=b"ssh-ed25519 AAAA dedicated\n")

        with mock.patch.object(install.subprocess, "run", side_effect=fake_derive):
            paths = install.install(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                allowed_root=self.root,
                no_generate_key=True,
                activate_dedicated_key=True,
                activation_runner=accepted_probe,
            )
        accepted_probe.assert_called_once()
        probe_call = accepted_probe.call_args
        self.assertEqual(probe_call.kwargs["input"], b'{"version":1,"providers":{}}\n')
        self.assertNotIn("providers", "\0".join(probe_call.args[0]))
        self.assertEqual(paths["identity_file"], prepared)
        self.assertEqual(paths["known_hosts_file"], standard_known_hosts)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["server"], {"host": self.server_host, "port": 2222, "user": "rsshub-sync"})
        self.assertEqual(config["ssh"]["identity_file"], str(prepared))
        self.assertNotIn("proxy", config)

    def test_identity_file_must_be_direct_child_of_ssh_directory(self) -> None:
        with self.assertRaises(install.InstallError):
            install._identity_path(self.root / "outside-key", home=self.root)
        with self.assertRaises(install.InstallError):
            install._identity_path(self.root / ".ssh" / "nested" / "key", home=self.root)
        with self.assertRaises(install.InstallError):
            install._identity_path(self.root / ".ssh" / "id_ed25519", home=self.root)

    def test_existing_known_hosts_is_merged_not_replaced(self) -> None:
        destination = self.root / ".ssh" / "known_hosts"
        destination.parent.mkdir(parents=True)
        destination.write_text("other.example ssh-ed25519 BBBB\n", encoding="ascii")
        source = self.root / "known_hosts.source"
        source.write_text(
            f"[{self.server_host}]:22 ssh-ed25519 AAAA\n", encoding="ascii"
        )
        self.assertTrue(
            install._ensure_known_hosts(
                destination,
                source,
                server_host=self.server_host,
                server_port=22,
            )
        )
        value = destination.read_text(encoding="ascii")
        self.assertIn("other.example", value)
        self.assertIn(self.server_host, value)

    def test_install_does_not_require_preexisting_known_host_record(self) -> None:
        def fake_keygen(argv: list[str], **kwargs: object) -> CompletedProcess[object]:
            key_path = Path(argv[argv.index("-f") + 1])
            key_path.write_text("private", encoding="ascii")
            key_path.with_name(f"{key_path.name}.pub").write_text(
                "ssh-ed25519 AAAA generated\n", encoding="ascii"
            )
            return CompletedProcess(argv, 0)

        with mock.patch.object(install.subprocess, "run", side_effect=fake_keygen):
            paths = install.install(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                server_host=self.server_host,
                allowed_root=self.root,
            )
        self.assertFalse(paths["known_hosts_ready"])
        self.assertEqual(paths["known_hosts_file"], self.root / ".ssh" / "known_hosts")
        self.assertEqual(paths["known_hosts_file"].read_bytes(), b"")

    def test_macos_bootstrap_is_executable_and_has_safe_dispatch(self) -> None:
        bootstrap = Path(__file__).resolve().parents[1] / "scripts" / "install-macos.sh"
        self.assertTrue(bootstrap.is_file())
        self.assertEqual(stat.S_IMODE(bootstrap.stat().st_mode) & 0o111, 0o111)
        self.assertIn('[ "$(id -u)" -ne 0 ]', bootstrap.read_text(encoding="utf-8"))
        self.assertIn('command -v python3', bootstrap.read_text(encoding="utf-8"))
        self.assertIn('command -v uname', bootstrap.read_text(encoding="utf-8"))
        self.assertIn('[ "$(uname -s)" = "Darwin" ]', bootstrap.read_text(encoding="utf-8"))
        self.assertIn('ACTION=uninstall', bootstrap.read_text(encoding="utf-8"))
        self.assertIn('NF < 2', bootstrap.read_text(encoding="utf-8"))
        result = subprocess.run(["sh", "-n", str(bootstrap)], check=False)
        self.assertEqual(result.returncode, 0)

    def test_custom_install_roots_cannot_target_broad_or_external_directories(self) -> None:
        with self.assertRaises(install.InstallError):
            install.install(
                extension_id=self.extension_id,
                app_support_dir=self.root,
                edge_manifest_dir=self.edge_dir,
                server_host=self.server_host,
                allowed_root=self.root,
            )
        with self.assertRaises(RuntimeError):
            uninstall.uninstall(
                app_support_dir=self.root,
                edge_manifest_dir=self.edge_dir,
                allowed_root=self.root,
            )


if __name__ == "__main__":
    unittest.main()
