from __future__ import annotations

import json
import os
import signal
import shlex
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

from ..models import AutomationRun, WorkerLaunchSpec, utc_now


class WorkerLaunchError(RuntimeError):
    pass


def _env(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


def _bool_env(name: str) -> bool | None:
    value = _env(name)
    if value is None:
        return None
    normalized = value.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"Environment variable `{name}` must be a boolean value.")


def _csv_env(name: str) -> list[str]:
    value = _env(name)
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _shell_quote(value: str) -> str:
    return shlex.quote(value)


def _normalize_group_ids(primary: str | None, extras: list[str]) -> list[str]:
    group_ids = [item.strip() for item in extras if item and item.strip()]
    if primary and primary.strip():
        group_ids.append(primary.strip())
    # Preserve order while removing duplicates.
    return list(dict.fromkeys(group_ids))


def _stringify_manifest_value(value: object) -> object:
    if isinstance(value, dict):
        return {str(key): _stringify_manifest_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_stringify_manifest_value(item) for item in value]
    if isinstance(value, tuple):
        return [_stringify_manifest_value(item) for item in value]
    return value


def resolve_worker_launch_mode(mode: str | None = None) -> str:
    resolved = (mode or _env("AXLE_COORDINATOR_WORKER_LAUNCH_MODE") or "planned").strip().lower()
    if resolved not in {"planned", "ec2", "local"}:
        raise ValueError(f"Unsupported worker launch mode `{resolved}`.")
    return resolved


