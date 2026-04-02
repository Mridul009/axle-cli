from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    cli_home: Path
    workspace_root: Path
    git_timeout_seconds: int
    test_timeout_seconds: int
    goose_timeout_seconds: int
    default_base_branch: str
    default_goose_binary: str
    default_goose_provider: str
    default_goose_model: str
    git_author_name: str
    git_author_email: str
    max_repair_attempts: int
    repo_tree_limit: int
    diff_excerpt_lines: int
    coordinator_run_store: str | None
    coordinator_sqlite_path: Path | None


def _path_from_env(name: str, default: str) -> Path:
    return Path(os.getenv(name, default)).expanduser().resolve()


def _optional_path_from_env(name: str) -> Path | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    return Path(value).expanduser().resolve()


settings = Settings(
    cli_home=_path_from_env("AXLE_CLI_HOME", str(PROJECT_ROOT / ".axle-cli")),
    workspace_root=_path_from_env("AXLE_CLI_WORKSPACES", str(PROJECT_ROOT / ".axle-cli" / "workspaces")),
    git_timeout_seconds=int(os.getenv("AXLE_CLI_GIT_TIMEOUT_SECONDS", "120")),
    test_timeout_seconds=int(os.getenv("AXLE_CLI_TEST_TIMEOUT_SECONDS", "300")),
    goose_timeout_seconds=int(os.getenv("AXLE_CLI_GOOSE_TIMEOUT_SECONDS", "900")),
    default_base_branch=os.getenv("AXLE_CLI_DEFAULT_BASE_BRANCH", "main"),
    default_goose_binary=os.getenv("GOOSE_BINARY", "goose"),
    default_goose_provider=os.getenv("GOOSE_PROVIDER", os.getenv("OPENAI_PROVIDER", "openai")),
    default_goose_model=os.getenv("GOOSE_MODEL", os.getenv("OPENAI_DEFAULT_MODEL", "gpt-5-mini")),
    git_author_name=os.getenv("GIT_AUTHOR_NAME", "Axle CLI"),
    git_author_email=os.getenv("GIT_AUTHOR_EMAIL", "axle-cli@example.com"),
    max_repair_attempts=int(os.getenv("AXLE_CLI_MAX_REPAIR_ATTEMPTS", "1")),
    repo_tree_limit=int(os.getenv("AXLE_CLI_REPO_TREE_LIMIT", "200")),
    diff_excerpt_lines=int(os.getenv("AXLE_CLI_DIFF_EXCERPT_LINES", "120")),
    coordinator_run_store=(os.getenv("AXLE_RUN_STORE", "").strip() or None),
    coordinator_sqlite_path=_optional_path_from_env("AXLE_SQLITE_PATH"),
)
