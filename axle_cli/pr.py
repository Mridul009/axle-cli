from __future__ import annotations

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    class _MissingRequests:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("The `requests` package is required for GitHub PR creation.")

    requests = _MissingRequests()

from .repo import RepoError, extract_github_repo


def build_pr_body(
    task: str,
    *,
    issue_key: str | None = None,
    issue_url: str | None = None,
    issue_labels: list[str] | None = None,
    changed_files: list[str] | None = None,
    test_command: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    lines = ["Created by Axle CLI.", "", "## Summary", "", task.strip()]

    metadata: list[str] = []
    if issue_key:
        issue_line = f"- Ticket: {issue_key}"
        if issue_url and issue_url != issue_key:
            issue_line = f"- Ticket: [{issue_key}]({issue_url})"
        metadata.append(issue_line)
    elif issue_url:
        metadata.append(f"- Ticket: {issue_url}")
    if issue_labels:
        metadata.append(f"- Labels: {', '.join(issue_labels)}")
    if provider or model:
        metadata.append(f"- Model: {(provider or 'unknown').strip()} / {(model or 'unknown').strip()}")
    if test_command:
        metadata.append(f"- Validation: `{test_command}`")
    if metadata:
        lines.extend(["", "## Context", ""])
        lines.extend(metadata)

    lines.extend(["", "## Changed Files", ""])
    if changed_files:
        lines.extend(f"- `{path}`" for path in changed_files)
    else:
        lines.append("- None recorded")
    return "\n".join(lines).strip()


def create_pull_request(
    repository: str,
    branch_name: str,
    base_branch: str,
    title: str,
    task: str,
    github_token: str | None,
    *,
    issue_key: str | None = None,
    issue_url: str | None = None,
    issue_labels: list[str] | None = None,
    changed_files: list[str] | None = None,
    test_command: str | None = None,
    provider: str | None = None,
    model: str | None = None,
) -> str:
    if not github_token:
        raise RepoError("GitHub token is required for PR creation.")
    repo_parts = extract_github_repo(repository)
    if repo_parts is None:
        raise RepoError("PR creation currently supports GitHub HTTPS repositories only.")
    owner, repo = repo_parts
    response = requests.post(
        f"https://api.github.com/repos/{owner}/{repo}/pulls",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {github_token}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={
            "title": title,
            "head": branch_name,
            "base": base_branch,
            "body": build_pr_body(
                task,
                issue_key=issue_key,
                issue_url=issue_url,
                issue_labels=issue_labels,
                changed_files=changed_files,
                test_command=test_command,
                provider=provider,
                model=model,
            ),
        },
        timeout=30,
    )
    if not response.ok:
        raise RepoError(f"GitHub PR creation failed with status {response.status_code}: {response.text[:500]}")
    return response.json()["html_url"]
