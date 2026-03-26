from __future__ import annotations

import difflib
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .config import settings
from .models import RunRequest, Workspace


class RepoError(RuntimeError):
    pass


def _should_skip_local_copy(path: Path, source: Path, destination: Path) -> bool:
    if path == destination or path.is_relative_to(destination):
        return True
    for managed_root in (settings.cli_home, settings.workspace_root):
        if (path == managed_root or path.is_relative_to(managed_root)) and managed_root.is_relative_to(source):
            return True
    return False


def _copytree_ignore(source: Path, destination: Path):
    def ignore(current_dir: str, names: list[str]) -> list[str]:
        current_path = Path(current_dir).resolve()
        skipped: list[str] = []
        for name in names:
            candidate = current_path / name
            if _should_skip_local_copy(candidate, source, destination):
                skipped.append(name)
        return skipped

    return ignore


def looks_like_remote(repository: str) -> bool:
    parsed = urlparse(repository)
    return bool(parsed.scheme and parsed.netloc) or repository.startswith("git@")


def is_github_https(repository: str) -> bool:
    parsed = urlparse(repository)
    return parsed.scheme in {"http", "https"} and parsed.netloc.lower() == "github.com"


def authenticated_clone_url(repository: str, github_token: str | None) -> str:
    if not is_github_https(repository) or not github_token:
        return repository
    parsed = urlparse(repository)
    return parsed._replace(netloc=f"x-access-token:{github_token}@{parsed.netloc}").geturl()


def extract_github_repo(repository: str) -> tuple[str, str] | None:
    parsed = urlparse(repository)
    if parsed.netloc.lower() != "github.com":
        return None
    path = parsed.path.removeprefix("/").removesuffix(".git")
    parts = path.split("/")
    if len(parts) < 2:
        return None
    return parts[0], parts[1]


def run_git(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    git_bin = shutil.which("git")
    if git_bin is None:
        raise RepoError("git is not installed.")
    result = subprocess.run(
        [git_bin, *command],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=settings.git_timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        raise RepoError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(command)} failed")
    return result


def clone_or_copy_repo(repository: str, destination: Path, base_branch: str, github_token: str | None) -> str:
    if looks_like_remote(repository):
        clone_command = [
            shutil.which("git") or "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            base_branch,
            authenticated_clone_url(repository, github_token),
            str(destination),
        ]
        result = subprocess.run(
            clone_command,
            capture_output=True,
            text=True,
            timeout=settings.git_timeout_seconds,
            check=False,
        )
        if result.returncode != 0:
            raise RepoError(result.stderr.strip() or result.stdout.strip() or "git clone failed")
        return repository
    source = Path(repository).expanduser().resolve()
    if not source.exists() or not source.is_dir():
        raise RepoError(f"Local repository path is invalid: {source}")
    try:
        shutil.copytree(
            source,
            destination,
            dirs_exist_ok=True,
            ignore=_copytree_ignore(source, destination),
        )
    except shutil.Error as exc:
        raise RepoError(f"Failed to copy local repository into workspace: {exc}") from exc
    return str(source)


def prepare_workspace(request: RunRequest, github_token: str | None) -> Workspace:
    run_id = str(uuid4())
    root = settings.workspace_root / run_id
    if root.exists():
        shutil.rmtree(root)
    repo_dir = root / "repo"
    repo_dir.mkdir(parents=True, exist_ok=True)
    clone_or_copy_repo(request.repository, repo_dir, request.base_branch, github_token)
    return Workspace(
        run_id=run_id,
        root=root,
        repo_dir=repo_dir,
        transcript_path=root / "goose-transcript.log",
        prompt_path=root / "goose-prompt.md",
        diff_path=root / "changes.patch",
        test_log_path=root / "test.log",
    )


def repo_tree(root: Path, limit: int) -> str:
    lines: list[str] = []
    for index, path in enumerate(sorted(root.rglob("*"))):
        if ".git" in path.parts:
            continue
        if index >= limit:
            lines.append("... truncated ...")
            break
        rel = path.relative_to(root)
        lines.append(f"{rel}{'/' if path.is_dir() else ''}")
    return "\n".join(lines)


def branch_name_for(title: str, run_id: str) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in title).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)[:48] or "task"
    return f"axle/{slug}-{run_id[:8]}"


def changed_files(repo_dir: Path) -> list[str]:
    result = run_git(["status", "--short"], repo_dir)
    paths: list[str] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        paths.append(line[3:].strip())
    return paths


def diff_text(repo_dir: Path) -> str:
    return run_git(["diff", "--", "."], repo_dir).stdout


def diff_against_snapshot(before: dict[str, str], repo_dir: Path) -> str:
    diffs: list[str] = []
    for relative_path, before_content in before.items():
        path = repo_dir / relative_path
        after_content = path.read_text(encoding="utf-8") if path.exists() else ""
        if before_content == after_content:
            continue
        diff = difflib.unified_diff(
            before_content.splitlines(),
            after_content.splitlines(),
            fromfile=f"a/{relative_path}",
            tofile=f"b/{relative_path}",
            lineterm="",
        )
        diffs.append("\n".join(diff))
    return "\n\n".join(item for item in diffs if item).strip() + ("\n" if diffs else "")


def snapshot_text_files(repo_dir: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(repo_dir.rglob("*")):
        if ".git" in path.parts or not path.is_file():
            continue
        try:
            snapshot[str(path.relative_to(repo_dir))] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
    return snapshot


def commit_and_push(
    repo_dir: Path,
    branch_name: str,
    commit_message: str,
    github_token: str | None,
    repository: str,
    git_author_name: str,
    git_author_email: str,
) -> list[str]:
    if not github_token:
        raise RepoError("GitHub token is required for push.")
    if not looks_like_remote(repository):
        raise RepoError("Push is only supported for remote repositories.")
    run_git(["config", "user.name", git_author_name], repo_dir)
    run_git(["config", "user.email", git_author_email], repo_dir)
    run_git(["checkout", "-b", branch_name], repo_dir)
    run_git(["add", "-A"], repo_dir)
    staged = [line.strip() for line in run_git(["diff", "--cached", "--name-only"], repo_dir).stdout.splitlines() if line.strip()]
    if not staged:
        raise RepoError("No repository changes were produced.")
    run_git(["commit", "-m", commit_message], repo_dir)
    run_git(["push", "-u", "origin", branch_name], repo_dir)
    return staged