@dataclass
class Ec2WorkerLauncher:
    ami_id: str | None = None
    instance_type: str = "t3.large"
    subnet_id: str | None = None
    security_group_id: str | None = None
    security_group_ids: list[str] = field(default_factory=list)
    instance_profile_name: str | None = None
    key_name: str | None = None
    associate_public_ip_address: bool | None = None
    terminate_on_finish: bool = True
    tags: dict[str, str] = field(default_factory=dict)
    bootstrap_dir: str = "/var/lib/axle-worker"
    client_factory: Callable[[], Any] | None = None

    @staticmethod
    def _instance_state_name(instance: dict[str, Any]) -> str:
        state = instance.get("State")
        if isinstance(state, dict):
            return str(state.get("Name") or "").strip().lower()
        return ""

    def _ec2_client(self) -> Any | None:
        if self.client_factory is not None:
            return self.client_factory()
        try:
            import boto3  # type: ignore
        except ImportError:
            return None
        return boto3.client("ec2")

    def _worker_mode(self, coordinator_url: str | None) -> str:
        return "poll" if coordinator_url else "run"

    def _worker_command(self, run: AutomationRun, coordinator_url: str | None = None) -> list[str]:
        if coordinator_url:
            return [
                "python3",
                "-m",
                "axle_cli.worker.entrypoint",
                "poll",
                "--coordinator-url",
                coordinator_url,
                "--worker-id",
                f"axle-worker-{run.run_id[:8]}",
                "--once",
            ]
        return [
            "python3",
            "-m",
            "axle_cli.worker.entrypoint",
            "run",
            "--run-id",
            run.run_id,
            "--callback-token",
            run.callback_token,
        ]

    def _bootstrap_manifest(self, run: AutomationRun, coordinator_url: str | None = None) -> dict[str, object]:
        command = self._worker_command(run, coordinator_url=coordinator_url)
        return {
            "run": {
                "run_id": run.run_id,
                "issue_key": run.issue_key,
                "issue_url": run.issue_url,
                "repository": run.repository,
                "base_branch": run.base_branch,
                "status": run.status,
                "callback_url": run.callback_url,
            },
            "auth": {
                "callback_token": run.callback_token,
            },
            "coordinator_url": coordinator_url,
            "instance_name": f"axle-worker-{run.run_id[:8]}",
            "worker_mode": self._worker_mode(coordinator_url),
            "terminate_on_finish": self.terminate_on_finish,
            "worker_command": command,
        }

    def _render_user_data(self, manifest: dict[str, object]) -> str:
        bootstrap_file = f"{self.bootstrap_dir}/bootstrap.json"
        bootstrap_json = json.dumps(_stringify_manifest_value(manifest), indent=2, sort_keys=True)
        env_lines = [
            f"export AXLE_BOOTSTRAP_FILE={_shell_quote(bootstrap_file)}",
            f"export AXLE_RUN_ID={_shell_quote(str(manifest['run']['run_id']))}",
            f"export AXLE_CALLBACK_TOKEN={_shell_quote(str(manifest['auth']['callback_token']))}",
            f"export AXLE_COORDINATOR_URL={_shell_quote(str(manifest.get('coordinator_url') or ''))}",
            f"export AXLE_INSTANCE_NAME={_shell_quote(str(manifest.get('instance_name') or ''))}",
            f"export AXLE_TERMINATE_ON_FINISH={_shell_quote('1' if manifest.get('terminate_on_finish') else '0')}",
        ]
        env_block = "\n".join(env_lines)
        command = " ".join(_shell_quote(part) for part in manifest["worker_command"])
        cleanup_block = textwrap.dedent(
            """
            cleanup() {
              if [ "${AXLE_TERMINATE_ON_FINISH}" = "1" ]; then
                if command -v shutdown >/dev/null 2>&1; then
                  shutdown -h now
                elif command -v poweroff >/dev/null 2>&1; then
                  poweroff
                fi
              fi
            }
            trap cleanup EXIT
            """
        ).strip()
        user_data_lines = [
            "#!/bin/bash",
            "set -euo pipefail",
            f"BOOTSTRAP_FILE={_shell_quote(bootstrap_file)}",
            f"mkdir -p {_shell_quote(self.bootstrap_dir)}",
            env_block,
            "cat > \"$BOOTSTRAP_FILE\" <<'JSON'",
            bootstrap_json,
            "JSON",
            "chmod 600 \"$BOOTSTRAP_FILE\"",
            cleanup_block,
            command,
        ]
        return "\n".join(line for line in user_data_lines if line).strip()

    def build_launch_spec(self, run: AutomationRun, coordinator_url: str | None = None) -> WorkerLaunchSpec:
        instance_name = f"axle-worker-{run.run_id[:8]}"
        bootstrap_manifest = self._bootstrap_manifest(run, coordinator_url=coordinator_url)
        user_data = self._render_user_data(bootstrap_manifest)
        launch_config = {
            "ami_id": self.ami_id,
            "instance_type": self.instance_type,
            "subnet_id": self.subnet_id,
            "security_group_ids": _normalize_group_ids(self.security_group_id, self.security_group_ids),
            "instance_profile_name": self.instance_profile_name,
            "key_name": self.key_name,
            "associate_public_ip_address": self.associate_public_ip_address,
            "terminate_on_finish": self.terminate_on_finish,
            "tags": dict(self.tags),
        }
        return WorkerLaunchSpec(
            run_id=run.run_id,
            callback_token=run.callback_token,
            instance_name=instance_name,
            coordinator_url=coordinator_url,
            user_data=user_data,
            bootstrap_manifest=bootstrap_manifest,
            launch_config=launch_config,
            terminate_on_finish=self.terminate_on_finish,
        )

    def describe_instances(
        self,
        *,
        run_id: str | None = None,
        instance_ids: Iterable[str] | None = None,
        include_terminated: bool = False,
    ) -> list[dict[str, Any]]:
        client = self._ec2_client()
        if client is None:
            return []

        params: dict[str, Any] = {
            "MaxResults": 100,
        }
        filtered_instance_ids = [item.strip() for item in (instance_ids or []) if item and str(item).strip()]
        if filtered_instance_ids:
            params["InstanceIds"] = filtered_instance_ids
        else:
            filters: list[dict[str, str]] = []
            if run_id:
                filters.append({"Name": "tag:AxleRunId", "Values": [run_id]})
            if not include_terminated:
                filters.append({"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]})
            if filters:
                params["Filters"] = filters
        response = client.describe_instances(**params)
        instances: list[dict[str, Any]] = []
        for reservation in response.get("Reservations") or []:
            for instance in reservation.get("Instances") or []:
                if isinstance(instance, dict):
                    instances.append(instance)
        return instances

    def reconcile_run_instance(self, run: AutomationRun | dict[str, Any], coordinator_url: str | None = None) -> dict[str, Any]:
        record = run if isinstance(run, AutomationRun) else AutomationRun.from_json(dict(run))
        launch_spec = record.worker_launch_spec
        instance_ids = []
        if launch_spec and launch_spec.instance_id:
            instance_ids.append(launch_spec.instance_id)
        if record.worker_instance_id and record.worker_instance_id not in instance_ids:
            instance_ids.append(record.worker_instance_id)

        instances = self.describe_instances(
            run_id=record.run_id,
            instance_ids=instance_ids or None,
            include_terminated=True,
        )
        preferred_instance = None
        if instance_ids:
            for candidate in instances:
                if candidate.get("InstanceId") in instance_ids:
                    preferred_instance = candidate
                    break
        if preferred_instance is None and instances:
            preferred_instance = instances[0]

        state_name = self._instance_state_name(preferred_instance) if preferred_instance else "missing"
        active = state_name in {"pending", "running"}
        reconciliation = {
            "run_id": record.run_id,
            "coordinator_url": coordinator_url or (launch_spec.coordinator_url if launch_spec else None),
            "instance_id": preferred_instance.get("InstanceId") if preferred_instance else (instance_ids[0] if instance_ids else None),
            "instance_state": state_name,
            "instance_present": bool(preferred_instance),
            "instance_active": active,
            "terminate_on_finish": bool(launch_spec.terminate_on_finish if launch_spec else self.terminate_on_finish),
            "instances": [
                {
                    "instance_id": instance.get("InstanceId"),
                    "state": self._instance_state_name(instance),
                    "tags": {tag.get("Key"): tag.get("Value") for tag in (instance.get("Tags") or []) if isinstance(tag, dict)},
                }
                for instance in instances
            ],
        }
        if preferred_instance and launch_spec and not launch_spec.instance_id:
            reconciliation["instance_id"] = preferred_instance.get("InstanceId")
        return reconciliation

    def terminate_instances(self, instance_ids: Iterable[str]) -> list[str]:
        client = self._ec2_client()
        normalized_ids = [instance_id.strip() for instance_id in instance_ids if instance_id and instance_id.strip()]
        if client is None or not normalized_ids:
            return []
        response = client.terminate_instances(InstanceIds=normalized_ids)
        terminating = []
        for item in response.get("TerminatingInstances") or []:
            instance_id = item.get("InstanceId")
            if isinstance(instance_id, str) and instance_id.strip():
                terminating.append(instance_id)
        return terminating or normalized_ids

    def terminate_run_instance(self, run: AutomationRun | dict[str, Any], coordinator_url: str | None = None) -> dict[str, Any]:
        reconciliation = self.reconcile_run_instance(run, coordinator_url=coordinator_url)
        if not reconciliation["instance_present"]:
            reconciliation["terminated"] = False
            reconciliation["termination_skipped"] = "no_instance"
            return reconciliation
        if not reconciliation["instance_active"]:
            reconciliation["terminated"] = False
            reconciliation["termination_skipped"] = reconciliation["instance_state"]
            return reconciliation
        instance_id = reconciliation.get("instance_id")
        if not isinstance(instance_id, str) or not instance_id.strip():
            reconciliation["terminated"] = False
            reconciliation["termination_skipped"] = "missing_instance_id"
            return reconciliation
        terminated = self.terminate_instances([instance_id])
        reconciliation["terminated"] = bool(terminated)
        reconciliation["terminated_instance_ids"] = terminated
        return reconciliation

    def launch(self, run: AutomationRun, coordinator_url: str | None = None) -> WorkerLaunchSpec:
        spec = self.build_launch_spec(run, coordinator_url=coordinator_url)
        if not self.ami_id:
            spec.status = "planned"
            spec.launched_at = utc_now()
            return spec
        client = self._ec2_client()
        if client is None:
            raise WorkerLaunchError("boto3 is required to launch EC2 workers.")
        params: dict[str, object] = {
            "ImageId": self.ami_id,
            "InstanceType": self.instance_type,
            "MinCount": 1,
            "MaxCount": 1,
            "UserData": spec.user_data or "",
            "TagSpecifications": [
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": spec.instance_name},
                        {"Key": "AxleRunId", "Value": run.run_id},
                    ]
                    + [{"Key": key, "Value": value} for key, value in sorted(self.tags.items())],
                }
            ],
            "InstanceInitiatedShutdownBehavior": "terminate" if self.terminate_on_finish else "stop",
        }
        if coordinator_url:
            params["TagSpecifications"][0]["Tags"].append({"Key": "AxleCoordinator", "Value": coordinator_url})
        if self.key_name:
            params["KeyName"] = self.key_name
        if self.subnet_id:
            params["SubnetId"] = self.subnet_id
        security_group_ids = _normalize_group_ids(self.security_group_id, self.security_group_ids)
        if security_group_ids:
            params["SecurityGroupIds"] = security_group_ids
        if self.instance_profile_name:
            params["IamInstanceProfile"] = {"Name": self.instance_profile_name}
        if self.associate_public_ip_address is not None and self.subnet_id:
            params["NetworkInterfaces"] = [
                {
                    "DeviceIndex": 0,
                    "SubnetId": self.subnet_id,
                    "AssociatePublicIpAddress": self.associate_public_ip_address,
                    "Groups": security_group_ids,
                }
            ]
            params.pop("SubnetId", None)
            params.pop("SecurityGroupIds", None)
        response = client.run_instances(**params)
        instances = response.get("Instances") or []
        if not instances:
            raise WorkerLaunchError("EC2 launch did not return an instance record.")
        spec.instance_id = instances[0]["InstanceId"]
        spec.status = "launched"
        spec.launched_at = utc_now()
        return spec


