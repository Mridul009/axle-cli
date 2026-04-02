from __future__ import annotations

from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    class _MissingRequests:
        def get(self, *_args, **_kwargs):
            raise RuntimeError("The `requests` package is required for GitHub API operations.")

    requests = _MissingRequests()

from .models import SavedConfig
from .state import load_config, save_config


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _github_headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def test_github_token(token: str) -> dict[str, Any]:
    normalized_token = token.strip()
    if not normalized_token:
        raise ValueError("GitHub token cannot be empty.")

    response = requests.get("https://api.github.com/user", headers=_github_headers(normalized_token), timeout=15)
    response.raise_for_status()
    return response.json()


def save_github_settings(token: str | None, repo: str | None, branch: str | None) -> SavedConfig:
    config = load_config()

    next_token = _normalize_optional(token)
    next_repo = _normalize_optional(repo)
    next_branch = _normalize_optional(branch)

    if token is not None:
        if next_token is None:
            raise ValueError("GitHub token cannot be empty.")
        payload = test_github_token(next_token)
        config.github_token = next_token
        config.github_login = payload.get("login")
    if next_repo is not None:
        config.repository = next_repo
    if next_branch is not None:
        config.base_branch = next_branch
    save_config(config)
    return config
