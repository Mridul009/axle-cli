from __future__ import annotations

import os
import subprocess
import sys
import json
import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping


class BootstrapError(RuntimeError):
    pass


@dataclass(frozen=True)
class PythonProjectSnapshot:
    repo_dir: Path
    markers: tuple[str, ...]
    dependency_files: tuple[Path, ...]

    @property
    def is_python_project(self) -> bool:
        return bool(self.markers)


@dataclass(frozen=True)
class NodeProjectSnapshot:
    repo_dir: Path
    markers: tuple[str, ...]
    package_manager: str

    @property
    def is_node_project(self) -> bool:
        return bool(self.markers)


@dataclass(frozen=True)
class BootstrapPlan:
    repo_dir: Path
    venv_dir: Path
    python_executable: str
    snapshot: PythonProjectSnapshot
    node_snapshot: NodeProjectSnapshot | None = None
    project_kind: str = "none"
    create_venv_command: tuple[str, ...] = field(default_factory=tuple)
    dependency_commands: tuple[tuple[str, ...], ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class BootstrapExecution:
    plan: BootstrapPlan
    executed_commands: tuple[tuple[str, ...], ...]

    @property
    def created_venv(self) -> bool:
        return bool(self.executed_commands)


@dataclass(frozen=True)
class WorkspaceRuntime:
    bootstrap: BootstrapExecution
    command_env: dict[str, str]
    test_command: str | None


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
BOOTSTRAP_STATE_FILENAME = ".axle-bootstrap.json"


_PYTHON_MARKERS = (
    "requirements.txt",
    "requirements-dev.txt",
    "pyproject.toml",
    "setup.py",
    "setup.cfg",
    "Pipfile",
    "manage.py",
)

_NODE_MARKERS = (
    "package.json",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "bun.lockb",
)


def venv_bin_dir(venv_dir: Path) -> Path:
    return venv_dir / ("Scripts" if os.name == "nt" else "bin")


def venv_python_path(venv_dir: Path) -> Path:
    return venv_bin_dir(venv_dir) / ("python.exe" if os.name == "nt" else "python")


def _discover_markers(repo_dir: Path) -> tuple[str, ...]:
    return tuple(marker for marker in _PYTHON_MARKERS if (repo_dir / marker).exists())


def _discover_dependency_files(repo_dir: Path) -> tuple[Path, ...]:
    requirements = sorted(
        (path for path in repo_dir.glob("requirements*.txt") if path.is_file()),
        key=lambda path: (0 if path.name == "requirements.txt" else 1, path.name),
    )
    return tuple(requirements)


def _discover_node_markers(repo_dir: Path) -> tuple[str, ...]:
    return tuple(marker for marker in _NODE_MARKERS if (repo_dir / marker).exists())


def inspect_python_project(repo_dir: Path) -> PythonProjectSnapshot:
    repo_dir = repo_dir.expanduser().resolve()
    return PythonProjectSnapshot(
        repo_dir=repo_dir,
        markers=_discover_markers(repo_dir),
        dependency_files=_discover_dependency_files(repo_dir),
    )


def inspect_node_project(repo_dir: Path) -> NodeProjectSnapshot:
    repo_dir = repo_dir.expanduser().resolve()
    markers = _discover_node_markers(repo_dir)
    if not (repo_dir / "package.json").exists():
        return NodeProjectSnapshot(repo_dir=repo_dir, markers=tuple(), package_manager="npm")
    if (repo_dir / "pnpm-lock.yaml").exists():
        manager = "pnpm"
    elif (repo_dir / "yarn.lock").exists():
        manager = "yarn"
    elif (repo_dir / "bun.lockb").exists():
        manager = "bun"
    else:
        manager = "npm"
    return NodeProjectSnapshot(repo_dir=repo_dir, markers=markers, package_manager=manager)


def _is_packaging_project(snapshot: PythonProjectSnapshot) -> bool:
    return any((snapshot.repo_dir / name).exists() for name in ("pyproject.toml", "setup.py", "setup.cfg"))


def _detect_node_install_command(snapshot: NodeProjectSnapshot) -> tuple[str, ...]:
    repo_dir = snapshot.repo_dir
    if snapshot.package_manager == "pnpm":
        if (repo_dir / "pnpm-lock.yaml").exists():
            return ("pnpm", "install", "--frozen-lockfile")
        return ("pnpm", "install")
    if snapshot.package_manager == "yarn":
        if (repo_dir / "yarn.lock").exists():
            return ("yarn", "install", "--frozen-lockfile")
        return ("yarn", "install")
    if snapshot.package_manager == "bun":
        return ("bun", "install")
    if (repo_dir / "package-lock.json").exists() or (repo_dir / "npm-shrinkwrap.json").exists():
        return ("npm", "ci")
    return ("npm", "install")


def build_python_bootstrap_plan(
    repo_dir: Path,
    *,
    python_executable: str = sys.executable,
    venv_dir: Path | None = None,
) -> BootstrapPlan:
    snapshot = inspect_python_project(repo_dir)
    repo_dir = snapshot.repo_dir
    venv_dir = (venv_dir or repo_dir / ".venv").expanduser().resolve()

    create_venv_command = (python_executable, "-m", "venv", str(venv_dir))
    dependency_commands: list[tuple[str, ...]] = []
    venv_python = str(venv_python_path(venv_dir))

    for dependency_file in snapshot.dependency_files:
        dependency_commands.append((venv_python, "-m", "pip", "install", "-r", dependency_file.name))

    if _is_packaging_project(snapshot):
        dependency_commands.append((venv_python, "-m", "pip", "install", "-e", "."))
    elif "manage.py" in snapshot.markers and not snapshot.dependency_files:
        dependency_commands.append((venv_python, "-m", "pip", "install", "django"))

    notes: list[str] = []
    if not snapshot.markers:
        notes.append("No Python project markers were detected.")
    elif not snapshot.dependency_files and not _is_packaging_project(snapshot):
        if "manage.py" in snapshot.markers:
            notes.append("Django project detected without a dependency manifest; bootstrapping with `pip install django`.")
        else:
            notes.append("Python project detected, but no dependency manifest was found.")

    return BootstrapPlan(
        repo_dir=repo_dir,
        venv_dir=venv_dir,
        python_executable=python_executable,
        snapshot=snapshot,
        node_snapshot=None,
        project_kind="python",
        create_venv_command=create_venv_command,
        dependency_commands=tuple(dependency_commands),
        notes=tuple(notes),
    )


def build_node_bootstrap_plan(
    repo_dir: Path,
    *,
    python_executable: str = sys.executable,
    venv_dir: Path | None = None,
) -> BootstrapPlan:
    repo_dir = repo_dir.expanduser().resolve()
    node_snapshot = inspect_node_project(repo_dir)
    venv_dir = (venv_dir or repo_dir / ".venv").expanduser().resolve()
    install_command = _detect_node_install_command(node_snapshot)
    notes: list[str] = []
    if not node_snapshot.is_node_project:
        notes.append("No Node project markers were detected.")
    else:
        notes.append(f"Node project detected; bootstrapping with `{ ' '.join(install_command) }`.")
    return BootstrapPlan(
        repo_dir=repo_dir,
        venv_dir=venv_dir,
        python_executable=python_executable,
        snapshot=PythonProjectSnapshot(repo_dir=repo_dir, markers=tuple(), dependency_files=tuple()),
        node_snapshot=node_snapshot,
        project_kind="node",
        create_venv_command=tuple(),
        dependency_commands=(install_command,) if node_snapshot.is_node_project else tuple(),
        notes=tuple(notes),
    )


def build_workspace_bootstrap_plan(
    repo_dir: Path,
    *,
    python_executable: str = sys.executable,
    venv_dir: Path | None = None,
) -> BootstrapPlan:
    node_snapshot = inspect_node_project(repo_dir)
    python_plan = build_python_bootstrap_plan(repo_dir, python_executable=python_executable, venv_dir=venv_dir)
    if python_plan.snapshot.is_python_project and node_snapshot.is_node_project:
        notes = list(python_plan.notes)
        notes.extend(
            [
                f"Node project detected; bootstrapping with `{ ' '.join(_detect_node_install_command(node_snapshot)) }`.",
            ]
        )
        return BootstrapPlan(
            repo_dir=python_plan.repo_dir,
            venv_dir=python_plan.venv_dir,
            python_executable=python_executable,
            snapshot=python_plan.snapshot,
            node_snapshot=node_snapshot,
            project_kind="mixed",
            create_venv_command=python_plan.create_venv_command,
            dependency_commands=python_plan.dependency_commands + (_detect_node_install_command(node_snapshot),),
            notes=tuple(notes),
        )
    if python_plan.snapshot.is_python_project:
        return python_plan
    if node_snapshot.is_node_project:
        return build_node_bootstrap_plan(repo_dir, python_executable=python_executable, venv_dir=venv_dir)
    return BootstrapPlan(
        repo_dir=repo_dir.expanduser().resolve(),
        venv_dir=(venv_dir or repo_dir / ".venv").expanduser().resolve(),
        python_executable=python_executable,
        snapshot=python_plan.snapshot,
        node_snapshot=node_snapshot,
        project_kind="none",
        create_venv_command=tuple(),
        dependency_commands=tuple(),
        notes=("No Python or Node project markers were detected.",),
    )


def _default_env(venv_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    bin_dir = venv_bin_dir(venv_dir)
    env["VIRTUAL_ENV"] = str(venv_dir)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return env


def workspace_command_env(repo_dir: Path, *, venv_dir: Path | None = None) -> dict[str, str]:
    resolved_repo = repo_dir.expanduser().resolve()
    resolved_venv = (venv_dir or resolved_repo / ".venv").expanduser().resolve()
    if venv_python_path(resolved_venv).exists():
        return _default_env(resolved_venv)
    return os.environ.copy()


def _state_path(repo_dir: Path) -> Path:
    return repo_dir / BOOTSTRAP_STATE_FILENAME


def _plan_fingerprint(plan: BootstrapPlan) -> str:
    payload = {
        "repo_dir": str(plan.repo_dir),
        "venv_dir": str(plan.venv_dir),
        "project_kind": plan.project_kind,
        "create_venv_command": list(plan.create_venv_command),
        "dependency_commands": [list(item) for item in plan.dependency_commands],
        "dependency_files": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in plan.snapshot.dependency_files
            if path.exists()
        },
        "markers": list(plan.snapshot.markers),
        "node_markers": list(plan.node_snapshot.markers) if plan.node_snapshot else [],
        "package_manager": plan.node_snapshot.package_manager if plan.node_snapshot else None,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def _load_state(repo_dir: Path) -> dict[str, str] | None:
    path = _state_path(repo_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _save_state(repo_dir: Path, plan: BootstrapPlan) -> None:
    _state_path(repo_dir).write_text(
        json.dumps({"fingerprint": _plan_fingerprint(plan)}, indent=2) + "\n",
        encoding="utf-8",
    )


def bootstrap_is_current(plan: BootstrapPlan) -> bool:
    state = _load_state(plan.repo_dir)
    if state is None:
        return False
    if plan.project_kind in {"python", "mixed"} and not venv_python_path(plan.venv_dir).exists():
        return False
    return state.get("fingerprint") == _plan_fingerprint(plan)


def _run_command(
    runner: CommandRunner,
    command: tuple[str, ...],
    *,
    cwd: Path,
    env: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    result = runner(
        list(command),
        cwd=cwd,
        env=dict(env),
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        detail = stderr or stdout or "bootstrap command failed"
        raise BootstrapError(f"{' '.join(command)} failed: {detail}")
    return result


def bootstrap_python_workspace(
    repo_dir: Path,
    *,
    python_executable: str = sys.executable,
    venv_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> BootstrapExecution:
    plan = build_workspace_bootstrap_plan(
        repo_dir,
        python_executable=python_executable,
        venv_dir=venv_dir,
    )

    if plan.project_kind == "none":
        return BootstrapExecution(plan=plan, executed_commands=tuple())

    if bootstrap_is_current(plan):
        return BootstrapExecution(plan=plan, executed_commands=tuple())

    executed: list[tuple[str, ...]] = []
    if plan.project_kind in {"python", "mixed"}:
        plan.venv_dir.mkdir(parents=True, exist_ok=True)
        env = _default_env(plan.venv_dir)
    else:
        env = os.environ.copy()

    if plan.create_venv_command:
        _run_command(runner, plan.create_venv_command, cwd=plan.repo_dir, env=env)
        executed.append(plan.create_venv_command)

    for command in plan.dependency_commands:
        _run_command(runner, command, cwd=plan.repo_dir, env=env)
        executed.append(command)

    _save_state(plan.repo_dir, plan)

    return BootstrapExecution(plan=plan, executed_commands=tuple(executed))


def infer_test_command(repo_dir: Path) -> str | None:
    repo_dir = repo_dir.expanduser().resolve()
    if (repo_dir / "manage.py").exists():
        return "python manage.py test"

    if (repo_dir / "package.json").exists():
        node_snapshot = inspect_node_project(repo_dir)
        if node_snapshot.package_manager == "pnpm":
            return "pnpm test"
        if node_snapshot.package_manager == "yarn":
            return "yarn test"
        if node_snapshot.package_manager == "bun":
            return "bun test"
        return "npm test"

    pytest_markers = (
        "pytest.ini",
        "tox.ini",
        "setup.cfg",
        "pyproject.toml",
    )
    if any((repo_dir / marker).exists() for marker in pytest_markers):
        return "pytest -q"
    if any(repo_dir.glob("tests/test_*.py")) or (repo_dir / "tests").exists():
        return "pytest -q"
    return None


def prepare_workspace_runtime(
    repo_dir: Path,
    *,
    requested_test_command: str | None = None,
    saved_test_command: str | None = None,
    python_executable: str = sys.executable,
    venv_dir: Path | None = None,
    runner: CommandRunner = subprocess.run,
) -> WorkspaceRuntime:
    bootstrap = bootstrap_python_workspace(
        repo_dir,
        python_executable=python_executable,
        venv_dir=venv_dir,
        runner=runner,
    )
    test_command = requested_test_command or saved_test_command or infer_test_command(repo_dir)
    command_env = workspace_command_env(repo_dir, venv_dir=venv_dir)
    return WorkspaceRuntime(
        bootstrap=bootstrap,
        command_env=command_env,
        test_command=test_command,
    )
