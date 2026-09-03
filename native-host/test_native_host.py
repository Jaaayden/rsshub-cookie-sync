#!/usr/bin/env python3
"""Unit tests for the Native Messaging host.

All SSH calls are mocked.  Running this file never installs anything and never
opens a network connection.
"""

from __future__ import annotations

import io
import json
import os
import stat
import struct
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

import native_host


class NativeHostTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        self.server_host = "rsshub.example.test"
        self.proxy_host = "proxy.example.test"
        self.identity = root / "ssh" / "id_ed25519"
        self.known_hosts = root / "ssh" / "known_hosts"
        self.identity.parent.mkdir(mode=0o700)
        self.identity.write_text("test private key", encoding="ascii")
        self.known_hosts.write_text(
            f"[{self.server_host}]:22 ssh-ed25519 AAAA\n", encoding="ascii"
        )
        os.chmod(self.identity, 0o600)
        os.chmod(self.known_hosts, 0o600)
        self.config = native_host.HostConfig(
            server_host=self.server_host,
            identity_file=self.identity,
            known_hosts_file=self.known_hosts,
        )
        self.proxied_config = native_host.HostConfig(
            server_host=self.server_host,
            proxy_host=self.proxy_host,
            proxy_port=6153,
            identity_file=self.identity,
            known_hosts_file=self.known_hosts,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    @staticmethod
    def request(**providers: object) -> bytes:
        return json.dumps(
            {"version": 1, "providers": providers}, separators=(",", ":")
        ).encode("utf-8")

    def test_native_frame_is_little_endian_and_supports_partial_reads(self) -> None:
        payload = b'{"version":1}'
        encoded = native_host.encode_frame(payload)

        class Partial(io.BytesIO):
            def read(self, size: int = -1) -> bytes:
                return super().read(min(size, 1))

        self.assertEqual(native_host.read_frame(Partial(encoded)), payload)
        self.assertEqual(encoded[:4], struct.pack("<I", len(payload)))

    def test_browser_extension_origin_argument_is_accepted_and_bounded(self) -> None:
        origin = "chrome-extension://ohpnejcdmchhchkamammonikfbmfpiam/"
        args = native_host._parse_args([origin])
        self.assertEqual(args.origin, origin)
        self.assertEqual(args.config, native_host.DEFAULT_CONFIG_PATH)

        for argv in (
            ["https://www.zhihu.com/"],
            ["chrome-extension://not-an-extension/"],
            [origin, "unexpected-extra-argument"],
        ):
            with self.subTest(argv=argv), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    native_host._parse_args(argv)

    def test_frame_rejects_truncation_and_oversize(self) -> None:
        with self.assertRaises(native_host.ProtocolError):
            native_host.read_frame(io.BytesIO(b"\x01\x00"))
        with self.assertRaises(native_host.ProtocolError):
            native_host.read_frame(io.BytesIO(struct.pack("<I", native_host.MAX_FRAME_BYTES + 1)))
        with self.assertRaises(native_host.ProtocolError):
            native_host.read_frame(io.BytesIO(struct.pack("<I", 0)))

    def test_request_requires_version_and_provider_objects(self) -> None:
        valid = {"cookieHeader": "z_c0=abc; foo=bar=baz"}
        self.assertEqual(
            native_host.validate_request(self.request(zhihu=valid)),
            {"zhihu": valid},
        )
        invalid_messages = (
            {"version": 2, "providers": {"zhihu": valid}},
            {"version": 1.0, "providers": {"zhihu": valid}},
            {"version": True, "providers": {"zhihu": valid}},
            {"version": 1, "providers": {}},
            {"version": 1, "providers": {"other": valid}},
            {"version": 1, "providers": {"zhihu": {"cookieHeader": "a=1", "extra": 1}}},
            {"version": 1, "providers": {"zhihu": "a=1"}},
            {"version": 1, "providers": {"zhihu": valid}, "extra": 1},
        )
        for message in invalid_messages:
            with self.subTest(message=message), self.assertRaises(native_host.ProtocolError):
                native_host.validate_request(json.dumps(message).encode("utf-8"))

    def test_request_rejects_control_characters_and_malformed_cookie_names(self) -> None:
        for header in (
            "a=1\nsecret=2",
            "a=1\r\n",
            "a=1\x00",
            "not-a-cookie",
            "bad name=1",
            "surrogate=\ud800",
        ):
            with self.subTest(header=repr(header)), self.assertRaises(native_host.ProtocolError):
                native_host.validate_request(self.request(zhihu={"cookieHeader": header}))

    def test_duplicate_json_keys_are_rejected(self) -> None:
        duplicate = b'{"version":1,"providers":{"zhihu":{"cookieHeader":"a=1"}},"version":1}'
        with self.assertRaises(native_host.ProtocolError):
            native_host.validate_request(duplicate)

    def test_ssh_argv_is_explicit_and_contains_no_cookie(self) -> None:
        argv = native_host.build_ssh_argv(self.proxied_config)
        self.assertEqual(argv[0], "/usr/bin/ssh")
        self.assertIn("-T", argv)
        self.assertIn("-p", argv)
        self.assertIn("22", argv)
        self.assertIn("-o", argv)
        self.assertIn("StrictHostKeyChecking=yes", argv)
        self.assertIn("GlobalKnownHostsFile=/dev/null", argv)
        self.assertIn("UpdateHostKeys=no", argv)
        self.assertIn("HostKeyAlgorithms=ssh-ed25519", argv)
        self.assertIn("ControlMaster=no", argv)
        self.assertIn("ControlPath=none", argv)
        self.assertTrue(any(item.startswith(f"ProxyCommand=/usr/bin/nc -x {self.proxy_host}:6153 -X 5") for item in argv))
        self.assertEqual(argv[-1], f"rsshub-sync@{self.server_host}")
        self.assertNotIn("z_c0=super-secret", argv)

        direct = native_host.build_ssh_argv(self.config, use_proxy=False)
        self.assertFalse(any(item.startswith("ProxyCommand=") for item in direct))
        self.assertEqual(direct[-1], f"rsshub-sync@{self.server_host}")

    def test_ssh_argv_without_proxy_is_direct_even_with_default_use_proxy(self) -> None:
        argv = native_host.build_ssh_argv(self.config)
        self.assertFalse(any(item.startswith("ProxyCommand=") for item in argv))
        self.assertEqual(argv[-1], f"rsshub-sync@{self.server_host}")

    def test_ssh_file_paths_are_quoted_for_openssh_config_parser(self) -> None:
        root = Path(self.temp_dir.name) / 'Application Support' / 'RSSHub Cookie Sync'
        config = native_host.HostConfig(
            server_host=self.server_host,
            identity_file=root / 'ssh' / 'id_ed25519',
            known_hosts_file=root / 'ssh' / 'known_hosts',
        )
        argv = native_host.build_ssh_argv(config)

        self.assertIn(
            f'IdentityFile="{config.identity_file}"',
            argv,
        )
        self.assertIn(
            f'UserKnownHostsFile="{config.known_hosts_file}"',
            argv,
        )

    def test_cookie_is_sent_only_as_stdin_with_minimal_environment(self) -> None:
        secret = "z_c0=super-secret; foo=bar=baz"
        fake_runner = mock.Mock(
            return_value=CompletedProcess(
                args=[],
                returncode=0,
                stdout=b'{"status":"candidate_saved"}',
                stderr=b"z_c0=super-secret",
            )
        )
        status = native_host.send_to_server(
            {"zhihu": {"cookieHeader": secret}}, self.config, runner=fake_runner
        )
        self.assertEqual(status, "candidate_saved")
        call = fake_runner.call_args
        self.assertNotIn(secret, call.args[0])
        self.assertIn(secret.encode("utf-8"), call.kwargs["input"])
        self.assertEqual(
            call.kwargs["env"],
            {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "LC_ALL": "C"},
        )
        self.assertFalse(call.kwargs["shell"])
        # Remote diagnostics are parsed down to the status allow-list.
        self.assertNotIn("super-secret", status)

    def test_apply_timeout_covers_remote_transaction_and_has_a_fixed_upper_bound(self) -> None:
        fake_runner = mock.Mock(
            return_value=CompletedProcess(
                args=[], returncode=0, stdout=b'{"status":"unchanged"}', stderr=b""
            )
        )
        for connect_timeout in (1, native_host.DEFAULT_CONNECT_TIMEOUT, native_host.MAX_CONNECT_TIMEOUT):
            with self.subTest(connect_timeout=connect_timeout):
                config = native_host.HostConfig(
                    server_host=self.server_host,
                    identity_file=self.identity,
                    known_hosts_file=self.known_hosts,
                    connect_timeout=connect_timeout,
                )
                status = native_host.send_to_server(
                    {"zhihu": {"cookieHeader": "a=1"}}, config, runner=fake_runner
                )
                self.assertEqual(status, "unchanged")
                timeout = fake_runner.call_args.kwargs["timeout"]
                self.assertEqual(
                    timeout,
                    native_host.DEFAULT_APPLY_TIMEOUT + connect_timeout,
                )
                self.assertGreaterEqual(timeout, 15 * 60)
                self.assertLessEqual(timeout, native_host.MAX_SSH_SUBPROCESS_TIMEOUT)

        self.assertEqual(
            native_host.MAX_SSH_SUBPROCESS_TIMEOUT,
            native_host.DEFAULT_APPLY_TIMEOUT + native_host.MAX_CONNECT_TIMEOUT,
        )

    def test_ssh_failure_and_unknown_status_are_retryable(self) -> None:
        failed = mock.Mock(
            return_value=CompletedProcess(args=[], returncode=255, stdout=b"", stderr=b"secret")
        )
        self.assertEqual(
            native_host.send_to_server(
                {"zhihu": {"cookieHeader": "a=1"}}, self.config, runner=failed
            ),
            "retryable_error",
        )
        unknown = mock.Mock(
            return_value=CompletedProcess(args=[], returncode=0, stdout=b'{"status":"oops"}', stderr=b"")
        )
        self.assertEqual(
            native_host.send_to_server(
                {"zhihu": {"cookieHeader": "a=1"}}, self.config, runner=unknown
            ),
            "retryable_error",
        )
        extra = mock.Mock(
            return_value=CompletedProcess(
                args=[], returncode=0, stdout=b'{"status":"unchanged","extra":1}', stderr=b""
            )
        )
        self.assertEqual(
            native_host.send_to_server(
                {"zhihu": {"cookieHeader": "a=1"}}, self.config, runner=extra
            ),
            "retryable_error",
        )

    def test_proxy_transport_failure_never_bypasses_explicit_proxy(self) -> None:
        runner = mock.Mock(
            return_value=CompletedProcess(
                args=[], returncode=255, stdout=b"", stderr=b"proxy failed"
            )
        )

        status = native_host.send_to_server(
            {"weibo": {"cookieHeader": "sid=invalid"}}, self.proxied_config, runner=runner
        )

        self.assertEqual(status, "retryable_error")
        runner.assert_called_once()
        argv = runner.call_args.args[0]
        self.assertTrue(any(item.startswith("ProxyCommand=") for item in argv))

    def test_remote_failure_is_not_replayed_directly(self) -> None:
        runner = mock.Mock(
            return_value=CompletedProcess(args=[], returncode=75, stdout=b'{"status":"retryable_error"}', stderr=b"")
        )

        status = native_host.send_to_server(
            {"weibo": {"cookieHeader": "sid=invalid"}}, self.config, runner=runner
        )

        self.assertEqual(status, "retryable_error")
        runner.assert_called_once()

    def test_direct_transport_is_attempted_once_without_proxy(self) -> None:
        runner = mock.Mock(
            return_value=CompletedProcess(
                args=[], returncode=255, stdout=b"", stderr=b"connection failed"
            )
        )

        status = native_host.send_to_server(
            {"zhihu": {"cookieHeader": "a=1"}}, self.config, runner=runner
        )

        self.assertEqual(status, "retryable_error")
        runner.assert_called_once()
        argv = runner.call_args.args[0]
        self.assertFalse(any(item.startswith("ProxyCommand=") for item in argv))

    def test_missing_or_unsafe_key_does_not_invoke_ssh(self) -> None:
        self.identity.chmod(0o644)
        fake_runner = mock.Mock()
        self.assertEqual(
            native_host.send_to_server(
                {"zhihu": {"cookieHeader": "a=1"}}, self.config, runner=fake_runner
            ),
            "retryable_error",
        )
        fake_runner.assert_not_called()

    def test_config_symlink_is_rejected(self) -> None:
        config = Path(self.temp_dir.name) / "config.json"
        config.write_text("{}", encoding="utf-8")
        config.chmod(0o600)
        link = Path(self.temp_dir.name) / "config-link.json"
        link.symlink_to(config)
        with self.assertRaises(native_host.ConfigurationError):
            native_host.load_config(link)

    def test_missing_config_fails_closed_without_invoking_ssh(self) -> None:
        missing = Path(self.temp_dir.name) / "missing-config.json"
        output_stream = io.BytesIO()
        errors = io.StringIO()
        fake_runner = mock.Mock()

        code = native_host.run_host(
            config_path=missing,
            stdin=io.BytesIO(native_host.encode_frame(self.request(zhihu={"cookieHeader": "secret=never-sent"}))),
            stdout=output_stream,
            stderr=errors,
            runner=fake_runner,
        )

        self.assertEqual(code, 0)
        fake_runner.assert_not_called()
        output_stream.seek(0)
        self.assertEqual(native_host.read_frame(output_stream), b'{"status":"retryable_error"}')
        self.assertEqual(errors.getvalue(), "rsshub-cookie-sync: configuration\n")

    def test_run_host_handles_multiple_frames_and_returns_only_framed_statuses(self) -> None:
        payload = self.request(zhihu={"cookieHeader": "a=1"})
        input_stream = io.BytesIO(native_host.encode_frame(payload) * 2)
        output_stream = io.BytesIO()
        config_path = Path(self.temp_dir.name) / "config.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "server": {"host": self.server_host, "port": 22, "user": "rsshub-sync"},
                    "proxy": None,
                    "ssh": {
                        "identity_file": str(self.identity),
                        "known_hosts_file": str(self.known_hosts),
                    },
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        fake_runner = mock.Mock(
            return_value=CompletedProcess(args=[], returncode=0, stdout=b'{"status":"unchanged"}', stderr=b"")
        )
        code = native_host.run_host(
            config_path=config_path,
            stdin=input_stream,
            stdout=output_stream,
            stderr=io.StringIO(),
            runner=fake_runner,
        )
        self.assertEqual(code, 0)
        output_stream.seek(0)
        self.assertEqual(native_host.read_frame(output_stream), b'{"status":"unchanged"}')
        self.assertEqual(native_host.read_frame(output_stream), b'{"status":"unchanged"}')
        self.assertIsNone(native_host.read_frame(output_stream))
        self.assertEqual(fake_runner.call_count, 2)

    def test_config_loader_requires_server_host_and_accepts_null_proxy(self) -> None:
        config = native_host.config_from_mapping(
            {
                "schema_version": 1,
                "server": {"host": self.server_host},
                "proxy": None,
                "ssh": {
                    "identity_file": str(self.identity),
                    "known_hosts_file": str(self.known_hosts),
                },
            },
        )
        self.assertEqual(config.server_host, self.server_host)
        self.assertIsNone(config.proxy_host)
        self.assertIsNone(config.proxy_port)

        with self.assertRaises(native_host.ConfigurationError):
            native_host.config_from_mapping(
                {
                    "schema_version": 1,
                    "server": {},
                    "proxy": None,
                }
            )

    def test_invalid_frame_returns_rejected_status_without_secret_diagnostic(self) -> None:
        output_stream = io.BytesIO()
        errors = io.StringIO()
        # This test targets frame diagnostics, so it must not depend on whether
        # the developer machine happens to have an installed Host config.
        with mock.patch.object(native_host, "load_config", return_value=self.config):
            code = native_host.run_host(
                stdin=io.BytesIO(b"\x03\x00"),
                stdout=output_stream,
                stderr=errors,
            )
        self.assertEqual(code, 2)
        output_stream.seek(0)
        self.assertEqual(native_host.read_frame(output_stream), b'{"status":"rejected_invalid"}')
        self.assertEqual(errors.getvalue(), "rsshub-cookie-sync: invalid_frame\n")


if __name__ == "__main__":
    unittest.main()
