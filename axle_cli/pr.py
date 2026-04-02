from __future__ import annotations

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    class _MissingRequests:
        def post(self, *_args, **_kwargs):
            raise RuntimeError("The `requests` package is required for GitHub PR creation.")

    requests = _MissingRequests()

from .repo import RepoError, extract_github_repo


def create_pull_request(
    repository: str,
    branch_name: str,
    base_branch: str,
    title: str,
    task: str,
    github_token: str | None,
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
            "body": f"Created by Axle CLI.\n\nTask:\n{task}",
        },
        timeout=30,
    )
    if not response.ok:
        raise RepoError(f"GitHub PR creation failed with status {response.status_code}: {response.text[:500]}")
    return response.json()["html_url"]
