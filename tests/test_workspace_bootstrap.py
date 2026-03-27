from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import subprocess
from subprocess import CompletedProcess
from unittest import mock

from axle_cli import bootstrap


class WorkspaceBootstrapTests(unittest.TestCase):
    def _make_repo(self) -> tempfile.TemporaryDirectory:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return tempdir

    def test_inspect_python_project_detects_markers_and_requirements(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "manage.py").write_text("print('ok')\n", encoding="utf-8")
        (repo_dir / "requirements.txt").write_text("django\n", encoding="utf-8")
        (repo_dir / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

        snapshot = bootstrap.inspect_python_project(repo_dir)

        self.assertTrue(snapshot.is_python_project)
        self.assertEqual(snapshot.markers, ("requirements.txt", "pyproject.toml", "manage.py"))
        self.assertEqual([path.name for path in snapshot.dependency_files], ["requirements.txt"])

    def test_build_plan_includes_dependency_and_editable_install_commands(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "requirements.txt").write_text("django\n", encoding="utf-8")
        (repo_dir / "requirements-dev.txt").write_text("pytest\n", encoding="utf-8")
        (repo_dir / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

        plan = bootstrap.build_python_bootstrap_plan(repo_dir, python_executable="python3")

        self.assertEqual(plan.venv_dir, repo_dir / ".venv")
        self.assertEqual(plan.create_venv_command, ("python3", "-m", "venv", str(repo_dir / ".venv")))
        self.assertEqual(
            plan.dependency_commands,
            (
                (str(repo_dir / ".venv" / "bin" / "python"), "-m", "pip", "install", "-r", "requirements.txt"),
                (str(repo_dir / ".venv" / "bin" / "python"), "-m", "pip", "install", "-r", "requirements-dev.txt"),
                (str(repo_dir / ".venv" / "bin" / "python"), "-m", "pip", "install", "-e", "."),
            ),
        )

    def test_build_plan_falls_back_to_django_for_manage_py_projects(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "manage.py").write_text("print('ok')\n", encoding="utf-8")

        plan = bootstrap.build_python_bootstrap_plan(repo_dir, python_executable="python3")

        self.assertEqual(
            plan.dependency_commands,
            (
                (str(repo_dir / ".venv" / "bin" / "python"), "-m", "pip", "install", "django"),
            ),
        )
        self.assertIn("pip install django", plan.notes[0])

    def test_build_workspace_plan_detects_node_projects(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "package.json").write_text('{"name":"demo","scripts":{"test":"node test.js"}}\n', encoding="utf-8")
        (repo_dir / "package-lock.json").write_text("{}\n", encoding="utf-8")

        plan = bootstrap.build_workspace_bootstrap_plan(repo_dir, python_executable="python3")

        self.assertEqual(plan.project_kind, "node")
        self.assertIsNotNone(plan.node_snapshot)
        self.assertEqual(plan.node_snapshot.package_manager, "npm")
        self.assertEqual(plan.dependency_commands, (("npm", "ci"),))
        self.assertIn("Node project detected", plan.notes[0])

    def test_bootstrap_executes_node_dependency_commands(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")

        calls: list[dict[str, object]] = []

        def fake_runner(args, **kwargs):
            calls.append({"args": tuple(args), **kwargs})
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        execution = bootstrap.bootstrap_python_workspace(repo_dir, python_executable="python3", runner=fake_runner)

        self.assertEqual(execution.plan.project_kind, "node")
        self.assertEqual(execution.executed_commands, (("npm", "install"),))
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["cwd"], repo_dir)

    def test_bootstrap_executes_venv_then_dependency_commands(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "requirements.txt").write_text("django\n", encoding="utf-8")
        (repo_dir / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")

        calls: list[dict[str, object]] = []

        def fake_runner(args, **kwargs):
            calls.append({"args": tuple(args), **kwargs})
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        execution = bootstrap.bootstrap_python_workspace(repo_dir, python_executable="python3", runner=fake_runner)

        self.assertEqual(
            execution.executed_commands,
            (
                ("python3", "-m", "venv", str(repo_dir / ".venv")),
                (str(repo_dir / ".venv" / "bin" / "python"), "-m", "pip", "install", "-r", "requirements.txt"),
                (str(repo_dir / ".venv" / "bin" / "python"), "-m", "pip", "install", "-e", "."),
            ),
        )
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0]["cwd"], repo_dir)
        self.assertEqual(calls[0]["env"]["VIRTUAL_ENV"], str(repo_dir / ".venv"))
        self.assertTrue(str(repo_dir / ".venv" / "bin") in calls[0]["env"]["PATH"])

    def test_bootstrap_skips_non_python_projects(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "README.md").write_text("hello\n", encoding="utf-8")

        calls: list[tuple[str, ...]] = []

        def fake_runner(args, **kwargs):
            calls.append(tuple(args))
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        execution = bootstrap.bootstrap_python_workspace(repo_dir, python_executable="python3", runner=fake_runner)

        self.assertEqual(execution.executed_commands, tuple())
        self.assertEqual(calls, [])
        self.assertFalse(execution.plan.snapshot.is_python_project)

    def test_infer_test_command_prefers_project_type_markers(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "manage.py").write_text("print('ok')\n", encoding="utf-8")

        self.assertEqual(bootstrap.infer_test_command(repo_dir), "python manage.py test")

        node_tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(node_tempdir.cleanup)
        node_dir = Path(node_tempdir.name).resolve()
        (node_dir / "package.json").write_text('{"name":"demo"}\n', encoding="utf-8")
        self.assertEqual(bootstrap.infer_test_command(node_dir), "npm test")

    def test_bundled_goose_recipe_validates_with_cli(self):
        recipe_path = Path(__file__).resolve().parent.parent / "axle_cli" / "recipes" / "axle_run.yaml"

        result = subprocess.run(
            ["goose", "recipe", "validate", str(recipe_path)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("valid", (result.stdout or "").lower())

    def test_bundled_goose_recipe_renders_expected_policy(self):
        recipe_path = Path(__file__).resolve().parent.parent / "axle_cli" / "recipes" / "axle_run.yaml"

        result = subprocess.run(
            [
                "goose",
                "run",
                "--render-recipe",
                "--recipe",
                str(recipe_path),
                "--params",
                "task_prompt=Inspect the repository before editing.",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        rendered = result.stdout or ""
        self.assertEqual(result.returncode, 0)
        self.assertIn("Axle Coding Session", rendered)
        self.assertIn("You are Goose running inside Axle CLI.", rendered)
        self.assertIn("Inspect the repository before editing.", rendered)

    def test_bootstrap_raises_on_failed_command(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name)
        (repo_dir / "requirements.txt").write_text("django\n", encoding="utf-8")

        def fake_runner(args, **kwargs):
            if tuple(args[:3]) == ("python3", "-m", "venv"):
                return CompletedProcess(args=args, returncode=0, stdout="", stderr="")
            return CompletedProcess(args=args, returncode=1, stdout="", stderr="pip failed")

        with self.assertRaises(bootstrap.BootstrapError) as exc:
            bootstrap.bootstrap_python_workspace(repo_dir, python_executable="python3", runner=fake_runner)

        self.assertIn("pip failed", str(exc.exception))

    def test_python_venv_helpers_resolve_paths(self):
        venv_dir = Path("/tmp/workspace/.venv")
        self.assertEqual(bootstrap.venv_bin_dir(venv_dir), venv_dir / "bin")
        self.assertEqual(bootstrap.venv_python_path(venv_dir), venv_dir / "bin" / "python")

    def test_bootstrap_skips_when_cached_state_matches(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        (repo_dir / "requirements.txt").write_text("django\n", encoding="utf-8")
        venv_python = repo_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")

        calls: list[tuple[str, ...]] = []

        def fake_runner(args, **kwargs):
            calls.append(tuple(args))
            return CompletedProcess(args=args, returncode=0, stdout="", stderr="")

        first = bootstrap.build_python_bootstrap_plan(repo_dir, python_executable="python3")
        state_path = repo_dir / bootstrap.BOOTSTRAP_STATE_FILENAME
        state_path.write_text(
            '{"fingerprint": "%s"}\n' % bootstrap._plan_fingerprint(first),
            encoding="utf-8",
        )

        execution = bootstrap.bootstrap_python_workspace(repo_dir, python_executable="python3", runner=fake_runner)

        self.assertEqual(execution.executed_commands, tuple())
        self.assertEqual(calls, [])

    def test_workspace_command_env_uses_virtualenv_when_present(self):
        tempdir = self._make_repo()
        repo_dir = Path(tempdir.name).resolve()
        venv_python = repo_dir / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True, exist_ok=True)
        venv_python.write_text("", encoding="utf-8")

        env = bootstrap.workspace_command_env(repo_dir)

        self.assertEqual(env["VIRTUAL_ENV"], str(repo_dir / ".venv"))
        self.assertTrue(str(repo_dir / ".venv" / "bin") in env["PATH"])


if __name__ == "__main__":
    unittest.main()
