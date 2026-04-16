from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _serialize_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _deserialize_datetime(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass(frozen=True)
class RepositorySource:
    value: str
    kind: Literal["remote", "local"] = "remote"
    path: Path | None = None
    label: str | None = None

    def normalized_value(self) -> str:
        return self.value.strip()

    def display_name(self) -> str:
        return self.label or self.normalized_value()


@dataclass
class WorkspaceMetadata:
    workspace_key: str
    base_branch: str
    source_value: str
    source_kind: Literal["remote", "local"]
    source_path: str | None = None
    run_id: str | None = None
    goose_session_name: str | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_base_url: str | None = None
    prepared_at: datetime = field(default_factory=utc_now)
    updated_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["prepared_at"] = self.prepared_at.isoformat()
        payload["updated_at"] = self.updated_at.isoformat() if self.updated_at else None
        return payload

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WorkspaceMetadata":
        prepared_at = payload.get("prepared_at")
        updated_at = payload.get("updated_at")
        return cls(
            workspace_key=payload["workspace_key"],
            base_branch=payload["base_branch"],
            source_value=payload["source_value"],
            source_kind=payload["source_kind"],
            source_path=payload.get("source_path"),
            run_id=payload.get("run_id"),
            goose_session_name=payload.get("goose_session_name"),
            llm_provider=payload.get("llm_provider"),
            llm_model=payload.get("llm_model"),
            llm_base_url=payload.get("llm_base_url"),
            prepared_at=datetime.fromisoformat(prepared_at) if prepared_at else utc_now(),
            updated_at=datetime.fromisoformat(updated_at) if updated_at else None,
        )


@dataclass
class SavedConfig:
    github_token: str | None = None
    github_login: str | None = None
    repository: str | None = None
    base_branch: str = "main"
    jira_base_url: str | None = None
    jira_email: str | None = None
    jira_api_token: str | None = None
    jira_project_key: str | None = None
    llm_api_key: str | None = None
    llm_provider: str = "openai"
    llm_base_url: str | None = None
    llm_base_path: str | None = None
    llm_model: str = "gpt-5-mini"
    llm_fallback_model: str | None = None
    goose_binary: str = "goose"
    goose_builtin: str | None = None
    goose_recipe: str | None = None
    default_test_command: str | None = None
    git_author_name: str = "Axle CLI"
    git_author_email: str = "axle-cli@example.com"
    last_workspace: str | None = None

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "SavedConfig":
        return cls(**{key: value for key, value in payload.items() if key in cls.__dataclass_fields__})

    def effective_goose_builtins(self) -> str | None:
        raw = (self.goose_builtin or "").strip()
        if not raw:
            return None
        builtins = [
            item.strip()
            for item in raw.split(",")
            if item.strip() and item.strip().lower() != "github"
        ]
        return ",".join(dict.fromkeys(builtins)) or None


@dataclass
class RunRequest:
    task: str
    repository: str
    base_branch: str
    task_context: str | None = None
    issue_key: str | None = None
    issue_url: str | None = None
    issue_labels: list[str] = field(default_factory=list)
    repository_path: str | None = None
    repository_kind: Literal["auto", "remote", "local"] = "auto"
    setup_mode: Literal["auto", "skip"] = "auto"
    test_command: str | None = None
    create_pr: bool = False
    model: str | None = None
    provider: str | None = None
    max_repair_attempts: int = 1
    workspace_key: str | None = None
    reuse_workspace: bool = True
    resume_goose_session: bool = False


@dataclass
class Workspace:
    run_id: str
    workspace_key: str
    root: Path
    repo_dir: Path
    transcript_path: Path
    prompt_path: Path
    diff_path: Path
    test_log_path: Path
    metadata_path: Path | None = None
    source_value: str | None = None
    source_kind: Literal["remote", "local"] = "remote"
    source_path: Path | None = None
    metadata: WorkspaceMetadata | None = None
    goose_session_name: str | None = None


@dataclass
class GooseRunResult:
    command: list[str]
    exit_code: int
    transcript_path: Path
    changed_files: list[str]
    diff_text: str
    summary: str
    output_kind: str = "unknown"
    transcript_excerpt: str = ""
    likely_files: list[str] = field(default_factory=list)
    file_write_candidates: dict[str, str] = field(default_factory=dict)
    structured_output: bool = False
    session_name: str | None = None
    assistant_text: str = ""
    output_format: str = "json"
    used_recipe: str | None = None
    structured_status: str | None = None
    tool_events: list[str] = field(default_factory=list)
    error_events: list[str] = field(default_factory=list)
    event_counts: dict[str, int] = field(default_factory=dict)
    tool_events: list[str] = field(default_factory=list)
    error_events: list[str] = field(default_factory=list)
    status_events: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PreparedWorkspaceContext:
    workspace: Workspace
    test_command: str | None
    repo_tree: str
    command_env: dict[str, str]
    bootstrap_notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass
class TestResult:
    command: str
    exit_code: int
    output: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


@dataclass
class RunSummary:
    run_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "completed"
    repository: str | None = None
    workspace: str | None = None
    changed_files: list[str] = field(default_factory=list)
    test_result: TestResult | None = None
    pr_url: str | None = None
    branch_name: str | None = None
    diff_path: str | None = None
    goose_session_name: str | None = None
    risks: list[str] = field(default_factory=list)
    failure: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None


@dataclass
class RunEvent:
    timestamp: datetime = field(default_factory=utc_now)
    kind: str = "info"
    stage: str | None = None
    message: str = ""
    details: dict[str, str] = field(default_factory=dict)

    def to_json(self) -> dict[str, Any]:
        return {
            "timestamp": _serialize_datetime(self.timestamp),
            "kind": self.kind,
            "stage": self.stage,
            "message": self.message,
            "details": self.details,
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "RunEvent":
        return cls(
            timestamp=_deserialize_datetime(payload.get("timestamp")) or utc_now(),
            kind=str(payload.get("kind") or "info"),
            stage=str(payload["stage"]) if payload.get("stage") is not None else None,
            message=str(payload.get("message") or ""),
            details=dict(payload.get("details") or {}),
        )


@dataclass
class WorkerLaunchSpec:
    run_id: str
    callback_token: str
    instance_name: str
    coordinator_url: str | None = None
    user_data: str | None = None
    bootstrap_manifest: dict[str, Any] = field(default_factory=dict)
    launch_config: dict[str, Any] = field(default_factory=dict)
    terminate_on_finish: bool = True
    instance_id: str | None = None
    status: str = "pending"
    launched_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "callback_token": self.callback_token,
            "instance_name": self.instance_name,
            "coordinator_url": self.coordinator_url,
            "user_data": self.user_data,
            "bootstrap_manifest": self.bootstrap_manifest,
            "launch_config": self.launch_config,
            "terminate_on_finish": self.terminate_on_finish,
            "instance_id": self.instance_id,
            "status": self.status,
            "launched_at": _serialize_datetime(self.launched_at),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "WorkerLaunchSpec":
        return cls(
            run_id=payload["run_id"],
            callback_token=payload["callback_token"],
            instance_name=payload["instance_name"],
            coordinator_url=payload.get("coordinator_url"),
            user_data=payload.get("user_data"),
            bootstrap_manifest=dict(payload.get("bootstrap_manifest") or {}),
            launch_config=dict(payload.get("launch_config") or {}),
            terminate_on_finish=bool(payload.get("terminate_on_finish", True)),
            instance_id=payload.get("instance_id"),
            status=str(payload.get("status") or "pending"),
            launched_at=_deserialize_datetime(payload.get("launched_at")),
        )


@dataclass
class AutomationRun:
    run_id: str
    issue_key: str
    issue_url: str
    repository: str
    base_branch: str
    task: str
    issue_labels: list[str] = field(default_factory=list)
    callback_url: str | None = None
    test_command: str | None = None
    provider: str | None = None
    model: str | None = None
    create_pr: bool = True
    repository_path: str | None = None
    repository_kind: Literal["auto", "remote", "local"] = "auto"
    setup_mode: Literal["auto", "skip"] = "auto"
    max_repair_attempts: int = 1
    retry_count: int = 0
    max_retries: int = 1
    priority: int = 0
    queue_name: str = "default"
    paused_reason: str | None = None
    paused_from_status: str | None = None
    cancel_requested_at: datetime | None = None
    cancelled_at: datetime | None = None
    status: str = "queued"
    worker_instance_id: str | None = None
    worker_launch_spec: WorkerLaunchSpec | None = None
    callback_token: str = field(default_factory=lambda: uuid4().hex)
    branch_name: str | None = None
    pr_url: str | None = None
    workspace: str | None = None
    failure: str | None = None
    result: str | None = None
    recovery_state: str | None = None
    recovery_reason: str | None = None
    last_recovery_at: datetime | None = None
    orphaned_at: datetime | None = None
    changed_files: list[str] = field(default_factory=list)
    events: list[RunEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=utc_now)
    updated_at: datetime | None = None
    finished_at: datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "issue_key": self.issue_key,
            "issue_url": self.issue_url,
            "issue_labels": self.issue_labels,
            "repository": self.repository,
            "base_branch": self.base_branch,
            "task": self.task,
            "callback_url": self.callback_url,
            "test_command": self.test_command,
            "provider": self.provider,
            "model": self.model,
            "create_pr": self.create_pr,
            "repository_path": self.repository_path,
            "repository_kind": self.repository_kind,
            "setup_mode": self.setup_mode,
            "max_repair_attempts": self.max_repair_attempts,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "priority": self.priority,
            "queue_name": self.queue_name,
            "paused_reason": self.paused_reason,
            "paused_from_status": self.paused_from_status,
            "cancel_requested_at": _serialize_datetime(self.cancel_requested_at),
            "cancelled_at": _serialize_datetime(self.cancelled_at),
            "status": self.status,
            "worker_instance_id": self.worker_instance_id,
            "worker_launch_spec": self.worker_launch_spec.to_json() if self.worker_launch_spec else None,
            "callback_token": self.callback_token,
            "branch_name": self.branch_name,
            "pr_url": self.pr_url,
            "workspace": self.workspace,
            "failure": self.failure,
            "result": self.result,
            "recovery_state": self.recovery_state,
            "recovery_reason": self.recovery_reason,
            "last_recovery_at": _serialize_datetime(self.last_recovery_at),
            "orphaned_at": _serialize_datetime(self.orphaned_at),
            "changed_files": self.changed_files,
            "events": [event.to_json() for event in self.events],
            "created_at": _serialize_datetime(self.created_at),
            "updated_at": _serialize_datetime(self.updated_at),
            "finished_at": _serialize_datetime(self.finished_at),
        }

    @classmethod
    def from_json(cls, payload: dict[str, Any]) -> "AutomationRun":
        return cls(
            run_id=payload["run_id"],
            issue_key=payload["issue_key"],
            issue_url=payload["issue_url"],
            issue_labels=list(payload.get("issue_labels") or []),
            repository=payload["repository"],
            base_branch=payload["base_branch"],
            task=payload["task"],
            callback_url=payload.get("callback_url"),
            test_command=payload.get("test_command"),
            provider=payload.get("provider"),
            model=payload.get("model"),
            create_pr=bool(payload.get("create_pr", True)),
            repository_path=payload.get("repository_path"),
            repository_kind=payload.get("repository_kind", "auto"),
            setup_mode=payload.get("setup_mode", "auto"),
            max_repair_attempts=int(payload.get("max_repair_attempts", 1)),
            retry_count=int(payload.get("retry_count", 0)),
            max_retries=int(payload.get("max_retries", 1)),
            priority=int(payload.get("priority", 0)),
            queue_name=str(payload.get("queue_name") or "default"),
            paused_reason=payload.get("paused_reason"),
            paused_from_status=payload.get("paused_from_status"),
            cancel_requested_at=_deserialize_datetime(payload.get("cancel_requested_at")),
            cancelled_at=_deserialize_datetime(payload.get("cancelled_at")),
            status=str(payload.get("status") or "queued"),
            worker_instance_id=payload.get("worker_instance_id"),
            worker_launch_spec=WorkerLaunchSpec.from_json(payload["worker_launch_spec"]) if payload.get("worker_launch_spec") else None,
            callback_token=payload.get("callback_token") or uuid4().hex,
            branch_name=payload.get("branch_name"),
            pr_url=payload.get("pr_url"),
            workspace=payload.get("workspace"),
            failure=payload.get("failure"),
            result=payload.get("result"),
            recovery_state=payload.get("recovery_state"),
            recovery_reason=payload.get("recovery_reason"),
            last_recovery_at=_deserialize_datetime(payload.get("last_recovery_at")),
            orphaned_at=_deserialize_datetime(payload.get("orphaned_at")),
            changed_files=list(payload.get("changed_files") or []),
            events=[RunEvent.from_json(item) for item in payload.get("events") or []],
            created_at=_deserialize_datetime(payload.get("created_at")) or utc_now(),
            updated_at=_deserialize_datetime(payload.get("updated_at")),
            finished_at=_deserialize_datetime(payload.get("finished_at")),
        )

    def to_run_request(self) -> RunRequest:
        return RunRequest(
            task=self.task,
            task_context=self.task,
            issue_key=self.issue_key,
            issue_url=self.issue_url,
            issue_labels=list(self.issue_labels),
            repository=self.repository,
            base_branch=self.base_branch,
            repository_path=self.repository_path,
            repository_kind=self.repository_kind,
            setup_mode=self.setup_mode,
            test_command=self.test_command,
            create_pr=self.create_pr,
            model=self.model,
            provider=self.provider,
            max_repair_attempts=self.max_repair_attempts,
            reuse_workspace=False,
        )
