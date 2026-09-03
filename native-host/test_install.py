#!/usr/bin/env python3
"""Local-only tests for Native Messaging host installation helpers."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
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

    def test_known_hosts_requires_exact_server_entry(self) -> None:
        valid = f"# comment\n[{self.server_host}]:22 ssh-ed25519 AAAA\n".encode()
        install._validate_known_hosts_entry(
            valid, server_host=self.server_host, server_port=22
        )
        with self.assertRaises(install.InstallError):
            install._validate_known_hosts_entry(
                b"other.example ssh-ed25519 AAAA\n",
                server_host=self.server_host,
                server_port=22,
            )
        with self.assertRaises(install.InstallError):
            install._validate_known_hosts_entry(
                f"{self.server_host} ssh-rsa AAAA\n".encode(),
                server_host=self.server_host,
                server_port=22,
            )

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

        with mock.patch.object(install.subprocess, "run") as run:
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
        run.assert_not_called()
        self.assertTrue(paths["known_hosts_ready"])
        self.assertEqual(stat.S_IMODE(paths["config_path"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["manifest_path"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["host_path"].stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(paths["launcher_path"].stat().st_mode), 0o700)
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
        self.assertIn(str(Path(os.sys.executable).resolve()), launcher)
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
        unrelated = self.app_dir / "keep.txt"
        unrelated.write_text("keep", encoding="ascii")

        removed = uninstall.uninstall(
            app_support_dir=self.app_dir,
            edge_manifest_dir=self.edge_dir,
            allowed_root=self.root,
        )
        self.assertEqual(len(removed), 7)
        self.assertTrue(unrelated.exists())
        self.assertFalse(manifest.exists())
        self.assertFalse((self.app_dir / "native_host.py").exists())
        self.assertFalse((self.app_dir / "native_host").exists())

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

        args = install._parse_args(
            ["--extension-id", self.extension_id, "--server-host", self.server_host]
        )
        self.assertEqual(args.server_host, self.server_host)
        self.assertIsNone(args.identity_file)

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

    def test_reinstall_preserves_existing_server_legacy_identity_and_removes_proxy(self) -> None:
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

        with mock.patch.object(install.subprocess, "run") as run:
            paths = install.install(
                app_support_dir=self.app_dir,
                edge_manifest_dir=self.edge_dir,
                allowed_root=self.root,
                no_generate_key=True,
            )
        run.assert_not_called()
        self.assertEqual(paths["identity_file"], legacy_key)
        self.assertEqual(paths["known_hosts_file"], legacy_known_hosts)
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(config["server"], {"host": self.server_host, "port": 2222, "user": "rsshub-sync"})
        self.assertEqual(config["ssh"]["identity_file"], str(legacy_key))
        self.assertNotIn("proxy", config)

    def test_identity_file_must_be_direct_child_of_ssh_directory(self) -> None:
        with self.assertRaises(install.InstallError):
            install._identity_path(self.root / "outside-key", home=self.root)
        with self.assertRaises(install.InstallError):
            install._identity_path(self.root / ".ssh" / "nested" / "key", home=self.root)

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
