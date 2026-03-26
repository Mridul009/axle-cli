from __future__ import annotations

from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .models import SavedConfig
from .state import load_config, save_config


def _normalize_base_url(base_url: str) -> str:
    return base_url.strip().rstrip("/")


def _jira_auth(email: str, api_token: str) -> HTTPBasicAuth:
    return HTTPBasicAuth(email.strip(), api_token.strip())


def _jira_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
    }


def test_jira_connection(base_url: str, email: str, api_token: str) -> dict[str, Any]:
    response = requests.get(
        f"{_normalize_base_url(base_url)}/rest/api/3/myself",
        headers=_jira_headers(),
        auth=_jira_auth(email, api_token),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def save_jira_settings(base_url: str | None, email: str | None, api_token: str | None, project_key: str | None,) -> SavedConfig:
    config = load_config()
    next_base_url = _normalize_base_url(base_url) if base_url is not None else config.jira_base_url
    next_email = email.strip() if email is not None else config.jira_email
    next_api_token = api_token.strip() if api_token is not None else config.jira_api_token

    if any(value is not None for value in (base_url, email, api_token)):
        if not all((next_base_url, next_email, next_api_token)):
            raise ValueError("Jira auth requires --base-url, --email, and --api-token together.")
        test_jira_connection(next_base_url, next_email, next_api_token)
        config.jira_base_url = next_base_url
        config.jira_email = next_email
        config.jira_api_token = next_api_token

    if project_key is not None:
        config.jira_project_key = project_key.strip() or None

    save_config(config)
    return config
