from __future__ import annotations

from typing import Any

import requests

from .models import SavedConfig
from .state import load_config, save_config


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def test_github_token(token: str) -> dict[str, Any]:
    response = requests.get("https://api.github.com/user", headers=_github_headers(token), timeout=15)
    response.raise_for_status()
    return response.json()


def save_github_settings(token: str | None, repo: str | None, branch: str | None) -> SavedConfig:
    config = load_config()
    if token is not None:
        payload = test_github_token(token.strip())
        config.github_token = token.strip()
        config.github_login = payload.get("login")
    if repo is not None:
        config.repository = repo.strip()
    if branch is not None:
        config.base_branch = branch.strip()
    save_config(config)
    return config
