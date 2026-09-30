"""Tests for discovering and refreshing the local RSSHub access key.

These tests use temporary configuration files plus injected Docker and HTTP
boundaries.  They never invoke a real Docker daemon or make network requests.
"""

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import rsshub_cookie_sync as sync  # noqa: E402
import test_server  # noqa: E402


ACCESS_KEY = "synthetic-rsshub-access-key-never-print"


class ComposeRunner:
    def __init__(self, document=None, *, returncode=0, stderr="", exception=None):
        self.document = document
        self.returncode = returncode
        self.stderr = stderr
        self.exception = exception
        self.calls = []

    def run(self, args, timeout, capture=False, output_limit=None):
        self.calls.append((list(args), timeout, capture, output_limit))
        if self.exception is not None:
            raise self.exception
        stdout = json.dumps(self.document) if self.document is not None else ""
        return SimpleNamespace(returncode=self.returncode, stdout=stdout, stderr=self.stderr)


class HealthTransport:
    def __init__(self, status=200, exception=None):
        self.status = status
        self.exception = exception
        self.requests = []

    def request(self, url, method="GET", headers=None, body=None, timeout=20):
        self.requests.append((url, method, dict(headers or {}), body, timeout))
        if self.exception is not None:
            raise self.exception
        return sync.HTTPResponse(self.status, b"ok" if self.status == 200 else b"unavailable")


class RecoveryRunner(test_server.FakeDocker):
    def run(self, args, timeout, capture=False, output_limit=None):
        if self.healthy is False and "inspect" in args and "Health" in " ".join(args):
            self.calls.append(("inspect-unhealthy",))
            return SimpleNamespace(returncode=0, stdout="unhealthy\n", stderr="")
        return super().run(args, timeout, capture=capture)


def compose_document(service="rsshub", environment=None):
    return {
        "services": {
            service: {
                "image": "diygod/rsshub:latest",
                "environment": environment,
            }
        }
    }


class RSSHubConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config.json"
        self.compose_file = self.root / "docker-compose.yml"
        self.compose_file.write_text("services: {}\n", encoding="utf-8")
        self.live_env = self.root / "secrets" / "rsshub.env"
        self.candidate_dir = self.root / "secrets" / "candidates"
        self.state_file = self.root / "state.json"
        self.lock_file = self.root / "sync.lock"

    def tearDown(self):
        self.temp.cleanup()

    def write_config(self, *, access_key=ACCESS_KEY, base_url="http://127.0.0.1:1200"):
        document = {
            "deployment": {
                "compose_file": str(self.compose_file),
                "live_env": str(self.live_env),
                "candidate_dir": str(self.candidate_dir),
                "state_file": str(self.state_file),
                "lock_file": str(self.lock_file),
                "project": "rsshub",
                "service": "rsshub",
            },
            "rsshub": {
                "base_url": base_url,
                "health_path": "/healthz",
                "access_key": access_key,
                "custom_setting": "preserve-me",
            },
            "bark": {"base_url": "https://api.day.app", "device_key": "synthetic-bark-key"},
            "custom": {"nested": [1, "preserve-me"]},
        }
        sync.atomic_write(
            self.config_path,
            (json.dumps(document, sort_keys=True) + "\n").encode("utf-8"),
            mode=0o600,
        )
        return document

    def read_config(self):
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def test_discover_reads_the_selected_compose_service_and_bounds_output(self):
        self.write_config()
        config = sync.RuntimeConfig.from_file(self.config_path, require_file=True, require_deployment=True)
        runner = ComposeRunner(compose_document(environment={"ACCESS_KEY": ACCESS_KEY}))

        found = sync.discover_rsshub_access_key(config, runner=runner)

        self.assertEqual(found, ACCESS_KEY)
        self.assertEqual(len(runner.calls), 1)
        args, timeout, capture, output_limit = runner.calls[0]
        self.assertEqual(args[-3:], ["config", "--format", "json"])
        self.assertEqual(timeout, 30)
        self.assertTrue(capture)
        self.assertEqual(output_limit, 2 * 1024 * 1024)

    def test_discover_treats_absent_null_and_empty_key_as_unconfigured(self):
        self.write_config()
        config = sync.RuntimeConfig.from_file(self.config_path, require_file=True, require_deployment=True)
        for environment in ({}, {"ACCESS_KEY": None}, {"ACCESS_KEY": ""}):
            with self.subTest(environment=environment):
                self.assertIsNone(
                    sync.discover_rsshub_access_key(
                        config,
                        runner=ComposeRunner(compose_document(environment=environment)),
                    )
                )

    def test_discover_rejects_malformed_or_unsafe_compose_output_without_leaking_key(self):
        self.write_config()
        config = sync.RuntimeConfig.from_file(self.config_path, require_file=True, require_deployment=True)
        cases = (
            ComposeRunner(returncode=1, stderr="failed with " + ACCESS_KEY),
            ComposeRunner(exception=RuntimeError("failed with " + ACCESS_KEY)),
            ComposeRunner(document={"services": {}}),
            ComposeRunner(document=compose_document(environment=["ACCESS_KEY=" + ACCESS_KEY])),
            ComposeRunner(document=compose_document(environment={"ACCESS_KEY": 12})),
            ComposeRunner(document=compose_document(environment={"ACCESS_KEY": "bad\nkey"})),
        )
        for runner in cases:
            with self.subTest(runner=runner):
                with self.assertRaises(sync.SyncError) as raised:
                    sync.discover_rsshub_access_key(config, runner=runner)
                self.assertNotIn(ACCESS_KEY, str(raised.exception))

    def test_refresh_validates_and_atomically_saves_rotated_key_and_optional_url(self):
        original = self.write_config()
        runner = ComposeRunner(compose_document(environment={"ACCESS_KEY": "rotated-key"}))
        transport = HealthTransport()

        result = sync.refresh_rsshub_config(
            self.config_path,
            base_url="http://127.0.0.1:1300",
            runner=runner,
            transport=transport,
        )

        stored = self.read_config()
        self.assertTrue(result["configured"])
        self.assertTrue(result["changed"])
        self.assertTrue(result["access_key_configured"])
        self.assertEqual(result["health_status"], 200)
        self.assertFalse(result["dry_run"])
        self.assertEqual(stored["rsshub"]["access_key"], "rotated-key")
        self.assertEqual(stored["rsshub"]["base_url"], "http://127.0.0.1:1300")
        self.assertEqual(stored["rsshub"]["health_path"], original["rsshub"]["health_path"])
        self.assertEqual(stored["rsshub"]["custom_setting"], "preserve-me")
        self.assertEqual(stored["bark"], original["bark"])
        self.assertEqual(stored["custom"], original["custom"])
        self.assertEqual(self.config_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(
            parse_qs(urlsplit(transport.requests[0][0]).query),
            {"key": ["rotated-key"]},
        )

    def test_refresh_can_revoke_key_when_compose_no_longer_defines_it(self):
        self.write_config()
        runner = ComposeRunner(compose_document(environment={"OTHER": "value"}))
        transport = HealthTransport()

        result = sync.refresh_rsshub_config(
            self.config_path,
            runner=runner,
            transport=transport,
        )

        self.assertTrue(result["changed"])
        self.assertFalse(result["access_key_configured"])
        self.assertIsNone(self.read_config()["rsshub"]["access_key"])
        self.assertEqual(urlsplit(transport.requests[0][0]).query, "")

    def test_refresh_dry_run_validates_but_does_not_write(self):
        self.write_config()
        before = self.config_path.read_bytes()
        runner = ComposeRunner(compose_document(environment={"ACCESS_KEY": "new-key"}))
        transport = HealthTransport()

        result = sync.refresh_rsshub_config(
            self.config_path,
            dry_run=True,
            runner=runner,
            transport=transport,
        )

        self.assertTrue(result["dry_run"])
        self.assertTrue(result["changed"])
        self.assertEqual(self.config_path.read_bytes(), before)
        self.assertEqual(parse_qs(urlsplit(transport.requests[0][0]).query), {"key": ["new-key"]})

    def test_refresh_does_not_overwrite_config_on_discovery_or_health_failure(self):
        self.write_config()
        before = self.config_path.read_bytes()
        failed_discovery = ComposeRunner(returncode=1, stderr="private " + ACCESS_KEY)
        with self.assertRaises(sync.SyncError) as discovery_error:
            sync.refresh_rsshub_config(
                self.config_path,
                runner=failed_discovery,
                transport=HealthTransport(),
            )
        self.assertNotIn(ACCESS_KEY, str(discovery_error.exception))
        self.assertEqual(self.config_path.read_bytes(), before)

        failed_health = HealthTransport(status=403)
        with self.assertRaises(sync.SyncError) as health_error:
            sync.refresh_rsshub_config(
                self.config_path,
                runner=ComposeRunner(compose_document(environment={"ACCESS_KEY": ACCESS_KEY})),
                transport=failed_health,
            )
        self.assertNotIn(ACCESS_KEY, str(health_error.exception))
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_refresh_command_dispatches_flags_and_emits_safe_json(self):
        self.write_config()
        safe_result = {
            "configured": True,
            "changed": True,
            "access_key_configured": True,
            "health_status": 200,
            "dry_run": True,
        }
        output = io.StringIO()
        with patch("rsshub_cookie_sync.refresh_rsshub_config", return_value=safe_result) as refresh:
            with patch("sys.stdout", output):
                exit_code = sync.main(
                    [
                        "refresh-rsshub-config",
                        "--config",
                        str(self.config_path),
                        "--dry-run",
                        "--rsshub-base-url",
                        "http://127.0.0.1:1300",
                    ]
                )

        self.assertEqual(exit_code, 0)
        refresh.assert_called_once_with(
            self.config_path,
            base_url="http://127.0.0.1:1300",
            dry_run=True,
        )
        self.assertEqual(json.loads(output.getvalue()), safe_result)
        self.assertNotIn(ACCESS_KEY, output.getvalue())

    def test_dry_run_is_rejected_for_commands_that_can_write(self):
        errors = io.StringIO()
        with patch("sys.stderr", errors), patch(
            "rsshub_cookie_sync.make_config",
            side_effect=AssertionError("must reject before loading a write-capable command"),
        ) as make_config:
            exit_code = sync.main(["apply", "--dry-run"])

        self.assertEqual(exit_code, 1)
        make_config.assert_not_called()
        self.assertIn("--dry-run is only supported by refresh-rsshub-config", errors.getvalue())

    def test_first_deployment_can_discover_key_but_routine_configure_preserves_it(self):
        kwargs = {
            "compose_file": self.compose_file,
            "live_env": self.live_env,
            "candidate_dir": self.candidate_dir,
            "state_file": self.state_file,
            "lock_file": self.lock_file,
            "project": "rsshub",
            "service": "rsshub",
            "rsshub_base_url": "http://127.0.0.1:1200",
        }
        discovered_runner = ComposeRunner(compose_document(environment={"ACCESS_KEY": "install-key"}))

        first = sync.configure_deployment(
            self.config_path,
            detect_access_key=True,
            runner=discovered_runner,
            **kwargs,
        )

        self.assertTrue(first["configured"])
        self.assertEqual(self.read_config()["rsshub"]["access_key"], "install-key")
        self.assertEqual(len(discovered_runner.calls), 1)

        routine_runner = ComposeRunner(exception=AssertionError("routine configure must not inspect Compose"))
        sync.configure_deployment(self.config_path, runner=routine_runner, **kwargs)
        self.assertEqual(routine_runner.calls, [])
        self.assertEqual(self.read_config()["rsshub"]["access_key"], "install-key")


class TransactionRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.compose = self.root / "docker-compose.yml"
        self.compose.write_text("services: {}\n", encoding="utf-8")
        self.live = self.root / "secrets" / "rsshub.env"
        self.live.parent.mkdir(parents=True)
        self.original_env = b"ZHIHU_COOKIES=z_c0=old\n"
        sync.atomic_write(self.live, b"ZHIHU_COOKIES=z_c0=new\n")
        self.prev = self.live.with_name(self.live.name + ".prev")
        sync.atomic_write(self.prev, self.original_env)
        self.transaction = self.live.with_name(self.live.name + ".txn.json")
        sync.atomic_write(
            self.transaction,
            json.dumps({"version": 1, "phase": "rolling_back", "providers": ["zhihu"]}).encode(),
        )
        self.config = sync.RuntimeConfig(
            compose_file=self.compose,
            live_env=self.live,
            candidate_dir=self.root / "secrets" / "candidates",
            state_file=self.root / "state.json",
            lock_file=self.root / "sync.lock",
            config_file=self.root / "config.json",
            sync_mode="direct",
            health_timeout=1,
            health_poll_seconds=0,
        )
        self.runner = RecoveryRunner()
        self.service = sync.SyncService(self.config, transport=HealthTransport(), runner=self.runner)

    def tearDown(self):
        self.temp.cleanup()

    def test_recovery_failure_preserves_marker_and_backup_for_retry(self):
        cases = ("config", "recreate", "healthy")
        for failed_step in cases:
            with self.subTest(failed_step=failed_step):
                # Recreate the pending recovery state for each simulated failure.
                sync.atomic_write(self.live, b"ZHIHU_COOKIES=z_c0=new\n")
                sync.atomic_write(self.prev, self.original_env)
                sync.atomic_write(
                    self.transaction,
                    json.dumps({"version": 1, "phase": "rolling_back", "providers": ["zhihu"]}).encode(),
                )
                self.runner.config_ok = failed_step != "config"
                self.runner.recreate_ok = failed_step != "recreate"
                self.runner.healthy = failed_step != "healthy"
                backup_before = self.prev.read_bytes()
                marker_before = self.transaction.read_bytes()

                with self.assertRaises(sync.TransactionError):
                    self.service.recover_transaction()

                self.assertEqual(self.live.read_bytes(), self.original_env)
                self.assertEqual(self.prev.read_bytes(), backup_before)
                self.assertEqual(self.transaction.read_bytes(), marker_before)
                self.assertTrue(self.prev.exists())
                self.assertTrue(self.transaction.exists())


if __name__ == "__main__":
    unittest.main()
