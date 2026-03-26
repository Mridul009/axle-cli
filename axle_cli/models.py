from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


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
    goose_binary: str = "goose"
    goose_builtin: str | None = None
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
    test_command: str | None = None
    create_pr: bool = False
    model: str | None = None
    provider: str | None = None
    max_repair_attempts: int = 1


@dataclass
class Workspace:
    run_id: str
    root: Path
    repo_dir: Path
    transcript_path: Path
    prompt_path: Path
    diff_path: Path
    test_log_path: Path


@dataclass
class GooseRunResult:
    command: list[str]
    exit_code: int
    transcript_path: Path
    changed_files: list[str]
    diff_text: str
    summary: str


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
    risks: list[str] = field(default_factory=list)
    failure: str | None = None
    started_at: datetime = field(default_factory=utc_now)
    finished_at: datetime | None = None