@dataclass
class LocalWorkerLauncher(Ec2WorkerLauncher):
    store_root: str | None = None
    working_directory: str | None = None
    python_executable: str = sys.executable
    extra_env: dict[str, str] = field(default_factory=dict)

    def _local_instance_id(self, pid: int | None) -> str | None:
        if pid is None:
            return None
        return f"local-{pid}"

    def _local_pid(self, instance_id: str | None) -> int | None:
        if not instance_id:
            return None
        text = str(instance_id).strip()
        if not text.startswith("local-"):
            return None
        try:
            return int(text.split("-", 1)[1])
        except (IndexError, ValueError):
            return None

    def _local_worker_command(self, run: AutomationRun) -> list[str]:
        command = [
            self.python_executable,
            "-m",
            "axle_cli.worker.entrypoint",
            "run",
            "--run-id",
            run.run_id,
            "--callback-token",
            run.callback_token,
        ]
        if self.store_root:
            command.extend(["--store-root", self.store_root])
        return command

    def _local_log_path(self, run: AutomationRun) -> Path:
        root = Path(self.store_root).expanduser().resolve() if self.store_root else Path.cwd() / ".axle-cli" / "coordinator"
        log_dir = root / "local-workers"
        log_dir.mkdir(parents=True, exist_ok=True)
        return log_dir / f"{run.run_id}.log"

    def _is_pid_active(self, pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def build_launch_spec(self, run: AutomationRun, coordinator_url: str | None = None) -> WorkerLaunchSpec:
        command = self._local_worker_command(run)
        instance_name = f"axle-local-{run.run_id[:8]}"
        log_path = self._local_log_path(run)
        return WorkerLaunchSpec(
            run_id=run.run_id,
            callback_token=run.callback_token,
            instance_name=instance_name,
            coordinator_url=coordinator_url,
            bootstrap_manifest={
                "run": {
                    "run_id": run.run_id,
                    "issue_key": run.issue_key,
                    "repository": run.repository,
                    "base_branch": run.base_branch,
                    "status": run.status,
                },
                "launch_mode": "local",
                "worker_command": command,
            },
            launch_config={
                "mode": "local",
                "command": command,
                "store_root": self.store_root,
                "working_directory": self.working_directory,
                "log_path": str(log_path),
            },
            terminate_on_finish=False,
            status="planned",
        )

    def describe_instances(
        self,
        *,
        run_id: str | None = None,
        instance_ids: Iterable[str] | None = None,
        include_terminated: bool = False,
    ) -> list[dict[str, Any]]:
        del run_id, include_terminated
        instances: list[dict[str, Any]] = []
        for instance_id in instance_ids or []:
            pid = self._local_pid(str(instance_id))
            if pid is None:
                continue
            instances.append(
                {
                    "InstanceId": str(instance_id),
                    "State": {"Name": "running" if self._is_pid_active(pid) else "terminated"},
                    "Tags": [{"Key": "AxleLaunchMode", "Value": "local"}],
                }
            )
        return instances

    def reconcile_run_instance(self, run: AutomationRun | dict[str, Any], coordinator_url: str | None = None) -> dict[str, Any]:
        record = run if isinstance(run, AutomationRun) else AutomationRun.from_json(dict(run))
        launch_spec = record.worker_launch_spec
        instance_id = None
        if launch_spec and launch_spec.instance_id:
            instance_id = launch_spec.instance_id
        elif record.worker_instance_id:
            instance_id = record.worker_instance_id
        pid = self._local_pid(instance_id)
        active = self._is_pid_active(pid)
        state = "running" if active else "terminated"
        return {
            "run_id": record.run_id,
            "coordinator_url": coordinator_url or (launch_spec.coordinator_url if launch_spec else None),
            "instance_id": instance_id,
            "instance_state": state if instance_id else "missing",
            "instance_present": bool(instance_id),
            "instance_active": active,
            "terminate_on_finish": False,
            "instances": (
                [{"instance_id": instance_id, "state": state, "tags": {"AxleLaunchMode": "local"}}]
                if instance_id
                else []
            ),
        }

    def terminate_instances(self, instance_ids: Iterable[str]) -> list[str]:
        terminated: list[str] = []
        for instance_id in instance_ids:
            text = str(instance_id).strip()
            pid = self._local_pid(text)
            if pid is None or not self._is_pid_active(pid):
                continue
            os.kill(pid, signal.SIGTERM)
            terminated.append(text)
        return terminated

    def launch(self, run: AutomationRun, coordinator_url: str | None = None) -> WorkerLaunchSpec:
        spec = self.build_launch_spec(run, coordinator_url=coordinator_url)
        command = list(spec.launch_config.get("command") or [])
        if not command:
            raise WorkerLaunchError("Local worker launch command is empty.")
        log_path = Path(str(spec.launch_config.get("log_path") or self._local_log_path(run)))
        env = os.environ.copy()
        env.update({key: value for key, value in self.extra_env.items() if value is not None})
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(f"[{utc_now().isoformat()}] launching local worker for run {run.run_id}\n")
            log_handle.flush()
            process = subprocess.Popen(  # noqa: S603
                command,
                cwd=self.working_directory or str(Path(__file__).resolve().parents[2]),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        spec.instance_id = self._local_instance_id(process.pid)
        spec.status = "launched"
        spec.launched_at = utc_now()
        return spec


def build_worker_launcher(
    *,
    mode: str | None = None,
    store_root: str | None = None,
    working_directory: str | None = None,
    python_executable: str | None = None,
) -> Ec2WorkerLauncher:
    resolved_mode = resolve_worker_launch_mode(mode)
    if resolved_mode == "local":
        return LocalWorkerLauncher(
            store_root=store_root,
            working_directory=working_directory,
            python_executable=python_executable or sys.executable,
        )

    ami_id = _env("AXLE_EC2_AMI_ID")
    terminate_on_finish = _bool_env("AXLE_EC2_TERMINATE_ON_FINISH")
    if resolved_mode == "ec2" and not ami_id:
        raise ValueError("`AXLE_EC2_AMI_ID` is required when `--worker-launch-mode ec2` is enabled.")
    return Ec2WorkerLauncher(
        ami_id=ami_id,
        instance_type=_env("AXLE_EC2_INSTANCE_TYPE") or "t3.large",
        subnet_id=_env("AXLE_EC2_SUBNET_ID"),
        security_group_id=_env("AXLE_EC2_SECURITY_GROUP_ID"),
        security_group_ids=_csv_env("AXLE_EC2_SECURITY_GROUP_IDS"),
        instance_profile_name=_env("AXLE_EC2_INSTANCE_PROFILE_NAME") or _env("AXLE_EC2_INSTANCE_PROFILE"),
        key_name=_env("AXLE_EC2_KEY_NAME"),
        associate_public_ip_address=_bool_env("AXLE_EC2_ASSOCIATE_PUBLIC_IP_ADDRESS"),
        terminate_on_finish=terminate_on_finish if terminate_on_finish is not None else True,
        tags={},
    )
