from __future__ import annotations

import difflib
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from .config import settings
from .models import RepositorySource, RunRequest, Workspace, WorkspaceMetadata


class RepoError(RuntimeError):
    pass


WORKSPACE_METADATA_FILENAME = ".axle-workspace.json"
MANAGED_REPO_ARTIFACT_FILES = {
    ".axle-bootstrap.json",
    "db.sqlite3",
}
MANAGED_REPO_ARTIFACT_PARTS = {
    ".venv",
    ".goose",
    "node_modules",
    ".pytest_cache",
    ".ruff_cache",
    ".mypy_cache",
    ".nox",
    ".tox",
    "__pycache__",
}


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


def _slugify(value: str, limit: int = 40) -> str:
    slug = "".join(char.lower() if char.isalnum() else "-" for char in value).strip("-")
    slug = "-".join(part for part in slug.split("-") if part)
    return (slug or "workspace")[:limit]


def _normalize_local_path(path: str) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.exists() or not resolved.is_dir():
        raise RepoError(f"Local repository path is invalid: {resolved}")
    return resolved


def _repository_label(repository: str) -> str:
    parsed = urlparse(repository)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/").removesuffix(".git").removeprefix("/")
        if path:
            parts = path.split("/")
            return parts[-1] or parsed.netloc
        return parsed.netloc
    return Path(repository).expanduser().name or "workspace"


def repository_source(
    repository: str,
    repository_path: str | None = None,
    repository_kind: str = "auto",
) -> RepositorySource:
    explicit_path = repository_path.strip() if repository_path else None
    kind_hint = (repository_kind or "auto").strip().lower()

    if explicit_path:
        path = _normalize_local_path(explicit_path)
        return RepositorySource(value=str(path), kind="local", path=path, label=path.name or "workspace")

    normalized = (repository or "").strip()
    if not normalized:
        raise RepoError("A repository URL or local path is required.")

    if kind_hint == "local" or (kind_hint == "auto" and not looks_like_remote(normalized)):
        path = _normalize_local_path(normalized)
        return RepositorySource(value=str(path), kind="local", path=path, label=path.name or "workspace")

    return RepositorySource(value=normalized, kind="remote", path=None, label=_repository_label(normalized))


def workspace_key_for_source(source: RepositorySource, base_branch: str) -> str:
    branch = (base_branch or "main").strip() or "main"
    digest = hashlib.sha256(f"{source.kind}:{source.normalized_value()}:{branch}".encode("utf-8")).hexdigest()[:12]
    return f"{_slugify(source.display_name())}-{_slugify(branch)}-{digest}"


def goose_session_name_for_workspace(workspace_key: str) -> str:
    return f"axle-{workspace_key}"


def workspace_root_for_source(source: RepositorySource, base_branch: str, run_id: str | None = None, *, reuse_workspace: bool = True) -> Path:
    workspace_key = workspace_key_for_source(source, base_branch)
    if reuse_workspace:
        return settings.workspace_root / workspace_key
    suffix = (run_id or str(uuid4()))[:8]
    return settings.workspace_root / f"{workspace_key}-{suffix}"


def workspace_metadata_path(root: Path) -> Path:
    return root / WORKSPACE_METADATA_FILENAME


def load_workspace_metadata(root: Path) -> WorkspaceMetadata | None:
    path = workspace_metadata_path(root)
    if not path.exists():
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    return WorkspaceMetadata.from_json(payload)


def save_workspace_metadata(root: Path, metadata: WorkspaceMetadata) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = workspace_metadata_path(root)
    path.write_text(json.dumps(metadata.to_json(), indent=2) + "\n", encoding="utf-8")
    return path


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


def clone_or_copy_repo(
    repository: str,
    destination: Path,
    base_branch: str,
    github_token: str | None,
    *,
    repository_path: str | None = None,
    repository_kind: str = "auto",
) -> str:
    source = repository_source(repository, repository_path, repository_kind)

    if source.kind == "remote":
        if destination.exists() and (destination / ".git").exists():
            return source.value
        if destination.exists() and any(destination.iterdir()) and not (destination / ".git").exists():
            raise RepoError(f"Workspace destination already exists and is not a git checkout: {destination}")
        clone_command = [
            shutil.which("git") or "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            base_branch,
            authenticated_clone_url(source.value, github_token),
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
        return source.value

    if source.path is None:
        raise RepoError("Local repository source could not be resolved.")
    if destination.exists() and any(destination.iterdir()):
        return source.value
    try:
        shutil.copytree(
            source.path,
            destination,
            dirs_exist_ok=True,
            ignore=_copytree_ignore(source.path, destination),
        )
    except shutil.Error as exc:
        raise RepoError(f"Failed to copy local repository into workspace: {exc}") from exc
    return source.value


def prepare_workspace(request: RunRequest, github_token: str | None) -> Workspace:
    run_id = str(uuid4())
    source = repository_source(
        request.repository,
        getattr(request, "repository_path", None),
        getattr(request, "repository_kind", "auto"),
    )
    workspace_key = getattr(request, "workspace_key", None) or workspace_key_for_source(source, request.base_branch)
    root = workspace_root_for_source(source, request.base_branch, run_id, reuse_workspace=getattr(request, "reuse_workspace", True))
    repo_dir = root / "repo"
    existing_metadata = load_workspace_metadata(root)
    repo_dir.mkdir(parents=True, exist_ok=True)
    clone_or_copy_repo(
        source.value,
        repo_dir,
        request.base_branch,
        github_token,
        repository_path=str(source.path) if source.path else None,
        repository_kind=source.kind,
    )
    metadata = WorkspaceMetadata(
        workspace_key=workspace_key,
        base_branch=request.base_branch,
        source_value=source.value,
        source_kind=source.kind,
        source_path=str(source.path) if source.path else None,
        run_id=run_id,
        goose_session_name=(existing_metadata.goose_session_name if existing_metadata and existing_metadata.goose_session_name else goose_session_name_for_workspace(workspace_key)),
    )
    metadata_path = save_workspace_metadata(root, metadata)
    return Workspace(
        run_id=run_id,
        workspace_key=workspace_key,
        root=root,
        repo_dir=repo_dir,
        transcript_path=root / "goose-transcript.log",
        prompt_path=root / "goose-prompt.md",
        diff_path=root / "changes.patch",
        test_log_path=root / "test.log",
        metadata_path=metadata_path,
        source_value=source.value,
        source_kind=source.kind,
        source_path=source.path,
        metadata=metadata,
        goose_session_name=metadata.goose_session_name,
    )


def repo_tree(root: Path, limit: int) -> str:
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        normalized = str(rel)
        if ".git" in path.parts:
            continue
        if normalized in MANAGED_REPO_ARTIFACT_FILES:
            continue
        if any(part in MANAGED_REPO_ARTIFACT_PARTS for part in rel.parts):
            continue
        if len(lines) >= limit:
            lines.append("... truncated ...")
            break
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


def source_changed_files(repo_dir: Path) -> list[str]:
    paths = changed_files(repo_dir)
    filtered: list[str] = []
    for path in paths:
        normalized = path.strip()
        if normalized in MANAGED_REPO_ARTIFACT_FILES:
            continue
        parts = Path(normalized).parts
        if any(part in MANAGED_REPO_ARTIFACT_PARTS for part in parts):
            continue
        filtered.append(normalized)
    return filtered


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
