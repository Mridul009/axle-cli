from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
