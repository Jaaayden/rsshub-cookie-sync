"""docker (part of the RSSHub Cookie Sync server)."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional, Sequence

from .clock import Clock
from .config import RuntimeConfig, _validate_compose_project, _validate_compose_service
from .constants import CONTAINER_ID_RE


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner:
    """Subprocess runner which never writes command output to logs."""

    def run(
        self, args: Sequence[str], timeout: float, capture: bool = False,
        output_limit: int = 1024,
    ) -> CommandResult:
        try:
            completed = subprocess.run(
                list(args),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                stderr=subprocess.PIPE if capture else subprocess.DEVNULL,
                env={
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "LC_ALL": "C",
                },
                timeout=timeout,
                check=False,
                text=True,
            )
        except subprocess.TimeoutExpired:
            return CommandResult(124, "", "")
        except OSError:
            return CommandResult(127, "", "")
        # Compose discovery needs more than a container ID.  Its secret-bearing
        # output stays in memory and is never forwarded to logs or status.
        return CommandResult(
            int(completed.returncode),
            (completed.stdout or "")[:output_limit],
            (completed.stderr or "")[:1024],
        )


class DockerCompose:
    def __init__(
        self,
        config: RuntimeConfig,
        runner: Optional[CommandRunner] = None,
        clock: Optional[Clock] = None,
    ) -> None:
        self.config = config
        self.runner = runner or CommandRunner()
        self.clock = clock or Clock()

    def _compose(self) -> list[str]:
        _validate_compose_project(self.config.project)
        _validate_compose_service(self.config.service)
        return [
            "docker",
            "compose",
            "-p",
            self.config.project,
            "-f",
            str(self.config.compose_file),
        ]

    def config_quiet(self) -> bool:
        result = self.runner.run(self._compose() + ["config", "--quiet"], timeout=30.0)
        return result.returncode == 0

    def recreate(self) -> bool:
        result = self.runner.run(
            self._compose()
            + ["up", "-d", "--no-deps", "--force-recreate", "--pull", "never", self.config.service],
            timeout=180.0,
        )
        return result.returncode == 0

    def _container_id(self) -> Optional[str]:
        result = self.runner.run(
            self._compose() + ["ps", "-q", self.config.service], timeout=15.0, capture=True
        )
        if result.returncode != 0:
            return None
        value = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        return value if CONTAINER_ID_RE.fullmatch(value) else None

    def _inspect(self, container_id: str, field: str) -> Optional[str]:
        if not CONTAINER_ID_RE.fullmatch(container_id):
            return None
        result = self.runner.run(
            ["docker", "inspect", "--format=" + field, container_id],
            timeout=15.0,
            capture=True,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip()[:64]

    def wait_healthy(self) -> bool:
        deadline = self.clock.now() + max(0.0, self.config.health_timeout)
        while self.clock.now() <= deadline:
            container_id = self._container_id()
            if container_id:
                status = self._inspect(container_id, "{{.State.Status}}")
                health = self._inspect(container_id, "{{if .State.Health}}{{.State.Health.Status}}{{end}}")
                if status == "running" and health in ("healthy", ""):
                    return True
                if status in ("exited", "dead", "created") or health == "unhealthy":
                    return False
            self.clock.sleep(min(self.config.health_poll_seconds, 1.0))
        return False
