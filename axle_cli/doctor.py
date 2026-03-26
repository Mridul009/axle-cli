from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse
from typing import Literal

from .models import SavedConfig


DoctorLevel = Literal["ok", "warn", "fail"]


@dataclass
class DoctorCheck:
    name: str
    level: DoctorLevel
    message: str
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class DoctorReport:
    checks: list[DoctorCheck] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(check.level != "fail" for check in self.checks)

    def add(self, name: str, level: DoctorLevel, message: str, **details: str) -> None:
        self.checks.append(
            DoctorCheck(
                name=name,
                level=level,
                message=message,
                details={key: value for key, value in details.items() if value},
            )
        )


def _binary_version(binary: str) -> str | None:
    resolved = shutil.which(binary)
    if not resolved:
        return None
    try:
        result = subprocess.run(
            [resolved, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except OSError:
        return None
    output = (result.stdout or result.stderr or "").strip()
    return output.splitlines()[0] if output else None


def _provider_api_key(config: SavedConfig) -> tuple[str | None, str | None]:
    provider = (config.llm_provider or "").strip().lower()
    candidates: list[tuple[str, str | None]] = [
        ("OPENAI_API_KEY", config.llm_api_key if provider in {"openai", "openrouter"} else None),
        ("OPENROUTER_API_KEY", config.llm_api_key if provider == "openrouter" else None),
        ("ANTHROPIC_API_KEY", config.llm_api_key if provider == "anthropic" else None),
        ("AZURE_OPENAI_API_KEY", config.llm_api_key if provider in {"azure", "azure_openai"} else None),
    ]
    for env_name, fallback in candidates:
        value = os.getenv(env_name) or fallback
        if value and value.strip():
            return env_name, value.strip()
    if config.llm_api_key and config.llm_api_key.strip():
        return "OPENAI_API_KEY", config.llm_api_key.strip()
    return None, None


def run_doctor(config: SavedConfig) -> DoctorReport:
    report = DoctorReport()

    report.add(
        "Saved config",
        "ok",
        "Loaded local CLI configuration.",
        repository=config.repository or "not configured",
        base_branch=config.base_branch,
    )

    goose_binary = config.goose_binary or "goose"
    goose_path = shutil.which(goose_binary)
    if goose_path:
        version = _binary_version(goose_binary)
        report.add(
            "Goose binary",
            "ok",
            f"Found Goose at `{goose_path}`.",
            version=version or "unknown",
        )
    else:
        report.add(
            "Goose binary",
            "fail",
            f"Goose binary `{goose_binary}` was not found on PATH.",
        )

    builtins = config.effective_goose_builtins()
    raw_builtins = (config.goose_builtin or "").strip()
    if builtins:
        details = {"builtins": builtins}
        if raw_builtins and raw_builtins != builtins:
            ignored = [
                item.strip()
                for item in raw_builtins.split(",")
                if item.strip() and item.strip().lower() == "github"
            ]
            if ignored:
                details["ignored"] = ",".join(ignored)
        report.add("Goose builtins", "ok", "Effective Goose builtins are configured.", **details)
    else:
        report.add("Goose builtins", "warn", "No Goose builtins are configured.")

    if config.github_token:
        report.add("GitHub", "ok", "GitHub token is saved locally.", repository=config.repository or "not configured")
    else:
        report.add("GitHub", "fail", "GitHub token is missing from local config.")

    if config.repository:
        parsed = urlparse(config.repository)
        if parsed.scheme and parsed.netloc:
            report.add("Repository", "ok", "Default repository is configured.", repository=config.repository)
        else:
            repo_path = Path(config.repository).expanduser()
            if repo_path.exists():
                report.add("Repository", "ok", "Local repository path exists.", repository=str(repo_path))
            else:
                report.add("Repository", "fail", "Configured local repository path does not exist.", repository=str(repo_path))
    else:
        report.add("Repository", "warn", "No default repository is configured.")

    jira_fields = {
        "base_url": (config.jira_base_url or "").strip(),
        "email": (config.jira_email or "").strip(),
        "api_token": (config.jira_api_token or "").strip(),
        "project": (config.jira_project_key or "").strip(),
    }
    configured_jira = [name for name, value in jira_fields.items() if value]
    if not configured_jira:
        report.add("Jira", "warn", "Jira is not configured.")
    elif all(jira_fields[key] for key in ("base_url", "email", "api_token")):
        report.add(
            "Jira",
            "ok",
            "Jira credentials are saved locally.",
            base_url=jira_fields["base_url"],
            email=jira_fields["email"],
            project=jira_fields["project"] or "not configured",
        )
    else:
        report.add(
            "Jira",
            "fail",
            "Jira config is incomplete. Save base URL, email, and API token together.",
            base_url=jira_fields["base_url"] or "missing",
            email=jira_fields["email"] or "missing",
            api_token="present" if jira_fields["api_token"] else "missing",
            project=jira_fields["project"] or "not configured",
        )

    provider = (config.llm_provider or "openai").strip().lower()
    env_name, api_key = _provider_api_key(config)
    if api_key:
        report.add(
            "LLM provider",
            "ok",
            "A provider API key is available without making a network call.",
            provider=provider,
            env=env_name or "unknown",
            model=config.llm_model or "not configured",
            base_url=config.llm_base_url or "not configured",
        )
    else:
        report.add(
            "LLM provider",
            "fail",
            "No provider API key was found in config or environment.",
            provider=provider,
            model=config.llm_model or "not configured",
            base_url=config.llm_base_url or "not configured",
        )

    return report
