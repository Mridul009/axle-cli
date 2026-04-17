from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


AUTOSCALE_LABEL = "axle.autoscaled=true"
POOL_LABEL_PREFIX = "axle.worker_pool="
ACTIVE_RUN_STATUSES = {"launching", "running", "cancel_requested"}


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _clean(value: str | None) -> str:
    return str(value or "").strip()


@dataclass(frozen=True)
class DockerWorkerPoolConfig:
    enabled: bool = False
    image: str = ""
    network: str | None = None
    volume: str | None = None
    cli_home: str = "/data/.axle-cli"
    min_workers: int = 0
    max_workers: int = 4
    idle_ttl_seconds: int = 600
    poll_interval_seconds: int = 5
    container_prefix: str = "axle-worker-auto"
    coordinator_url: str = ""
    worker_token: str = ""
    disabled_reason: str | None = None

    @classmethod
    def from_env(cls, *, coordinator_url: str | None = None, worker_token: str | None = None) -> "DockerWorkerPoolConfig":
        enabled = _truthy(os.getenv("AXLE_DOCKER_AUTOSCALE"))
        image = _clean(os.getenv("AXLE_WORKER_IMAGE"))
        resolved_url = _clean(coordinator_url) or _clean(os.getenv("AXLE_COORDINATOR_URL"))
        resolved_token = _clean(worker_token) or _clean(os.getenv("AXLE_WORKER_TOKEN"))
        min_workers = max(0, _env_int("AXLE_WORKER_MIN", 0))
        max_workers = max(0, _env_int("AXLE_WORKER_MAX", 4))
        idle_ttl_seconds = max(1, _env_int("AXLE_WORKER_IDLE_TTL_SECONDS", 600))
        poll_interval_seconds = max(1, _env_int("AXLE_WORKER_POLL_INTERVAL_SECONDS", 5))
        container_prefix = _clean(os.getenv("AXLE_WORKER_CONTAINER_PREFIX")) or "axle-worker-auto"

        disabled_reason = None
        if not enabled:
            disabled_reason = "AXLE_DOCKER_AUTOSCALE is not enabled."
        elif not image:
            disabled_reason = "AXLE_WORKER_IMAGE is required."
        elif not resolved_url:
            disabled_reason = "AXLE_COORDINATOR_URL is required."
        elif not resolved_token:
            disabled_reason = "AXLE_WORKER_TOKEN or --worker-token is required."
        elif max_workers < 1:
            disabled_reason = "AXLE_WORKER_MAX must be at least 1."
        elif min_workers > max_workers:
            disabled_reason = "AXLE_WORKER_MIN cannot exceed AXLE_WORKER_MAX."

        return cls(
            enabled=enabled and disabled_reason is None,
            image=image,
            network=_clean(os.getenv("AXLE_WORKER_NETWORK")) or None,
            volume=_clean(os.getenv("AXLE_WORKER_VOLUME")) or None,
            cli_home=_clean(os.getenv("AXLE_WORKER_CLI_HOME")) or "/data/.axle-cli",
            min_workers=min_workers,
            max_workers=max_workers,
            idle_ttl_seconds=idle_ttl_seconds,
            poll_interval_seconds=poll_interval_seconds,
            container_prefix=container_prefix,
            coordinator_url=resolved_url,
            worker_token=resolved_token,
            disabled_reason=disabled_reason,
        )


