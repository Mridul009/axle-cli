from __future__ import annotations

from collections.abc import Iterable
from typing import Any

import requests
from requests.auth import HTTPBasicAuth

from .models import SavedConfig
from .state import load_config, save_config


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _normalize_base_url(base_url: str) -> str:
    normalized = base_url.strip().rstrip("/")
    return normalized


def _jira_auth(email: str, api_token: str) -> HTTPBasicAuth:
    return HTTPBasicAuth(email.strip(), api_token.strip())


def _jira_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
    }


def test_jira_connection(base_url: str, email: str, api_token: str) -> dict[str, Any]:
    normalized_base_url = _normalize_base_url(base_url)
    if not normalized_base_url:
        raise ValueError("Jira base URL cannot be empty.")
    normalized_email = email.strip()
    if not normalized_email:
        raise ValueError("Jira email cannot be empty.")
    normalized_api_token = api_token.strip()
    if not normalized_api_token:
        raise ValueError("Jira API token cannot be empty.")

    response = requests.get(
        f"{normalized_base_url}/rest/api/3/myself",
        headers=_jira_headers(),
        auth=_jira_auth(normalized_email, normalized_api_token),
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def _require_jira_credentials(config: SavedConfig) -> tuple[str, str, str]:
    base_url = _normalize_optional(config.jira_base_url)
    email = _normalize_optional(config.jira_email)
    api_token = _normalize_optional(config.jira_api_token)
    if not all((base_url, email, api_token)):
        raise ValueError("Jira is not fully configured. Save --base-url, --email, and --api-token first.")
    return base_url, email, api_token


def _adf_text(node: Any) -> str:
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        parts = [_adf_text(item) for item in node]
        return "\n".join(part for part in parts if part).strip()
    if not isinstance(node, dict):
        return ""

    node_type = node.get("type")
    content = node.get("content", [])

    if node_type == "text":
        text = node.get("text", "")
        marks = node.get("marks", [])
        for mark in marks:
            mark_type = mark.get("type")
            if mark_type == "code":
                text = f"`{text}`"
            if mark_type == "strong":
                text = f"**{text}**"
        return text

    if node_type in {"doc", "paragraph"}:
        return "\n".join(part for part in (_adf_text(item) for item in content) if part).strip()
    if node_type in {"bulletList", "orderedList"}:
        items: list[str] = []
        for item in content:
            rendered = _adf_text(item).strip()
            if rendered:
                items.append(f"- {rendered}")
        return "\n".join(items)
    if node_type == "listItem":
        return "\n".join(part for part in (_adf_text(item) for item in content) if part).strip()
    if node_type in {"hardBreak", "rule"}:
        return "\n"
    if node_type == "heading":
        return "\n".join(part for part in (_adf_text(item) for item in content) if part).strip()
    if node_type == "codeBlock":
        body = "\n".join(part for part in (_adf_text(item) for item in content) if part).strip()
        return f"```\n{body}\n```" if body else ""

    if isinstance(content, Iterable):
        return "\n".join(part for part in (_adf_text(item) for item in content) if part).strip()
    return ""


def fetch_jira_issue(config: SavedConfig, issue_key: str) -> dict[str, Any]:
    normalized_issue_key = issue_key.strip().upper()
    if not normalized_issue_key:
        raise ValueError("A Jira issue key is required.")

    base_url, email, api_token = _require_jira_credentials(config)
    response = requests.get(
        f"{base_url}/rest/api/3/issue/{normalized_issue_key}",
        headers=_jira_headers(),
        auth=_jira_auth(email, api_token),
        params={
            "fields": "summary,description,comment",
        },
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    fields = payload.get("fields", {})
    comment_block = fields.get("comment") or {}
    raw_comments = comment_block.get("comments") or []
    comments: list[dict[str, str]] = []
    for item in raw_comments:
        body = _adf_text(item.get("body")).strip()
        comments.append(
            {
                "author": ((item.get("author") or {}).get("displayName") or "Unknown").strip(),
                "created": (item.get("created") or "").strip(),
                "body": body or "(no text)",
            }
        )

    return {
        "key": payload.get("key", normalized_issue_key),
        "summary": (fields.get("summary") or "").strip() or "(no summary)",
        "description": _adf_text(fields.get("description")).strip() or "(no description)",
        "comments": comments,
        "url": f"{base_url}/browse/{payload.get('key', normalized_issue_key)}",
    }


def format_jira_issue(issue: dict[str, Any]) -> str:
    lines = [
        f"Issue: {issue['key']}",
        f"URL: {issue['url']}",
        f"Summary: {issue['summary']}",
        "",
        "Description:",
        issue["description"],
        "",
        "Comments:",
    ]
    comments = issue.get("comments") or []
    if not comments:
        lines.append("- none")
    else:
        for comment in comments:
            header = comment["author"]
            if comment["created"]:
                header = f"{header} at {comment['created']}"
            lines.append(f"- {header}")
            lines.append(comment["body"])
            lines.append("")
    return "\n".join(lines).strip()


def jira_issue_task_text(issue: dict[str, Any], extra_instructions: str | None = None) -> str:
    lines = [
        f"Jira issue: {issue['key']}",
        f"Summary: {issue['summary']}",
        "",
        "Description:",
        issue["description"],
    ]
    comments = issue.get("comments") or []
    if comments:
        lines.extend(["", "Comments:"])
        for comment in comments:
            header = comment["author"]
            if comment["created"]:
                header = f"{header} at {comment['created']}"
            lines.append(f"- {header}: {comment['body']}")
    if extra_instructions and extra_instructions.strip():
        lines.extend(["", "Additional operator instructions:", extra_instructions.strip()])
    return "\n".join(lines).strip()


def save_jira_settings(
    base_url: str | None,
    email: str | None,
    api_token: str | None,
    project_key: str | None,
) -> SavedConfig:
    config = load_config()
    next_base_url = _normalize_base_url(base_url) if base_url is not None else config.jira_base_url
    next_email = _normalize_optional(email) if email is not None else config.jira_email
    next_api_token = _normalize_optional(api_token) if api_token is not None else config.jira_api_token

    if any(value is not None for value in (base_url, email, api_token)):
        if not all((next_base_url, next_email, next_api_token)):
            raise ValueError("Jira auth requires --base-url, --email, and --api-token together.")
        test_jira_connection(next_base_url, next_email, next_api_token)
        config.jira_base_url = next_base_url
        config.jira_email = next_email
        config.jira_api_token = next_api_token

    next_project_key = _normalize_optional(project_key)
    if project_key is not None:
        config.jira_project_key = next_project_key

    save_config(config)
    return config
