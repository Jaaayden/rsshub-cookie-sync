import io
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch
from urllib.parse import urlsplit
from urllib.request import ProxyHandler

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import rsshub_cookie_sync  # noqa: E402

from rsshub_cookie_sync import (  # noqa: E402
    BarkNotifier,
    CommandRunner,
    DockerCompose,
    HTTPResponse,
    InvalidInput,
    ProbeResult,
    ProviderProber,
    RuntimeConfig,
    SyncError,
    SyncService,
    atomic_write,
    build_manual_update_request,
    build_parser,
    configure_bark_from_stdin,
    configure_deployment,
    finalize_migration,
    install_cli,
    load_state,
    make_config,
    migrate_compose_file,
    rollback_migration,
    save_state,
    sha256_prefix,
    strict_json_from_stdin,
    DEFAULT_LOCK_FILE,
    HTTPTransport,
)


ZH_OLD = "z_c0=old; foo=bar"
ZH_NEW = "z_c0=new; foo=bar"
WB_OLD = "SUB=old; SSOLoginState=old"
WB_NEW = "SUB=new; SSOLoginState=new"


class FakeClock:
    def __init__(self):
        self.value = 1_700_000_000

    def now(self):
        return self.value

    def sleep(self, seconds):
        self.value += int(seconds or 1)


class FakeDocker:
    def __init__(self):
        self.calls = []
        self.config_ok = True
        self.recreate_ok = True
        self.healthy = True

    def run(self, args, timeout, capture=False):
        """CommandRunner-compatible fake used by DockerCompose."""
        if "config" in args:
            self.calls.append(("config",))
            return type("Result", (), {"returncode": 0 if self.config_ok else 1, "stdout": "", "stderr": ""})()
        if "up" in args:
            self.calls.append(("recreate",))
            return type("Result", (), {"returncode": 0 if self.recreate_ok else 1, "stdout": "", "stderr": ""})()
        if "ps" in args:
            return type("Result", (), {"returncode": 0, "stdout": "a" * 12 + "\n", "stderr": ""})()
        if "inspect" in args:
            value = "healthy" if "Health" in " ".join(args) else "running"
            return type("Result", (), {"returncode": 0, "stdout": value + "\n", "stderr": ""})()
        return type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()

    def config_quiet(self):
        self.calls.append(("config",))
        return self.config_ok

    def recreate(self):
        self.calls.append(("recreate",))
        return self.recreate_ok

    def wait_healthy(self):
        self.calls.append(("healthy",))
        return self.healthy


class FakeNotifier:
    def __init__(self):
        self.events = []

    def send(self, title, body):
        self.events.append((title, body))
        return True


class FakeTransport:
    """Provider-shaped responses; it records headers for security assertions."""

    def __init__(self):
        self.requests = []
        self.zhihu_old_auth_failures = 0
        self.zhihu_post_fail = False
        self.weibo_old_auth_failures = 0
        self.weibo_auth_failure_limit = 0
        self.weibo_transient = False

    def request(self, url, method="GET", headers=None, body=None, timeout=20):
        headers = dict(headers or {})
        self.requests.append((method, url, headers, body))
        path = urlsplit(url).path
        if method == "POST":
            return HTTPResponse(200, b'{"code":200}')
        if path == "/healthz":
            return HTTPResponse(200, b"ok")
        cookie = headers.get("Cookie", "")
        if path == "/api/v4/me":
            if "z_c0=new" in cookie:
                if self.zhihu_post_fail:
                    return HTTPResponse(401, b"{}")
                return HTTPResponse(200, b'{"name":"tester"}')
            self.zhihu_old_auth_failures += 1
            if self.zhihu_old_auth_failures <= 2:
                return HTTPResponse(401, b"{}")
            return HTTPResponse(200, b'{"name":"tester"}')
        if path == "/api/v3/moments":
            return HTTPResponse(200, b'{"data":[]}')
        if path == "/api/config":
            if self.weibo_transient:
                return HTTPResponse(429, b"{}")
            if (
                self.weibo_auth_failure_limit
                and "SUB=new" not in cookie
            ):
                self.weibo_old_auth_failures += 1
                if self.weibo_old_auth_failures <= self.weibo_auth_failure_limit:
                    return HTTPResponse(401, b"{}")
            return HTTPResponse(200, b'{"ok":1,"data":{"login":true,"uid":"123"}}')
        raise AssertionError("unexpected URL")


class ScriptedProber:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def probe(self, provider, cookie, full=False):
        self.calls.append((provider, cookie, full))
        if self.results:
            return self.results.pop(0)
        return ProbeResult("ok", 200, "ok")