class DockerWorkerPoolManager:
    def __init__(self, service: Any, config: DockerWorkerPoolConfig) -> None:
        self.service = service
        self.config = config

    @property
    def enabled(self) -> bool:
        return self.config.enabled

    def reconcile_once(self) -> dict[str, int]:
        if not self.enabled:
            return {"queued": 0, "desired": 0, "running": 0, "started": 0, "stopped": 0}

        runs = self.service.list_runs()
        queued = sum(1 for run in runs if run.status == "queued")
        active_worker_ids = {
            str(run.worker_instance_id)
            for run in runs
            if run.status in ACTIVE_RUN_STATUSES and run.worker_instance_id
        }
        all_containers = self.list_container_names(all_containers=True)
        running_containers = self.list_container_names(all_containers=False)
        desired = min(self.config.max_workers, max(self.config.min_workers, queued))

        started = 0
        if len(running_containers) < desired:
            for _ in range(desired - len(running_containers)):
                name = self._next_container_name(all_containers)
                all_containers.append(name)
                self.start_container(name)
                started += 1

        stopped = 0
        started_names = all_containers[-started:] if started else []
        running_after_start = list(running_containers) + started_names
        if queued == 0 and len(running_after_start) > desired:
            stop_budget = len(running_after_start) - desired
            for name in running_after_start:
                if stop_budget <= 0:
                    break
                if name in active_worker_ids:
                    continue
                if not self.container_is_idle(name):
                    continue
                self.stop_container(name)
                stopped += 1
                stop_budget -= 1

        return {
            "queued": queued,
            "desired": desired,
            "running": len(running_containers),
            "started": started,
            "stopped": stopped,
        }

    def list_container_names(self, *, all_containers: bool) -> list[str]:
        command = [
            "docker",
            "ps",
            "-a" if all_containers else "",
            "--filter",
            f"label={AUTOSCALE_LABEL}",
            "--filter",
            f"label={POOL_LABEL_PREFIX}{self.config.container_prefix}",
            "--format",
            "{{.Names}}",
        ]
        result = self._run([part for part in command if part])
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def start_container(self, name: str) -> None:
        command = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--restart",
            "unless-stopped",
            "--label",
            AUTOSCALE_LABEL,
            "--label",
            f"{POOL_LABEL_PREFIX}{self.config.container_prefix}",
        ]
        if self.config.network:
            command.extend(["--network", self.config.network])
        if self.config.volume:
            command.extend(["-v", self.config.volume])
        command.extend(
            [
                "-e",
                f"AXLE_CLI_HOME={self.config.cli_home}",
                "-e",
                "AXLE_AUTOSCALED_WORKER=true",
                self.config.image,
                "axle",
                "worker",
                "poll",
                "--coordinator-url",
                self.config.coordinator_url,
                "--worker-token",
                self.config.worker_token,
                "--worker-id",
                name,
                "--poll-interval",
                str(self.config.poll_interval_seconds),
            ]
        )
        self._run(command)

    def stop_container(self, name: str) -> None:
        self._run(["docker", "rm", "-f", name])

    def container_is_idle(self, name: str) -> bool:
        started_at = self.container_started_at(name)
        if started_at is None:
            return False
        age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
        return age_seconds >= self.config.idle_ttl_seconds

    def container_started_at(self, name: str) -> datetime | None:
        result = self._run(["docker", "inspect", "--format", "{{.State.StartedAt}}", name])
        return _parse_docker_timestamp(result.stdout.strip())

    def _next_container_name(self, existing_names: list[str]) -> str:
        existing = set(existing_names)
        index = 1
        while True:
            name = f"{self.config.container_prefix}-{index}"
            if name not in existing:
                return name
            index += 1

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.run(command, check=True, capture_output=True, text=True)  # noqa: S603


def _parse_docker_timestamp(value: str) -> datetime | None:
    text = value.strip()
    if not text or text.startswith("0001-"):
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    if "." in text:
        prefix, suffix = text.split(".", 1)
        timezone_start = max(suffix.rfind("+"), suffix.rfind("-"))
        if timezone_start > 0:
            fraction = suffix[:timezone_start][:6]
            zone = suffix[timezone_start:]
        else:
            fraction = suffix[:6]
            zone = ""
        text = f"{prefix}.{fraction}{zone}"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_docker_worker_pool(
    service: Any,
    *,
    coordinator_url: str | None = None,
    worker_token: str | None = None,
) -> DockerWorkerPoolManager:
    return DockerWorkerPoolManager(
        service,
        DockerWorkerPoolConfig.from_env(coordinator_url=coordinator_url, worker_token=worker_token),
    )
