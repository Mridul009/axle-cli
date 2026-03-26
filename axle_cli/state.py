from __future__ import annotations

import json
from pathlib import Path

from .config import settings
from .models import SavedConfig


def config_path() -> Path:
    return settings.cli_home / "config.json"


def ensure_cli_home() -> None:
    settings.cli_home.mkdir(parents=True, exist_ok=True)
    settings.workspace_root.mkdir(parents=True, exist_ok=True)


def load_config() -> SavedConfig:
    ensure_cli_home()
    path = config_path()
    if not path.exists():
        return SavedConfig(
            base_branch=settings.default_base_branch,
            goose_binary=settings.default_goose_binary,
            llm_provider=settings.default_goose_provider,
            llm_model=settings.default_goose_model,
            git_author_name=settings.git_author_name,
            git_author_email=settings.git_author_email,
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = SavedConfig.from_json(payload)
    if not config.base_branch:
        config.base_branch = settings.default_base_branch
    return config


def save_config(config: SavedConfig) -> Path:
    ensure_cli_home()
    path = config_path()
    path.write_text(json.dumps(config.to_json(), indent=2) + "\n", encoding="utf-8")
    return path
