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
        self.proxy_host = "proxy.example.test"

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
        ssh_dir = self.app_dir / "ssh"
        ssh_dir.mkdir(parents=True)
        identity = ssh_dir / "id_ed25519"
        identity.write_text("private", encoding="ascii")
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
                no_generate_key=True,
                allowed_root=self.root,
            )
        run.assert_not_called()
        self.assertTrue(paths["known_hosts_ready"])
        self.assertEqual(stat.S_IMODE(paths["config_path"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["manifest_path"].stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(paths["host_path"].stat().st_mode), 0o700)
        installed_host = paths["host_path"].read_text(encoding="utf-8")
        self.assertTrue(installed_host.startswith("#!/usr/bin/env python3\n\"\"\"Microsoft Edge Native Messaging host"))
        self.assertIn("def run_host(", installed_host)
        self.assertNotIn("def install(", installed_host)
        config = json.loads(paths["config_path"].read_text(encoding="utf-8"))
        self.assertEqual(config["host_name"], "com.jayden.rsshub_cookie_sync")
        self.assertEqual(config["server"]["host"], self.server_host)
        self.assertIsNone(config["proxy"])
        manifest = json.loads(paths["manifest_path"].read_text(encoding="utf-8"))
        self.assertEqual(manifest["allowed_origins"], [f"chrome-extension://{self.extension_id}/"])

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
        self.assertEqual(stat.S_IMODE((self.app_dir / "ssh" / "id_ed25519").stat().st_mode), 0o600)

    def test_uninstall_only_removes_managed_files(self) -> None:
        ssh_dir = self.app_dir / "ssh"
        ssh_dir.mkdir(parents=True)
        for name in ("id_ed25519", "id_ed25519.pub", "known_hosts"):
            (ssh_dir / name).write_text("x", encoding="ascii")
        (self.app_dir / "native_host.py").write_text("x", encoding="ascii")
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
        self.assertEqual(len(removed), 6)
        self.assertTrue(unrelated.exists())
        self.assertFalse(manifest.exists())
        self.assertFalse((self.app_dir / "native_host.py").exists())

    def test_proxy_is_opt_in_and_requires_both_endpoint_options(self) -> None:
        config = install.build_config(
            identity_file=self.app_dir / "ssh" / "id_ed25519",
            known_hosts_file=self.app_dir / "ssh" / "known_hosts",
            server_host=self.server_host,
        )
        self.assertIsNone(config["proxy"])

        proxied = install.build_config(
            identity_file=self.app_dir / "ssh" / "id_ed25519",
            known_hosts_file=self.app_dir / "ssh" / "known_hosts",
            server_host=self.server_host,
            proxy_host=self.proxy_host,
            proxy_port=6153,
        )
        self.assertEqual(
            proxied["proxy"],
            {"type": "socks5", "host": self.proxy_host, "port": 6153},
        )

        for kwargs in (
            {"proxy_host": self.proxy_host},
            {"proxy_port": 6153},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(install.InstallError):
                install.build_config(
                    identity_file=self.app_dir / "ssh" / "id_ed25519",
                    known_hosts_file=self.app_dir / "ssh" / "known_hosts",
                    server_host=self.server_host,
                    **kwargs,
                )

    def test_server_host_is_required_by_cli(self) -> None:
        with self.assertRaises(SystemExit):
            install._parse_args(["--extension-id", self.extension_id])
        args = install._parse_args(
            ["--extension-id", self.extension_id, "--server-host", self.server_host]
        )
        self.assertEqual(args.server_host, self.server_host)
        self.assertIsNone(args.proxy_host)
        self.assertIsNone(args.proxy_port)

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