class QueueTransport:
    """Small deterministic transport for provider/proxy boundary tests."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def request(self, url, method="GET", headers=None, body=None, timeout=20):
        self.requests.append((method, url, dict(headers or {}), body))
        if not self.responses:
            raise AssertionError("unexpected extra request")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.root = root
        self.compose = root / "docker-compose.yml"
        self.compose.write_text("services:\n  rsshub:\n    image: diygod/rsshub:latest\n", encoding="utf-8")
        self.live = root / "secrets" / "rsshub.env"
        atomic_write(
            self.live,
            ("ZHIHU_COOKIES=" + ZH_OLD + "\nWEIBO_COOKIES=" + WB_OLD + "\nTWITTER_AUTH_TOKEN=t\n").encode(),
        )
        self.config = RuntimeConfig(
            compose_file=self.compose,
            live_env=self.live,
            candidate_dir=root / "secrets" / "candidates",
            state_file=root / "state" / "state.json",
            lock_file=root / "lock" / "sync.lock",
            config_file=root / "config.json",
            provider_timeout=1,
            health_timeout=1,
            health_poll_seconds=0,
        )
        self.clock = FakeClock()
        self.transport = FakeTransport()
        self.docker = FakeDocker()
        self.notifier = FakeNotifier()
        self.service = SyncService(
            self.config,
            transport=self.transport,
            runner=self.docker,
            notifier=self.notifier,
            clock=self.clock,
        )

    def tearDown(self):
        self.temp.cleanup()

    def apply_payload(self, providers):
        return self.service.apply({"version": 1, "providers": providers})

    def test_default_lock_is_persistent_state_path(self):
        self.assertEqual(DEFAULT_LOCK_FILE, Path("/var/lib/rsshub-cookie-sync/lock"))

    def test_deployment_config_supplies_paths_project_and_service(self):
        config_path = self.root / "etc" / "config.json"
        deployment = {
            "compose_file": str(self.root / "deploy" / "compose.yml"),
            "live_env": str(self.root / "deploy" / "secrets" / "rsshub.env"),
            "candidate_dir": str(self.root / "deploy" / "secrets" / "candidates"),
            "state_file": str(self.root / "var" / "state.json"),
            "lock_file": str(self.root / "var" / "lock"),
            "project": "my-rsshub",
            "service": "reader",
        }
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"deployment": deployment}), encoding="utf-8")
        config_path.chmod(0o600)

        args = build_parser().parse_args(["status", "--config", str(config_path)])
        config = make_config(args)

        self.assertEqual(config.compose_file, Path(deployment["compose_file"]))
        self.assertEqual(config.live_env, Path(deployment["live_env"]))
        self.assertEqual(config.candidate_dir, Path(deployment["candidate_dir"]))
        self.assertEqual(config.state_file, Path(deployment["state_file"]))
        self.assertEqual(config.lock_file, Path(deployment["lock_file"]))
        self.assertEqual(config.project, "my-rsshub")
        self.assertEqual(config.service, "reader")
        config.validate()

        override = build_parser().parse_args(
            [
                "status",
                "--config",
                str(config_path),
                "--project",
                "one-shot",
                "--service",
                "rsshub-alt",
            ]
        )
        overridden = make_config(override)
        self.assertEqual(overridden.project, "one-shot")
        self.assertEqual(overridden.service, "rsshub-alt")
        self.assertEqual(overridden.compose_file, config.compose_file)

    def test_configure_deployment_is_atomic_preserves_secrets_and_requires_explicit_retarget(self):
        config_path = self.root / "etc" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text(
            json.dumps(
                {
                    "bark": {"base_url": "https://api.day.app", "device_key": "test-device-key"},
                    "rsshub": {"access_key": "test-access-key"},
                }
            ),
            encoding="utf-8",
        )
        config_path.chmod(0o600)
        kwargs = {
            "compose_file": self.compose,
            "live_env": self.live,
            "candidate_dir": self.root / "secrets" / "candidates",
            "state_file": self.root / "state" / "state.json",
            "lock_file": self.root / "state" / "lock",
            "project": "my-rsshub",
            "service": "reader",
            "rsshub_base_url": "http://127.0.0.1:1300",
        }

        first = configure_deployment(config_path, **kwargs)
        second = configure_deployment(config_path, **kwargs)
        stored = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertEqual(first, {"configured": True, "changed": True})
        self.assertEqual(second, {"configured": True, "changed": False})
        self.assertEqual(stored["bark"]["device_key"], "test-device-key")
        self.assertEqual(stored["rsshub"]["access_key"], "test-access-key")
        self.assertEqual(stored["rsshub"]["base_url"], "http://127.0.0.1:1300")
        self.assertEqual(stored["deployment"]["service"], "reader")
        self.assertEqual(config_path.stat().st_mode & 0o777, 0o600)

        changed = dict(kwargs)
        changed["project"] = "another-project"
        with self.assertRaises(SyncError):
            configure_deployment(config_path, **changed)
        result = configure_deployment(config_path, replace=True, **changed)
        self.assertEqual(result, {"configured": True, "changed": True})

        changed_url = dict(changed)
        changed_url["rsshub_base_url"] = "http://127.0.0.1:1400"
        with self.assertRaises(SyncError):
            configure_deployment(config_path, **changed_url)
        result = configure_deployment(config_path, replace=True, **changed_url)
        self.assertEqual(result, {"configured": True, "changed": True})

    def test_runtime_config_rejects_relative_deployment_paths(self):
        with self.assertRaises(SyncError):
            RuntimeConfig(
                compose_file=Path("compose.yml"),
                live_env=Path("secrets/rsshub.env"),
            ).validate()

    def test_runtime_cli_config_fails_closed_when_missing_or_world_readable(self):
        missing_args = build_parser().parse_args(
            ["status", "--config", str(self.root / "missing-config.json")]
        )
        with self.assertRaises(SyncError):
            make_config(missing_args)

        unsafe = self.root / "unsafe-config.json"
        unsafe.write_text("{}\n", encoding="utf-8")
        unsafe.chmod(0o644)
        unsafe_args = build_parser().parse_args(["status", "--config", str(unsafe)])
        with self.assertRaises(SyncError):
            make_config(unsafe_args)

        incomplete = self.root / "incomplete-config.json"
        incomplete.write_text('{"bark":{"device_key":null}}\n', encoding="utf-8")
        incomplete.chmod(0o600)
        incomplete_args = build_parser().parse_args(
            ["status", "--config", str(incomplete)]
        )
        with self.assertRaises(SyncError):
            make_config(incomplete_args)

    def test_runtime_config_rejects_non_loopback_or_ambiguous_health_base_url(self):
        for value in (
            "https://127.0.0.1:1200",
            "http://rsshub.example.test:1200",
            "http://127.0.0.1:1200/prefix",
            "http://127.0.0.1:1200?query=1",
            "http://127.0.0.1:99999",
        ):
            with self.subTest(value=value), self.assertRaises(SyncError):
                RuntimeConfig(
                    compose_file=self.compose,
                    live_env=self.live,
                    candidate_dir=self.config.candidate_dir,
                    state_file=self.config.state_file,
                    lock_file=self.config.lock_file,
                    rsshub_base_url=value,
                ).validate()

    def test_runtime_config_rejects_non_finite_or_extreme_timeouts(self):
        for field, value in (
            ("provider_timeout", float("nan")),
            ("provider_timeout", 301),
            ("health_timeout", -1),
            ("health_poll_seconds", 61),
            ("notification_cooldown", 32 * 24 * 60 * 60),
            ("moments_interval", float("inf")),
        ):
            with self.subTest(field=field, value=value), self.assertRaises(SyncError):
                values = {
                    "compose_file": self.compose,
                    "live_env": self.live,
                    "candidate_dir": self.config.candidate_dir,
                    "state_file": self.config.state_file,
                    "lock_file": self.config.lock_file,
                    field: value,
                }
                RuntimeConfig(**values).validate()

    def test_zhihu_ambiguous_success_payload_is_transient_not_auth_failure(self):
        transport = QueueTransport([HTTPResponse(200, b"<html>upstream challenge</html>")])
        result = ProviderProber(self.config, transport).probe("zhihu", ZH_OLD)
        self.assertEqual(result, ProbeResult("transient", 200, "profile_missing"))

    def test_docker_compose_targets_configured_service_and_project(self):
        config = RuntimeConfig(
            compose_file=self.compose,
            live_env=self.live,
            candidate_dir=self.config.candidate_dir,
            state_file=self.config.state_file,
            lock_file=self.config.lock_file,
            project="my-rsshub",
            service="reader",
        )
        docker = DockerCompose(config, runner=self.docker, clock=self.clock)

        self.assertTrue(docker.recreate())
        self.assertTrue(docker._container_id())

        # The fake records operation classes; inspect the arguments by
        # replaying with a tiny runner that retains the command vectors.
        commands = []

        class RecordingRunner:
            def run(self, args, timeout, capture=False):
                commands.append(list(args))
                if "ps" in args:
                    return type("Result", (), {"returncode": 0, "stdout": "a" * 12 + "\n", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        recording = DockerCompose(config, runner=RecordingRunner(), clock=self.clock)
        recording.recreate()
        recording._container_id()
        self.assertEqual(commands[0][-1], "reader")
        self.assertEqual(commands[1][-1], "reader")
        self.assertIn("my-rsshub", commands[0])

    def test_command_runner_uses_a_fixed_minimal_environment(self):
        completed = type(
            "Completed",
            (),
            {"returncode": 0, "stdout": "ok\n", "stderr": ""},
        )()
        with patch("rsshub_cookie_sync.subprocess.run", return_value=completed) as run:
            result = CommandRunner().run(["docker", "compose", "version"], 5, capture=True)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(
            run.call_args.kwargs["env"],
            {
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                "LC_ALL": "C",
            },
        )

    def test_migration_targets_non_default_service_and_env_reference(self):
        compose = self.root / "deploy" / "compose.yaml"
        live = self.root / "deploy" / "secrets" / "rsshub.env"
        compose.parent.mkdir(parents=True)
        compose.write_text(
            "services:\n"
            "  reader:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n",
            encoding="utf-8",
        )

        result = migrate_compose_file(
            compose,
            live,
            service_name="reader",
            managed_env_path="./secrets/rsshub.env",
        )

        self.assertEqual(result["migrated"], ["WEIBO_COOKIES", "ZHIHU_COOKIES"])
        migrated = compose.read_text()
        self.assertIn("  reader:\n", migrated)
        self.assertIn("- path: ./secrets/rsshub.env", migrated)
        self.assertIn("ZHIHU_COOKIES=" + ZH_NEW, live.read_text())

    def test_http_transport_explicitly_disables_environment_proxy(self):
        # build_opener omits the default ProxyHandler when given an explicit
        # empty one; patching the imported constructor verifies that the
        # opt-out cannot regress to environment proxy discovery.
        from urllib.request import build_opener

        with patch("rsshub_cookie_sync.build_opener", wraps=build_opener) as build:
            HTTPTransport()
        proxy = build.call_args.args[0]
        self.assertIsInstance(proxy, ProxyHandler)
        self.assertEqual(proxy.proxies, {})

    def test_migrate_cli_holds_the_same_lock(self):
        lock_path = self.root / "persistent" / "lock"
        cli_config = self.root / "cli-config.json"
        cli_config.write_text(
            json.dumps(
                {
                    "deployment": {
                        "compose_file": str(self.compose),
                        "live_env": str(self.live),
                        "candidate_dir": str(self.config.candidate_dir),
                        "state_file": str(self.config.state_file),
                        "lock_file": str(lock_path),
                        "project": "rsshub",
                        "service": "rsshub",
                    }
                }
            ),
            encoding="utf-8",
        )
        cli_config.chmod(0o600)
        args = build_parser().parse_args(
            [
                "migrate-compose",
                "--config",
                str(cli_config),
                "--compose-file",
                str(self.compose),
                "--live-env",
                str(self.live),
                "--lock-file",
                str(lock_path),
                "--json",
            ]
        )
        entered = []

        @contextmanager
        def recording_lock(path):
            entered.append(path)
            yield

        with patch("rsshub_cookie_sync.file_lock", recording_lock), patch(
            "rsshub_cookie_sync.migrate_compose_file",
            return_value={"migrated": [], "compose_changed": False, "migration_pending": False},
        ):
            install_cli(args)
        self.assertEqual(entered, [lock_path])

    def test_strict_input_rejects_unknown_fields_and_newline(self):
        with self.assertRaises(InvalidInput):
            strict_json_from_stdin(io.BytesIO(b'{"version":1,"providers":{},"x":1}'))
        with self.assertRaises(InvalidInput):
            strict_json_from_stdin(
                io.BytesIO(
                    json.dumps(
                        {"version": 1, "providers": {"zhihu": {"cookieHeader": "a=b\nc=d"}}}
                    ).encode()
                )
            )

        with self.assertRaises(InvalidInput):
            strict_json_from_stdin(
                io.BytesIO(b'{"version":1,"version":1,"providers":{"zhihu":{"cookieHeader":"a=b"}}}')
            )
        with self.assertRaises(InvalidInput):
            strict_json_from_stdin(
                io.BytesIO(b'{"version":true,"providers":{"zhihu":{"cookieHeader":"a=b"}}}')
            )
        with self.assertRaises(InvalidInput):
            strict_json_from_stdin(
                io.BytesIO(b'{"version":1,"providers":{"zhihu":{"cookieHeader":"a=b"}},"n":NaN}')
            )

    def test_configure_bark_accepts_endpoint_or_key_without_echo(self):
        config_path = self.root / "config" / "config.json"
        result = configure_bark_from_stdin(io.BytesIO(b"https://api.day.app/test-device-key/\n"), config_path)
        self.assertEqual(result, {"configured": True})
        content = config_path.read_text()
        self.assertIn('"base_url":"https://api.day.app"', content)
        self.assertIn('"device_key":"test-device-key"', content)
        with self.assertRaises(InvalidInput):
            configure_bark_from_stdin(io.BytesIO(b"https://evil.example/secretkey"), config_path)

    def test_configure_bark_cli_uses_hidden_prompt_on_tty(self):
        config_path = self.root / "interactive" / "config.json"
        output = io.StringIO()
        with (
            patch.object(rsshub_cookie_sync.sys.stdin, "isatty", return_value=True),
            patch.object(
                rsshub_cookie_sync.getpass,
                "getpass",
                return_value="interactive-device-key",
            ) as hidden_prompt,
            patch.object(rsshub_cookie_sync.sys, "stdout", output),
        ):
            exit_code = rsshub_cookie_sync.main(
                ["configure-bark", "--config", str(config_path)]
            )

        self.assertEqual(exit_code, 0)
        hidden_prompt.assert_called_once()
        self.assertNotIn("interactive-device-key", output.getvalue())
        self.assertEqual(
            json.loads(config_path.read_text(encoding="utf-8"))["bark"]["device_key"],
            "interactive-device-key",
        )

    def test_cookie_header_allows_browser_legal_empty_value(self):
        self.transport.zhihu_old_auth_failures = 2
        self.assertEqual(self.service.apply({
            "version": 1,
            "providers": {"zhihu": {"cookieHeader": "empty=; z_c0=new"}},
        })["status"], "candidate_saved")

    def test_manual_update_builds_the_same_bounded_single_provider_request(self):
        self.assertEqual(
            build_manual_update_request("zhihu", ZH_NEW),
            {
                "version": 1,
                "providers": {"zhihu": {"cookieHeader": ZH_NEW}},
            },
        )
        args = build_parser().parse_args(["manual-update", "--provider", "weibo"])
        self.assertEqual(args.provider, "weibo")
        with self.assertRaises(InvalidInput):
            build_manual_update_request("unknown", ZH_NEW)
        with self.assertRaises(InvalidInput):
            build_manual_update_request("zhihu", "bad\nvalue")

    def test_manual_update_cli_uses_hidden_prompt_and_never_prints_cookie(self):
        output = io.StringIO()
        with (
            patch.object(rsshub_cookie_sync.sys.stdin, "isatty", return_value=True),
            patch.object(rsshub_cookie_sync.getpass, "getpass", return_value=ZH_NEW) as hidden_prompt,
            patch.object(rsshub_cookie_sync, "make_config", return_value=self.config),
            patch.object(rsshub_cookie_sync, "SyncService", return_value=self.service),
            patch.object(rsshub_cookie_sync.sys, "stdout", output),
        ):
            exit_code = rsshub_cookie_sync.main(
                ["manual-update", "--provider", "zhihu", "--config", str(self.config.config_file)]
            )

        self.assertEqual(exit_code, 0)
        hidden_prompt.assert_called_once()
        self.assertNotIn(ZH_NEW, output.getvalue())
        self.assertIn(
            json.loads(output.getvalue())["status"],
            {"unchanged", "candidate_saved", "promoted"},
        )

    def test_partial_update_saves_only_valid_candidate(self):
        # Keep the existing live cookie healthy so this test exercises the
        # candidate-only path rather than the immediate-repair path.
        self.transport.zhihu_old_auth_failures = 2
        result = self.apply_payload(
            {
                "zhihu": {"cookieHeader": ZH_NEW},
                "weibo": {"cookieHeader": "not-a-cookie"},
            }
        )
        self.assertEqual(result["status"], "candidate_saved")
        self.assertEqual((self.config.candidate_dir / "zhihu.cookie").read_text(), ZH_NEW)
        self.assertFalse((self.config.candidate_dir / "weibo.cookie").exists())
        self.assertEqual(self.live.read_text().splitlines()[0], "ZHIHU_COOKIES=" + ZH_OLD)

    def test_rejected_upload_does_not_disable_an_existing_valid_candidate(self):
        candidate_path = self.config.candidate_dir / "zhihu.cookie"
        self.service._save_candidate("zhihu", ZH_NEW)
        state = load_state(self.config.state_file)
        item = state["providers"]["zhihu"]
        item["candidate_hash"] = sha256_prefix(ZH_NEW)
        item["candidate_validation"] = "ok"
        save_state(self.config.state_file, state)

        malformed = self.apply_payload({"zhihu": {"cookieHeader": "not-a-cookie"}})
        rejected = self.apply_payload(
            {"zhihu": {"cookieHeader": "z_c0=expired; foo=bar"}}
        )

        self.assertEqual(malformed["status"], "rejected_invalid")
        self.assertEqual(rejected["status"], "rejected_invalid")
        self.assertEqual(candidate_path.read_text(encoding="utf-8"), ZH_NEW)
        self.assertEqual(
            load_state(self.config.state_file)["providers"]["zhihu"]["candidate_validation"],
            "ok",
        )

    def test_monitor_promotes_after_two_auth_failures(self):
        self.service._save_candidate("zhihu", ZH_NEW)
        state = load_state(self.config.state_file)
        state["providers"]["zhihu"]["candidate_hash"] = sha256_prefix(ZH_NEW)
        state["providers"]["zhihu"]["candidate_validation"] = "ok"
        state["providers"]["zhihu"]["auth_failures"] = 1
        state["providers"]["zhihu"]["last_probe"] = "auth_failed"
        save_state(self.config.state_file, state)

        self.service.monitor()
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_NEW)
        self.assertFalse((self.config.candidate_dir / "zhihu.cookie").exists())
        self.assertIn(("recreate",), self.docker.calls)
        self.assertTrue(any("自动更新" in title for title, _ in self.notifier.events))

    def test_monitor_promotes_weibo_after_two_auth_failures(self):
        # Keep Zhihu healthy so this exercises the Weibo threshold and does
        # not depend on the other provider's state machine.
        self.transport.zhihu_old_auth_failures = 2
        self.transport.weibo_auth_failure_limit = 2
        self.service._save_candidate("weibo", WB_NEW)
        state = load_state(self.config.state_file)
        state["providers"]["weibo"]["candidate_hash"] = sha256_prefix(WB_NEW)
        state["providers"]["weibo"]["candidate_validation"] = "ok"
        state["providers"]["weibo"]["candidate_received_at"] = self.clock.now()
        state["providers"]["weibo"]["candidate_validated_at"] = self.clock.now()
        save_state(self.config.state_file, state)

        self.service.monitor()
        self.service.monitor()

        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["WEIBO_COOKIES"], WB_NEW)
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)
        self.assertFalse((self.config.candidate_dir / "weibo.cookie").exists())
        self.assertEqual(self.docker.calls.count(("recreate",)), 1)
        item = load_state(self.config.state_file)["providers"]["weibo"]
        self.assertEqual(item["last_probe"], "ok")
        self.assertEqual(item["auth_failures"], 0)

    def test_provider_state_is_independent_when_zhihu_promotes_and_weibo_is_transient(self):
        # Both providers have verified candidates.  Zhihu reaches its
        # two-failure threshold while Weibo remains temporarily unavailable;
        # only Zhihu may be promoted and the Weibo candidate must stay queued.
        self.transport.zhihu_old_auth_failures = 0
        self.transport.weibo_transient = True
        self.service._save_candidate("zhihu", ZH_NEW)
        self.service._save_candidate("weibo", WB_NEW)
        state = load_state(self.config.state_file)
        for provider, cookie in (("zhihu", ZH_NEW), ("weibo", WB_NEW)):
            item = state["providers"][provider]
            item["candidate_hash"] = sha256_prefix(cookie)
            item["candidate_validation"] = "ok"
            item["candidate_received_at"] = self.clock.now()
            item["candidate_validated_at"] = self.clock.now()
        save_state(self.config.state_file, state)

        self.service.monitor()
        self.service.monitor()

        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_NEW)
        self.assertEqual(values["WEIBO_COOKIES"], WB_OLD)
        self.assertFalse((self.config.candidate_dir / "zhihu.cookie").exists())
        self.assertTrue((self.config.candidate_dir / "weibo.cookie").exists())
        item = load_state(self.config.state_file)["providers"]["weibo"]
        self.assertEqual(item["last_probe"], "transient")
        self.assertEqual(item["transient_failures"], 2)
        self.assertEqual(item["candidate_validation"], "ok")
        self.assertEqual(self.docker.calls.count(("recreate",)), 1)

    def test_candidate_transient_probe_is_retried_and_later_promoted(self):
        self.service._save_candidate("zhihu", ZH_NEW)
        state = load_state(self.config.state_file)
        item = state["providers"]["zhihu"]
        item["candidate_hash"] = sha256_prefix(ZH_NEW)
        item["candidate_validation"] = "ok"
        item["auth_failures"] = 1
        item["last_probe"] = "auth_failed"
        save_state(self.config.state_file, state)
        self.service.prober = ScriptedProber(
            [
                ProbeResult("auth_failed", 401, "http_401"),
                ProbeResult("transient", 429, "http_429"),
                ProbeResult("ok", 200, "ok"),
                ProbeResult("auth_failed", 401, "http_401"),
                ProbeResult("ok", 200, "ok"),
                ProbeResult("ok", 200, "ok"),
                ProbeResult("ok", 200, "ok"),
            ]
        )

        self.service.monitor()
        self.assertIn("ZHIHU_COOKIES=" + ZH_OLD, self.live.read_text(encoding="utf-8"))
        self.assertEqual(
            load_state(self.config.state_file)["providers"]["zhihu"]["candidate_validation"],
            "ok",
        )

        self.service.monitor()
        self.assertIn("ZHIHU_COOKIES=" + ZH_NEW, self.live.read_text(encoding="utf-8"))
        self.assertFalse((self.config.candidate_dir / "zhihu.cookie").exists())

    def test_apply_repairs_invalid_live_cookie_immediately(self):
        result = self.apply_payload({"zhihu": {"cookieHeader": ZH_NEW}})

        self.assertEqual(result["status"], "promoted")
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_NEW)
        self.assertFalse((self.config.candidate_dir / "zhihu.cookie").exists())
        self.assertEqual(self.docker.calls.count(("recreate",)), 1)

    def test_apply_promotes_first_candidate_when_live_env_is_empty(self):
        self.live.unlink()
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n",
            encoding="utf-8",
        )
        migration = migrate_compose_file(self.compose, self.live)
        self.assertEqual(migration["migrated"], [])
        self.assertEqual(self.live.read_bytes(), b"")

        result = self.apply_payload({"zhihu": {"cookieHeader": ZH_NEW}})

        self.assertEqual(result["status"], "promoted")
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_NEW)
        self.assertNotIn("WEIBO_COOKIES", values)
        self.assertEqual(self.docker.calls.count(("recreate",)), 1)

    def test_apply_same_live_cookie_discards_stale_candidate_and_metadata(self):
        candidate_path = self.config.candidate_dir / "zhihu.cookie"
        self.service._save_candidate("zhihu", ZH_NEW)
        state = load_state(self.config.state_file)
        item = state["providers"]["zhihu"]
        item["candidate_hash"] = sha256_prefix(ZH_NEW)
        item["candidate_received_at"] = self.clock.now()
        item["candidate_validated_at"] = self.clock.now()
        item["candidate_validation"] = "ok"
        save_state(self.config.state_file, state)

        # The fake transport treats the existing old cookie as healthy after
        # two prior checks, so this exercises the live-equivalent path.
        self.transport.zhihu_old_auth_failures = 2
        result = self.apply_payload({"zhihu": {"cookieHeader": ZH_OLD}})

        self.assertEqual(result["status"], "unchanged")
        self.assertFalse(candidate_path.exists())
        state = load_state(self.config.state_file)
        item = state["providers"]["zhihu"]
        self.assertIsNone(item["candidate_hash"])
        self.assertIsNone(item["candidate_received_at"])
        self.assertIsNone(item["candidate_validated_at"])
        self.assertIsNone(item["candidate_validation"])
        self.assertEqual(item["live_hash"], sha256_prefix(ZH_OLD))
        self.assertEqual(self.docker.calls.count(("recreate",)), 0)

    def test_same_live_cookie_cleanup_failure_is_retryable_and_quarantines_candidate(self):
        candidate_path = self.config.candidate_dir / "zhihu.cookie"
        self.service._save_candidate("zhihu", ZH_NEW)
        state = load_state(self.config.state_file)
        item = state["providers"]["zhihu"]
        item["candidate_hash"] = sha256_prefix(ZH_NEW)
        item["candidate_received_at"] = self.clock.now()
        item["candidate_validated_at"] = self.clock.now()
        item["candidate_validation"] = "ok"
        save_state(self.config.state_file, state)

        self.transport.zhihu_old_auth_failures = 2
        with patch("rsshub_cookie_sync.secure_remove", side_effect=SyncError("cannot remove candidate")) as scrub:
            result = self.apply_payload({"zhihu": {"cookieHeader": ZH_OLD}})

        self.assertEqual(result["status"], "retryable_error")
        scrub.assert_called_once_with(candidate_path)
        self.assertTrue(candidate_path.exists())
        state = load_state(self.config.state_file)
        item = state["providers"]["zhihu"]
        self.assertEqual(item["candidate_hash"], sha256_prefix(ZH_NEW))
        self.assertEqual(item["candidate_validation"], "retryable_error")
        self.assertEqual(item["live_hash"], sha256_prefix(ZH_OLD))
        self.assertEqual(self.docker.calls.count(("recreate",)), 0)

        # A failed secure cleanup must not let the old candidate rotate into
        # production during a later two-sample live outage.
        self.transport.zhihu_old_auth_failures = 0
        self.service.monitor()
        self.service.monitor()
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)
        self.assertEqual(self.docker.calls.count(("recreate",)), 0)

    def test_monitor_transient_failure_alerts_after_four_without_rotation(self):
        self.service.prober = ScriptedProber(
            [result for _ in range(4) for result in (
                ProbeResult("transient", 429, "http_429"),
                ProbeResult("ok", 200, "ok"),
            )]
        )

        for _ in range(4):
            self.service.monitor()

        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)
        self.assertEqual(self.docker.calls.count(("recreate",)), 0)
        state = load_state(self.config.state_file)
        self.assertEqual(state["providers"]["zhihu"]["transient_failures"], 4)
        self.assertTrue(any("持续异常" in title for title, _ in self.notifier.events))

    def test_monitor_no_candidate_notifies_only_after_threshold(self):
        self.service.monitor()
        self.assertFalse(self.notifier.events)
        self.service.monitor()
        self.assertTrue(any("重新登录" in title for title, _ in self.notifier.events))
        event_count = len(self.notifier.events)
        self.service.monitor()
        self.assertGreater(len(self.notifier.events), event_count)
        self.assertTrue(any("恢复" in title for title, _ in self.notifier.events[event_count:]))

    def test_monitor_notifies_when_rsshub_health_recovers(self):
        state = load_state(self.config.state_file)
        state["compose"]["last_probe"] = "transient"
        state["compose"]["last_error"] = "rsshub_health_network_error"
        save_state(self.config.state_file, state)
        self.service.prober = ScriptedProber(
            [ProbeResult("ok", 200, "ok"), ProbeResult("ok", 200, "ok")]
        )

        self.service.monitor()

        self.assertTrue(any("服务已恢复" in title for title, _ in self.notifier.events))

    def test_post_provider_transient_does_not_rollback_other_update(self):
        self.service.prober = ScriptedProber(
            [
                ProbeResult("ok", 200, "ok"),
                ProbeResult("transient", 429, "http_429"),
            ]
        )
        self.service._promote({"zhihu": ZH_NEW, "weibo": WB_NEW}, load_state(self.config.state_file), "test")
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_NEW)
        self.assertEqual(values["WEIBO_COOKIES"], WB_NEW)
        self.assertEqual(self.docker.calls.count(("recreate",)), 1)

    def test_post_auth_failure_rolls_back(self):
        self.service.prober = ScriptedProber(
            [
                ProbeResult("auth_failed", 401, "http_401"),
            ]
        )
        with self.assertRaises(SyncError):
            self.service._promote({"zhihu": ZH_NEW}, load_state(self.config.state_file), "test")
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)
        self.assertGreaterEqual(self.docker.calls.count(("recreate",)), 1)

    def test_failed_compose_validation_restores_env_for_later_recovery(self):
        self.docker.config_ok = False
        with self.assertRaises(SyncError):
            self.service._promote({"zhihu": ZH_NEW}, load_state(self.config.state_file), "test")

        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)
        self.assertTrue(self.service.prev_env.exists())
        self.assertTrue(self.service.transaction_file.exists())
        self.assertTrue(any("回滚失败" in title for title, _ in self.notifier.events))

        self.docker.config_ok = True
        self.service.recover_transaction()
        self.assertFalse(self.service.prev_env.exists())
        self.assertFalse(self.service.transaction_file.exists())
        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)

    def test_unhealthy_recreate_rolls_back_live_env_and_cleans_transaction(self):
        # The first health check belongs to the new container and fails.  The
        # rollback check then succeeds, so a failed promotion must leave the
        # old live file active without stale transaction artifacts.
        health_checks = iter((False, True))

        def wait_healthy():
            self.docker.calls.append(("healthy",))
            return next(health_checks)

        self.service.docker.wait_healthy = wait_healthy

        with self.assertRaises(SyncError):
            self.service._promote(
                {"zhihu": ZH_NEW},
                load_state(self.config.state_file),
                "unhealthy_recreate",
            )

        values = dict(line.split("=", 1) for line in self.live.read_text().splitlines())
        self.assertEqual(values["ZHIHU_COOKIES"], ZH_OLD)
        self.assertFalse(self.service.prev_env.exists())
        self.assertFalse(self.service.transaction_file.exists())
        self.assertEqual(self.docker.calls.count(("recreate",)), 2)
        self.assertEqual(self.docker.calls.count(("healthy",)), 2)

    def test_recover_transaction_removes_orphan_backup_without_marker(self):
        atomic_write(self.service.prev_env, b"ZHIHU_COOKIES=orphan\n")

        self.service.recover_transaction()

        self.assertFalse(self.service.prev_env.exists())

    def test_provider_probes_reject_logged_out_and_moments_auth_payloads(self):
        weibo_transport = QueueTransport(
            [HTTPResponse(200, b'{"ok":1,"data":{"login":false,"uid":"123"}}')]
        )
        weibo_result = ProviderProber(self.config, weibo_transport).probe("weibo", WB_OLD)
        self.assertEqual(weibo_result, ProbeResult("auth_failed", 200, "config_logged_out"))

        zhihu_transport = QueueTransport(
            [HTTPResponse(200, b'{"name":"tester"}'), HTTPResponse(200, b'{"code":401}')]
        )
        zhihu_result = ProviderProber(self.config, zhihu_transport).probe("zhihu", ZH_OLD, full=True)
        self.assertEqual(zhihu_result, ProbeResult("auth_failed", 200, "moments_unauthorized"))

    def test_weibo_probe_rejects_boolean_ok_and_non_scalar_uids(self):
        payloads = (
            b'{"ok":true,"data":{"login":true,"uid":"123"}}',
            b'{"ok":1,"data":{"login":true,"uid":true}}',
            b'{"ok":1,"data":{"login":true,"uid":["123"]}}',
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                transport = QueueTransport([HTTPResponse(200, payload)])
                result = ProviderProber(self.config, transport).probe("weibo", WB_OLD)
                self.assertEqual(result.kind, "auth_failed")

    def test_bark_posts_device_key_in_json_body_to_fixed_endpoint(self):
        transport = QueueTransport([HTTPResponse(200, b'{"code":200}')])
        config = RuntimeConfig(
            compose_file=self.compose,
            live_env=self.live,
            candidate_dir=self.config.candidate_dir,
            state_file=self.config.state_file,
            lock_file=self.config.lock_file,
            config_file=self.config.config_file,
            bark_base_url="https://api.day.app",
            bark_device_key="test-device-key",
        )
        config.validate()

        self.assertTrue(BarkNotifier(config, transport=transport).send("title", "body"))
        method, url, headers, body = transport.requests[0]
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(method, "POST")
        self.assertEqual(url, "https://api.day.app/push")
        self.assertNotIn("test-device-key", url)
        self.assertNotIn("Cookie", headers)
        self.assertEqual(payload["device_key"], "test-device-key")

    def test_transaction_recovery_restores_previous_env(self):
        old_data = self.live.read_bytes()
        atomic_write(self.live, old_data.replace(ZH_OLD.encode(), ZH_NEW.encode()))
        atomic_write(self.service.prev_env, old_data)
        atomic_write(
            self.service.transaction_file,
            json.dumps({"version": 1, "phase": "recreated", "providers": ["zhihu"]}).encode(),
        )
        # recovery is normally called under file_lock; this direct call tests
        # the transaction state independently.
        self.service.recover_transaction()
        self.assertEqual(self.live.read_bytes(), old_data)
        self.assertFalse(self.service.transaction_file.exists())

    def test_migration_wires_raw_env_and_finalize_scrubs_backups(self):
        original = (
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_OLD + "\n"
            "      WEIBO_COOKIES: " + WB_OLD + "\n"
            "      TWITTER_AUTH_TOKEN: token\n"
        )
        self.compose.write_text(original, encoding="utf-8")
        result = migrate_compose_file(self.compose, self.live)
        self.assertTrue(result["migration_pending"])
        migrated = self.compose.read_text()
        self.assertIn("format: raw", migrated)
        self.assertNotIn("ZHIHU_COOKIES: " + ZH_OLD, migrated)
        self.assertIn("ZHIHU_COOKIES=" + ZH_OLD, self.live.read_text())
        self.assertTrue(self.compose.with_name(self.compose.name + ".pre-cookie-sync").exists())
        finalize_migration(self.compose, self.live)
        self.assertFalse(self.compose.with_name(self.compose.name + ".pre-cookie-sync").exists())
        self.assertFalse(self.live.with_name(self.live.name + ".pre-cookie-sync").exists())

        repeated = migrate_compose_file(self.compose, self.live)
        self.assertTrue(repeated["already_migrated"])
        self.assertFalse(repeated["migration_pending"])
        self.assertFalse(repeated["compose_changed"])

    def test_migration_extracts_quoted_list_and_mapping_secret_entries(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            f"      - \"ZHIHU_COOKIES={ZH_NEW}\"\n"
            f"      - 'WEIBO_COOKIES={WB_NEW}'\n"
            "      - \"TWITTER_AUTH_TOKEN=token-new\"\n",
            encoding="utf-8",
        )

        result = migrate_compose_file(self.compose, self.live)

        self.assertEqual(
            result["migrated"],
            ["TWITTER_AUTH_TOKEN", "WEIBO_COOKIES", "ZHIHU_COOKIES"],
        )
        migrated = self.compose.read_text(encoding="utf-8")
        self.assertNotIn(ZH_NEW, migrated)
        self.assertNotIn(WB_NEW, migrated)
        live = self.live.read_text(encoding="utf-8")
        self.assertIn(f"ZHIHU_COOKIES={ZH_NEW}\n", live)
        self.assertIn(f"WEIBO_COOKIES={WB_NEW}\n", live)

        # Quoted mapping keys are a separate legal YAML representation.
        finalize_migration(self.compose, self.live)
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            f"      \"ZHIHU_COOKIES\": \"{ZH_NEW}\"\n"
            f"      'WEIBO_COOKIES': '{WB_NEW}'\n",
            encoding="utf-8",
        )
        result = migrate_compose_file(self.compose, self.live)
        self.assertEqual(result["migrated"], ["WEIBO_COOKIES", "ZHIHU_COOKIES"])
        self.assertNotIn(ZH_NEW, self.compose.read_text(encoding="utf-8"))

    def test_migration_rejects_unparsed_secret_list_entry_without_changes(self):
        original = (
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      - \"ZHIHU_COOKIES\"\n"
        )
        self.compose.write_text(original, encoding="utf-8")
        old_live = self.live.read_bytes()

        with self.assertRaises(SyncError):
            migrate_compose_file(self.compose, self.live)

        self.assertEqual(self.compose.read_text(encoding="utf-8"), original)
        self.assertEqual(self.live.read_bytes(), old_live)

    def test_migration_accepts_fresh_compose_without_provider_cookies(self):
        self.live.unlink()
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n",
            encoding="utf-8",
        )

        result = migrate_compose_file(self.compose, self.live)

        self.assertEqual(result["migrated"], [])
        self.assertTrue(result["compose_changed"])
        self.assertTrue(result["migration_pending"])
        self.assertEqual(self.live.read_bytes(), b"")
        self.assertEqual(
            self.live.stat().st_mode & 0o777,
            0o600,
        )
        self.assertIn(
            "      - path: ./secrets/rsshub.env\n",
            self.compose.read_text(),
        )
        self.assertIn("        format: raw\n", self.compose.read_text())

    def test_bootstrap_unseeded_checks_only_rsshub_health(self):
        self.live.unlink()
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n",
            encoding="utf-8",
        )
        migrate_compose_file(self.compose, self.live)

        result = self.service.bootstrap()

        self.assertEqual(result, {"version": 1, "bootstrapped": True, "unseeded": True})
        self.assertEqual(
            [urlsplit(url).path for _, url, _, _ in self.transport.requests],
            ["/healthz"],
        )
        state = load_state(self.config.state_file)
        self.assertEqual(state["bootstrap"]["status"], "unseeded")
        self.assertEqual(state["providers"]["zhihu"]["last_probe"], "unknown")
        self.assertEqual(state["providers"]["weibo"]["last_probe"], "unknown")

    def test_migration_collapses_secret_only_environment_to_empty_mapping(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n"
            "      TWITTER_AUTH_TOKEN: token-new\n",
            encoding="utf-8",
        )

        migrate_compose_file(self.compose, self.live)

        migrated = self.compose.read_text()
        self.assertIn("    environment: {}\n", migrated)
        self.assertNotIn("    environment:\n", migrated)
        self.assertIn("      - path: ./secrets/rsshub.env\n", migrated)

    def test_migration_uses_actual_service_child_indent(self):
        self.compose.write_text(
            "services:\n"
            "    rsshub:\n"
            "        image: diygod/rsshub:latest\n"
            "        environment:\n"
            "            ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "            WEIBO_COOKIES: " + WB_NEW + "\n"
            "            TWITTER_AUTH_TOKEN: token-new\n"
            "    redis:\n"
            "        image: redis:latest\n",
            encoding="utf-8",
        )

        migrate_compose_file(self.compose, self.live)

        migrated = self.compose.read_text()
        self.assertIn("        env_file:\n", migrated)
        self.assertIn("          - path: ./secrets/rsshub.env\n", migrated)
        self.assertIn("            format: raw\n", migrated)
        self.assertNotIn("      env_file:\n", migrated.splitlines(keepends=True))

    def test_migration_only_extracts_rsshub_direct_environment(self):
        other_cookie = "z_c0=other; foo=bar"
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n"
            "  other:\n"
            "    image: example/other:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + other_cookie + "\n",
            encoding="utf-8",
        )

        result = migrate_compose_file(self.compose, self.live)

        self.assertEqual(result["migrated"], ["WEIBO_COOKIES", "ZHIHU_COOKIES"])
        migrated = self.compose.read_text()
        self.assertIn("      ZHIHU_COOKIES: " + other_cookie, migrated)
        self.assertNotIn("      ZHIHU_COOKIES: " + ZH_NEW, migrated)

    def test_migration_appends_managed_entry_to_existing_env_file(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    env_file:\n"
            "      - ./other.env\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n",
            encoding="utf-8",
        )

        migrate_compose_file(self.compose, self.live)

        migrated = self.compose.read_text()
        self.assertIn("      - ./other.env\n", migrated)
        self.assertIn("      - path: ./secrets/rsshub.env\n", migrated)
        self.assertIn("        format: raw\n", migrated)

    def test_migration_rejects_existing_target_without_raw_format(self):
        original = (
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    env_file:\n"
            "      - path: ./secrets/rsshub.env\n"
            "        format: dotenv\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n"
        )
        self.compose.write_text(original, encoding="utf-8")
        old_live = self.live.read_bytes()

        with self.assertRaises(SyncError):
            migrate_compose_file(self.compose, self.live)

        self.assertEqual(self.compose.read_text(), original)
        self.assertEqual(self.live.read_bytes(), old_live)

    def test_migration_marker_must_match_final_compose_wiring(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n",
            encoding="utf-8",
        )
        marker = self.compose.with_name(self.compose.name + ".pre-cookie-sync.txn.json")
        atomic_write(
            marker,
            json.dumps({"version": 1, "phase": "compose_replaced", "live_existed": True}).encode(),
        )

        with self.assertRaises(SyncError):
            migrate_compose_file(self.compose, self.live)

        self.assertIn("ZHIHU_COOKIES: " + ZH_NEW, self.compose.read_text())

    def test_valid_migration_marker_is_accepted_only_for_managed_compose(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    env_file:\n"
            "      - path: ./secrets/rsshub.env\n"
            "        format: raw\n"
            "    image: diygod/rsshub:latest\n",
            encoding="utf-8",
        )
        marker = self.compose.with_name(self.compose.name + ".pre-cookie-sync.txn.json")
        atomic_write(
            marker,
            json.dumps({"version": 1, "phase": "compose_replaced", "live_existed": True}).encode(),
        )

        result = migrate_compose_file(self.compose, self.live)

        self.assertTrue(result["migration_pending"])

    def test_migration_rejects_orphan_compose_backup_without_marker(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n",
            encoding="utf-8",
        )
        atomic_write(
            self.compose.with_name(self.compose.name + ".pre-cookie-sync"),
            b"old compose",
        )

        with self.assertRaises(SyncError):
            migrate_compose_file(self.compose, self.live)

    def test_migration_rejects_orphan_live_backup_without_marker(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n",
            encoding="utf-8",
        )
        atomic_write(self.live.with_name(self.live.name + ".pre-cookie-sync"), b"old env")

        with self.assertRaises(SyncError):
            migrate_compose_file(self.compose, self.live)

    def test_finalize_rejects_incomplete_migration_phase(self):
        marker = self.compose.with_name(self.compose.name + ".pre-cookie-sync.txn.json")
        atomic_write(
            marker,
            json.dumps({"version": 1, "phase": "prepared", "live_existed": True}).encode(),
        )

        with self.assertRaises(SyncError):
            finalize_migration(self.compose, self.live)
        self.assertTrue(marker.exists())

    def test_migration_rejects_ambiguous_service_child_indent(self):
        self.compose.write_text(
            "services:\n"
            "  rsshub:\n"
            "    - unexpected-list-item\n"
            "      environment:\n"
            "        ZHIHU_COOKIES: " + ZH_NEW + "\n",
            encoding="utf-8",
        )

        with self.assertRaises(SyncError):
            migrate_compose_file(self.compose, self.live)

    def test_migration_rollback_restores_original_compose_and_env(self):
        original = (
            "services:\n"
            "  rsshub:\n"
            "    image: diygod/rsshub:latest\n"
            "    environment:\n"
            "      ZHIHU_COOKIES: " + ZH_NEW + "\n"
            "      WEIBO_COOKIES: " + WB_NEW + "\n"
        )
        self.compose.write_text(original, encoding="utf-8")
        old_compose = self.compose.read_bytes()
        old_env = self.live.read_bytes()
        # The migration replaces values in both files, then rollback restores
        # exactly what was present before it started.
        migrate_compose_file(self.compose, self.live)
        rollback_migration(
            self.compose,
            self.live,
            config=self.config,
            runner=self.docker,
            clock=self.clock,
            transport=self.transport,
        )
        self.assertEqual(self.compose.read_bytes(), old_compose)
        self.assertEqual(self.live.read_bytes(), old_env)
        self.assertFalse(self.compose.with_name(self.compose.name + ".pre-cookie-sync.txn.json").exists())

    def test_status_contains_neither_hashes_nor_cookie_values(self):
        self.service._save_candidate("zhihu", ZH_NEW)
        result = self.service.status()
        encoded = json.dumps(result, ensure_ascii=True)
        self.assertNotIn(ZH_OLD, encoded)
        self.assertNotIn(ZH_NEW, encoded)
        self.assertNotIn("live_hash", result["providers"]["zhihu"])
        self.assertNotIn("candidate_hash", result["providers"]["zhihu"])


if __name__ == "__main__":
    unittest.main()
