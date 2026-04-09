from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import io
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest import mock

from axle_cli import doctor
from axle_cli.models import RunRequest, SavedConfig


class SetupFeatureTests(unittest.TestCase):
    def _load_runtime_modules(self):
        config = importlib.import_module("axle_cli.config")
        importlib.reload(config)

        state = importlib.import_module("axle_cli.state")
        importlib.reload(state)

        github_auth = importlib.import_module("axle_cli.github_auth")
        importlib.reload(github_auth)

        jira = importlib.import_module("axle_cli.jira")
        importlib.reload(jira)

        fake_goose_runner = types.ModuleType("axle_cli.goose_runner")

        class GooseError(RuntimeError):
            pass

        def run_goose(*args, **kwargs):  # pragma: no cover - sanity guard
            raise AssertionError("run_goose should not be called in setup feature tests")

        fake_goose_runner.GooseError = GooseError
        fake_goose_runner.run_goose = run_goose
        sys.modules["axle_cli.goose_runner"] = fake_goose_runner
        sys.modules.pop("axle_cli.session", None)
        sys.modules.pop("axle_cli.main", None)

        main = importlib.import_module("axle_cli.main")
        importlib.reload(main)

        return config, state, github_auth, jira, main

    def _temp_runtime(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        env = {
            "AXLE_CLI_HOME": str(Path(tempdir.name) / ".axle-cli"),
            "AXLE_CLI_WORKSPACES": str(Path(tempdir.name) / "workspaces"),
        }
        patcher = mock.patch.dict(os.environ, env, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        return tempdir

    def _check_by_name(self, report):
        return {check.name: check for check in report.checks}

    def test_github_and_jira_auth_persist_settings(self):
        self._temp_runtime()
        _, state, github_auth, jira, _ = self._load_runtime_modules()

        github_response = mock.Mock()
        github_response.raise_for_status.return_value = None
        github_response.json.return_value = {"login": "octocat"}

        jira_response = mock.Mock()
        jira_response.raise_for_status.return_value = None
        jira_response.json.return_value = {"accountId": "123"}

        with mock.patch.object(github_auth.requests, "get", return_value=github_response) as github_get:
            github_config = github_auth.save_github_settings(
                "gh-token",
                "git@github.com:acme/widgets.git",
                "develop",
            )

        with mock.patch.object(jira, "test_jira_connection", return_value=jira_response.json.return_value) as jira_test:
            jira_config = jira.save_jira_settings(
                "https://acme.atlassian.net",
                "me@example.com",
                "jira-token",
                "APP",
            )

        saved = state.load_config()

        self.assertEqual(github_config.github_token, "gh-token")
        self.assertEqual(github_config.github_login, "octocat")
        self.assertEqual(github_config.repository, "git@github.com:acme/widgets.git")
        self.assertEqual(github_config.base_branch, "develop")
        self.assertEqual(jira_config.jira_base_url, "https://acme.atlassian.net")
        self.assertEqual(jira_config.jira_email, "me@example.com")
        self.assertEqual(jira_config.jira_api_token, "jira-token")
        self.assertEqual(jira_config.jira_project_key, "APP")
        self.assertEqual(saved.github_login, "octocat")
        self.assertEqual(saved.jira_project_key, "APP")
        self.assertTrue(Path(os.environ["AXLE_CLI_HOME"]).joinpath("config.json").exists())
        github_get.assert_called_once()
        jira_test.assert_called_once()

    def test_status_reports_saved_values_and_secrets(self):
        self._temp_runtime()
        _, state, github_auth, jira, main = self._load_runtime_modules()

        github_response = mock.Mock()
        github_response.raise_for_status.return_value = None
        github_response.json.return_value = {"login": "octocat"}

        with mock.patch.object(github_auth.requests, "get", return_value=github_response):
            github_auth.save_github_settings(
                "gh-token",
                "git@github.com:acme/widgets.git",
                "develop",
            )

        with mock.patch.object(jira, "test_jira_connection", return_value={"accountId": "123"}):
            jira.save_jira_settings(
                "https://acme.atlassian.net",
                "me@example.com",
                "jira-token",
                "APP",
            )

        config = state.load_config()
        config.goose_binary = "goose"
        config.goose_builtin = "developer"
        state.save_config(config)

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = main.handle_status(argparse.Namespace(show_secrets=True), main.Terminal(verbose=False))

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("repository: git@github.com:acme/widgets.git", output)
        self.assertIn("base branch: develop", output)
        self.assertIn("GitHub user: octocat", output)
        self.assertIn("Jira base URL: https://acme.atlassian.net", output)
        self.assertIn("Jira project: APP", output)
        self.assertIn("GitHub token: gh-token", output)
        self.assertIn("Jira API token: jira-token", output)

    def test_auth_llm_allows_ollama_without_api_key(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()

        stdout = io.StringIO()
        args = argparse.Namespace(
            api_key=None,
            provider="ollama",
            model="qwen2.5-coder:1.5b",
            base_url=None,
        )
        with contextlib.redirect_stdout(stdout):
            exit_code = main.handle_auth_llm(args, main.Terminal(verbose=False))

        config = state.load_config()
        output = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertEqual(config.llm_provider, "ollama")
        self.assertEqual(config.llm_model, "qwen2.5-coder:1.5b")
        self.assertIsNone(config.llm_api_key)
        self.assertIn("Goose provider settings saved", output)

    def test_auth_llm_allows_docker_model_runner_without_api_key(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()

        stdout = io.StringIO()
        args = argparse.Namespace(
            api_key=None,
            provider="docker-model-runner",
            model="hf.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:q4_k_m",
            fallback_model=None,
            base_url="http://localhost:12434",
            base_path="/engines/llama.cpp/v1/chat/completions",
        )
        with contextlib.redirect_stdout(stdout):
            exit_code = main.handle_auth_llm(args, main.Terminal(verbose=False))

        config = state.load_config()
        self.assertEqual(exit_code, 0)
        self.assertEqual(config.llm_provider, "docker-model-runner")
        self.assertEqual(config.llm_model, "hf.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:q4_k_m")
        self.assertEqual(config.llm_base_url, "http://localhost:12434")
        self.assertEqual(config.llm_base_path, "/engines/llama.cpp/v1/chat/completions")
        self.assertIsNone(config.llm_api_key)

    def test_status_and_doctor_accept_ollama_without_api_key(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()

        config = state.load_config()
        config.github_token = "gh-token"
        config.github_login = "octocat"
        config.repository = "git@github.com:acme/widgets.git"
        config.jira_base_url = "https://acme.atlassian.net"
        config.jira_email = "me@example.com"
        config.jira_api_token = "jira-token"
        config.llm_provider = "ollama"
        config.llm_model = "qwen2.5-coder:1.5b"
        config.llm_api_key = None
        state.save_config(config)

        status_stdout = io.StringIO()
        with contextlib.redirect_stdout(status_stdout):
            status_exit = main.handle_status(argparse.Namespace(show_secrets=True), main.Terminal(verbose=False))

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/local/bin/goose"):
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=CompletedProcess(args=["goose", "--version"], returncode=0, stdout="goose 1.0\n", stderr=""),
            ):
                report = doctor.run_doctor(state.load_config())

        checks = self._check_by_name(report)
        self.assertEqual(status_exit, 0)
        self.assertIn("Goose provider/model: ollama / qwen2.5-coder:1.5b", status_stdout.getvalue())
        self.assertTrue(report.ok)
        self.assertEqual(checks["LLM provider"].level, "ok")
        self.assertEqual(checks["LLM provider"].details["provider"], "ollama")
        self.assertEqual(checks["LLM provider"].details["model"], "qwen2.5-coder:1.5b")

    def test_prepare_workspace_rotates_goose_session_when_provider_changes(self):
        self._temp_runtime()
        repo_module = importlib.import_module("axle_cli.repo")
        importlib.reload(repo_module)

        source_repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, source_repo, ignore_errors=True)
        (source_repo / "README.md").write_text("demo\n", encoding="utf-8")

        request = RunRequest(
            task="demo",
            repository=str(source_repo),
            repository_kind="local",
            base_branch="main",
        )
        openai_config = SavedConfig(llm_provider="openai", llm_model="gpt-5-mini")
        ollama_config = SavedConfig(llm_provider="ollama", llm_model="qwen2.5-coder:7b")

        first = repo_module.prepare_workspace(request, github_token=None, config=openai_config)
        second = repo_module.prepare_workspace(request, github_token=None, config=ollama_config)

        self.assertEqual(first.root, second.root)
        self.assertNotEqual(first.goose_session_name, second.goose_session_name)
        metadata = repo_module.load_workspace_metadata(second.root)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata.llm_provider, "ollama")
        self.assertEqual(metadata.llm_model, "qwen2.5-coder:7b")

    def test_doctor_reports_configuration_health(self):
        config = SavedConfig(
            github_token="gh-token",
            github_login="octocat",
            repository="git@github.com:acme/widgets.git",
            base_branch="develop",
            jira_base_url="https://acme.atlassian.net",
            jira_email="me@example.com",
            jira_api_token="jira-token",
            jira_project_key="APP",
            goose_binary="goose",
            goose_builtin="developer",
            llm_api_key="provider-key",
            llm_provider="openrouter",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/local/bin/goose"):
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=CompletedProcess(args=["goose", "--version"], returncode=0, stdout="goose 1.0\n", stderr=""),
            ):
                report = doctor.run_doctor(config)

        checks = self._check_by_name(report)
        self.assertTrue(report.ok)
        self.assertEqual(checks["Saved config"].details["base_branch"], "develop")
        self.assertEqual(checks["GitHub"].level, "ok")
        self.assertEqual(checks["GitHub"].details["login"], "octocat")
        self.assertEqual(checks["Repository"].level, "ok")
        self.assertEqual(checks["Repository"].details["type"], "remote")
        self.assertEqual(checks["Jira"].level, "ok")
        self.assertEqual(checks["Jira"].details["project"], "APP")
        self.assertEqual(checks["Goose binary"].level, "ok")
        self.assertEqual(checks["Goose builtins"].level, "ok")

    def test_doctor_reports_configured_goose_recipe_path(self):
        tempdir = self._temp_runtime()
        recipe_path = Path(tempdir.name) / "custom-recipe.yaml"
        recipe_path.write_text("version: 1.0.0\n", encoding="utf-8")
        config = SavedConfig(
            goose_binary="goose",
            goose_recipe=str(recipe_path),
        )

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/local/bin/goose"):
            with mock.patch.object(
                doctor.subprocess,
                "run",
                return_value=CompletedProcess(args=["goose", "--version"], returncode=0, stdout="goose 1.0\n", stderr=""),
            ):
                report = doctor.run_doctor(config)

        checks = self._check_by_name(report)
        self.assertEqual(checks["Goose recipe"].level, "ok")
        self.assertEqual(checks["Goose recipe"].details["recipe"], str(recipe_path.resolve()))

    def test_doctor_validates_builtin_goose_recipe(self):
        config = SavedConfig(
            goose_binary="goose",
            goose_builtin="developer",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )
        bundled_recipe = Path(__file__).resolve().parent.parent / "axle_cli" / "recipes" / "axle_run.yaml"
        smoke_recipe = Path(__file__).resolve().parent.parent / "axle_cli" / "recipes" / "axle_smoke.yaml"

        def fake_run(command, **kwargs):
            if command[1:] == ["--version"]:
                return CompletedProcess(args=command, returncode=0, stdout="goose 1.28.0\n", stderr="")
            if command[1:] == ["recipe", "validate", str(bundled_recipe)]:
                return CompletedProcess(args=command, returncode=0, stdout="recipe valid\n", stderr="")
            if command[1:] == ["recipe", "validate", str(smoke_recipe)]:
                return CompletedProcess(args=command, returncode=0, stdout="smoke recipe valid\n", stderr="")
            raise AssertionError(f"Unexpected subprocess command: {command}")

        with mock.patch.object(doctor.shutil, "which", return_value="/usr/local/bin/goose"):
            with mock.patch.object(doctor.subprocess, "run", side_effect=fake_run):
                report = doctor.run_doctor(config)

        checks = self._check_by_name(report)
        self.assertEqual(checks["Goose recipe"].level, "ok")
        self.assertIn("validated", checks["Goose recipe"].message.lower())
        self.assertEqual(checks["Goose recipe"].details["recipe"], str(bundled_recipe))
        self.assertEqual(checks["Goose smoke recipe"].level, "ok")
        self.assertEqual(checks["Goose smoke recipe"].details["recipe"], str(smoke_recipe))

    def test_doctor_flags_incomplete_jira_and_missing_local_repo(self):
        config = SavedConfig(
            github_token="gh-token",
            repository=str(Path("/tmp") / "definitely-missing-repo"),
            base_branch="main",
            jira_base_url="https://acme.atlassian.net",
            goose_binary="goose",
        )

        with mock.patch.object(doctor.shutil, "which", return_value=None):
            report = doctor.run_doctor(config)

        checks = self._check_by_name(report)
        self.assertFalse(report.ok)
        self.assertEqual(checks["Repository"].level, "fail")
        self.assertEqual(checks["Jira"].level, "fail")
        self.assertEqual(checks["GitHub"].level, "ok")

    def test_fetch_jira_issue_formats_description_and_comments(self):
        self._temp_runtime()
        _, _, _, jira, _ = self._load_runtime_modules()
        config = SavedConfig(
            jira_base_url="https://acme.atlassian.net",
            jira_email="me@example.com",
            jira_api_token="jira-token",
        )
        response = mock.Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "key": "APP-123",
            "fields": {
                "summary": "Fix dashboard filters",
                "description": {
                    "type": "doc",
                    "content": [
                        {"type": "paragraph", "content": [{"type": "text", "text": "Broken in production"}]},
                        {
                            "type": "bulletList",
                            "content": [
                                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Check SQL"}]}]},
                                {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Add tests"}]}]},
                            ],
                        },
                    ],
                },
                "project": {"key": "APP", "name": "Analytics Platform"},
                "issuetype": {"name": "Bug"},
                "labels": ["frontend", "urgent"],
                "components": [{"name": "dashboard"}],
                "priority": {"name": "High"},
                "status": {"name": "In Progress"},
                "comment": {
                    "comments": [
                        {
                            "author": {"displayName": "Alex"},
                            "created": "2026-03-26T10:00:00.000+0000",
                            "body": {
                                "type": "doc",
                                "content": [
                                    {"type": "paragraph", "content": [{"type": "text", "text": "Seen after deploy"}]}
                                ],
                            },
                        }
                    ]
                },
            },
        }

        with mock.patch.object(jira.requests, "get", return_value=response) as jira_get:
            issue = jira.fetch_jira_issue(config, "app-123")

        rendered = jira.format_jira_issue(issue)
        task_text = jira.jira_issue_task_text(issue, "Keep the patch narrow.")
        self.assertEqual(issue["key"], "APP-123")
        self.assertIn("Fix dashboard filters", rendered)
        self.assertIn("Broken in production", rendered)
        self.assertIn("- Check SQL", rendered)
        self.assertIn("Alex at 2026-03-26T10:00:00.000+0000", rendered)
        self.assertEqual(issue["project_key"], "APP")
        self.assertEqual(issue["issue_type"], "Bug")
        self.assertEqual(issue["labels"], ["frontend", "urgent"])
        self.assertEqual(issue["components"], ["dashboard"])
        self.assertEqual(issue["priority"], "High")
        self.assertEqual(issue["status"], "In Progress")
        self.assertIn("Routing metadata:", task_text)
        self.assertIn("project=APP", task_text)
        self.assertIn("labels=frontend, urgent", task_text)
        self.assertIn("components=dashboard", task_text)
        self.assertIn("Additional operator instructions:", task_text)
        jira_get.assert_called_once()

    def test_handle_jira_get_prints_issue_details(self):
        self._temp_runtime()
        _, _, _, _, main = self._load_runtime_modules()
        issue = {
            "key": "APP-123",
            "url": "https://acme.atlassian.net/browse/APP-123",
            "summary": "Fix dashboard filters",
            "description": "Broken in production",
            "comments": [{"author": "Alex", "created": "", "body": "Seen after deploy"}],
        }

        stdout = io.StringIO()
        with mock.patch.object(main, "fetch_jira_issue", return_value=issue):
            with contextlib.redirect_stdout(stdout):
                exit_code = main.handle_jira_get(argparse.Namespace(issue_key="APP-123"), main.Terminal(verbose=False))

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertIn("Issue: APP-123", output)
        self.assertIn("Summary: Fix dashboard filters", output)
        self.assertIn("Seen after deploy", output)

    def test_init_saves_repository_branch_and_default_test_command(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()
        project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: project_dir.exists() and shutil.rmtree(project_dir))

        stdout = io.StringIO()
        args = argparse.Namespace(
            repo=None,
            path=str(project_dir),
            branch="develop",
            test="python manage.py test",
        )
        with contextlib.redirect_stdout(stdout):
            exit_code = main.handle_init(args, main.Terminal(verbose=False))

        config = state.load_config()
        output = stdout.getvalue()

        self.assertEqual(exit_code, 0)
        self.assertEqual(config.repository, str(project_dir.resolve()))
        self.assertEqual(config.base_branch, "develop")
        self.assertEqual(config.default_test_command, "python manage.py test")
        self.assertIn("Axle defaults saved.", output)
        self.assertIn("default test command: python manage.py test", output)

    def test_run_with_jira_uses_issue_text_as_task(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()
        config = state.load_config()
        config.repository = "git@github.com:acme/widgets.git"
        state.save_config(config)
        issue = {
            "key": "APP-123",
            "url": "https://acme.atlassian.net/browse/APP-123",
            "summary": "Fix dashboard filters",
            "description": "Broken in production",
            "comments": [{"author": "Alex", "created": "", "body": "Seen after deploy"}],
        }
        captured_request: dict[str, object] = {}

        class FakeSessionRunner:
            def __init__(self, config, terminal):
                self.config = config
                self.terminal = terminal

            def run(self, request):
                captured_request["task"] = request.task
                captured_request["repository"] = request.repository
                from axle_cli.models import RunSummary

                return RunSummary(status="completed", repository=request.repository, workspace="/tmp/workspace")

        stdout = io.StringIO()
        args = argparse.Namespace(
            task="Keep the patch narrow.",
            jira="APP-123",
            repo=None,
            branch=None,
            test=None,
            pr=False,
            model=None,
            provider=None,
            repair_attempts=1,
            quiet=True,
        )
        with mock.patch.object(main, "fetch_jira_issue", return_value=issue):
            with mock.patch.dict(sys.modules, {"axle_cli.session": types.SimpleNamespace(SessionRunner=FakeSessionRunner)}):
                with contextlib.redirect_stdout(stdout):
                    exit_code = main.handle_run(args, main.Terminal(verbose=False))

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured_request["repository"], "git@github.com:acme/widgets.git")
        self.assertIn("Jira issue: APP-123", str(captured_request["task"]))
        self.assertIn("Seen after deploy", str(captured_request["task"]))
        self.assertIn("Additional operator instructions:", str(captured_request["task"]))

    def test_run_uses_saved_default_test_command_when_not_provided(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()
        config = state.load_config()
        config.repository = "git@github.com:acme/widgets.git"
        config.default_test_command = "pytest -q"
        state.save_config(config)
        captured_request: dict[str, object] = {}

        class FakeSessionRunner:
            def __init__(self, config, terminal):
                self.config = config
                self.terminal = terminal

            def run(self, request):
                captured_request["test_command"] = request.test_command
                captured_request["repository"] = request.repository
                from axle_cli.models import RunSummary

                return RunSummary(status="completed", repository=request.repository, workspace="/tmp/workspace")

        args = argparse.Namespace(
            task="Add filters",
            jira=None,
            repo=None,
            path=None,
            branch=None,
            setup="auto",
            test=None,
            pr=False,
            model=None,
            provider=None,
            repair_attempts=1,
            no_reuse_workspace=False,
            quiet=True,
        )

        with mock.patch.dict(sys.modules, {"axle_cli.session": types.SimpleNamespace(SessionRunner=FakeSessionRunner)}):
            exit_code = main.handle_run(args, main.Terminal(verbose=False))

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured_request["repository"], "git@github.com:acme/widgets.git")
        self.assertEqual(captured_request["test_command"], "pytest -q")

    def test_run_with_path_uses_local_repository_settings(self):
        self._temp_runtime()
        _, _, _, _, main = self._load_runtime_modules()
        project_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: project_dir.exists() and shutil.rmtree(project_dir))
        captured_request: dict[str, object] = {}

        class FakeSessionRunner:
            def __init__(self, config, terminal):
                self.config = config
                self.terminal = terminal

            def run(self, request):
                captured_request["repository"] = request.repository
                captured_request["repository_path"] = request.repository_path
                captured_request["repository_kind"] = request.repository_kind
                captured_request["setup_mode"] = request.setup_mode
                captured_request["reuse_workspace"] = request.reuse_workspace
                from axle_cli.models import RunSummary

                return RunSummary(status="completed", repository=request.repository, workspace="/tmp/workspace")

        args = argparse.Namespace(
            task="Add filters",
            jira=None,
            repo=None,
            path=str(project_dir),
            branch=None,
            setup="auto",
            test=None,
            pr=False,
            model=None,
            provider=None,
            repair_attempts=1,
            no_reuse_workspace=True,
            quiet=True,
        )

        with mock.patch.dict(sys.modules, {"axle_cli.session": types.SimpleNamespace(SessionRunner=FakeSessionRunner)}):
            exit_code = main.handle_run(args, main.Terminal(verbose=False))

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured_request["repository"], str(project_dir))
        self.assertEqual(captured_request["repository_path"], str(project_dir))
        self.assertEqual(captured_request["repository_kind"], "local")
        self.assertEqual(captured_request["setup_mode"], "auto")
        self.assertFalse(captured_request["reuse_workspace"])

    def test_run_chat_loop_supports_followup_diff_and_create_pr(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()
        config = state.load_config()
        config.repository = "https://github.com/acme/widgets.git"
        state.save_config(config)
        run_requests: list[object] = []
        resume_calls: list[tuple[object, str]] = []
        pr_calls: list[tuple[str, str]] = []

        class FakeSessionRunner:
            def __init__(self, config, terminal):
                self.config = config
                self.terminal = terminal

            def run(self, request):
                run_requests.append(request)
                from axle_cli.models import RunSummary

                return RunSummary(
                    status="completed",
                    repository=request.repository,
                    workspace="/tmp/workspace",
                    changed_files=["todos/views.py"],
                    diff_path="/tmp/workspace/changes.patch",
                )

            def resume(self, request, summary, instruction):
                resume_calls.append((request, instruction))
                summary.changed_files = ["todos/views.py"]
                summary.diff_path = "/tmp/workspace/changes.patch"
                return summary

            def create_pr(self, request, summary):
                pr_calls.append((request.repository, request.task))
                summary.branch_name = "axle/add-filters-12345678"
                summary.pr_url = "https://github.com/acme/widgets/pull/1"
                return summary

        args = argparse.Namespace(
            task="Add filters",
            jira=None,
            repo=None,
            path=None,
            branch=None,
            setup="auto",
            test=None,
            pr=False,
            model=None,
            provider=None,
            repair_attempts=1,
            no_reuse_workspace=False,
            chat=True,
            quiet=True,
        )

        stdout = io.StringIO()
        with mock.patch.dict(sys.modules, {"axle_cli.session": types.SimpleNamespace(SessionRunner=FakeSessionRunner)}):
            with mock.patch.object(main, "diff_text", return_value="diff --git a/todos/views.py b/todos/views.py\n"):
                with mock.patch("builtins.input", side_effect=["Please add a focused regression test.", "/diff", "/create_pr", "/exit"]):
                    with contextlib.redirect_stdout(stdout):
                        exit_code = main.handle_run(args, main.Terminal(verbose=False))

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(run_requests), 1)
        self.assertEqual(run_requests[0].task, "Add filters")
        self.assertEqual(run_requests[0].reuse_workspace, True)
        self.assertEqual(run_requests[0].repository, "https://github.com/acme/widgets.git")
        self.assertEqual(resume_calls[0][0].task, "Add filters")
        self.assertEqual(resume_calls[0][0].task_context, "Add filters")
        self.assertEqual(resume_calls[0][1], "Please add a focused regression test.")
        self.assertEqual(pr_calls, [("https://github.com/acme/widgets.git", "Add filters")])
        self.assertIn("Interactive mode is active.", output)
        self.assertIn("Current repository diff:", output)
        self.assertIn("PR: https://github.com/acme/widgets/pull/1", output)

    def test_command_shorthand_normalization(self):
        self._temp_runtime()
        _, _, _, _, main = self._load_runtime_modules()

        self.assertEqual(main._normalize_argv(["run", "KAN-2", "--test", "python manage.py test"]), ["run", "--jira", "KAN-2", "--test", "python manage.py test"])
        self.assertEqual(main._normalize_argv(["jira", "KAN-2"]), ["jira", "get", "KAN-2"])
        self.assertEqual(main._normalize_argv(["run", "--task", "Add filters"]), ["run", "--task", "Add filters"])

    def test_command_shorthand_normalization_preserves_future_chat_flag(self):
        self._temp_runtime()
        _, _, _, _, main = self._load_runtime_modules()

        self.assertEqual(
            main._normalize_argv(["run", "KAN-2", "--chat", "/status", "/diff", "/create_pr"]),
            ["run", "--jira", "KAN-2", "--chat", "/status", "/diff", "/create_pr"],
        )

    def test_terminal_summary_exposes_diff_and_pr_context(self):
        self._temp_runtime()
        _, _, _, _, main = self._load_runtime_modules()
        from axle_cli.models import RunSummary, TestResult

        summary = RunSummary(
            status="completed",
            repository="git@github.com:acme/widgets.git",
            workspace="/tmp/workspace",
            changed_files=["todos/views.py"],
            test_result=TestResult(command="python manage.py test", exit_code=0, output="OK\n"),
            branch_name="axle/add-filters-12345678",
            pr_url="https://github.com/acme/widgets/pull/1",
            diff_path="/tmp/workspace/changes.patch",
        )

        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            main.Terminal(verbose=False).summary(summary)

        output = stdout.getvalue()
        self.assertIn("Workspace: /tmp/workspace", output)
        self.assertIn("Changed files:", output)
        self.assertIn("- todos/views.py", output)
        self.assertIn("Verification: `python manage.py test` exited with 0", output)
        self.assertIn("Branch: axle/add-filters-12345678", output)
        self.assertIn("PR: https://github.com/acme/widgets/pull/1", output)
        self.assertIn("Diff: /tmp/workspace/changes.patch", output)

    def test_session_runner_fails_when_only_workspace_artifacts_change(self):
        self._temp_runtime()
        _, _, _, _, _ = self._load_runtime_modules()

        session = importlib.import_module("axle_cli.session")
        importlib.reload(session)
        from axle_cli.models import GooseRunResult, RunRequest, Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        workspace = Workspace(
            run_id="run-1234",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            github_token="gh-token",
            repository="git@github.com:acme/widgets.git",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:7b",
        )
        request = RunRequest(
            task="Add filters",
            repository="git@github.com:acme/widgets.git",
            base_branch="main",
            max_repair_attempts=1,
        )
        fake_bootstrap = types.SimpleNamespace(
            plan=types.SimpleNamespace(
                snapshot=types.SimpleNamespace(is_python_project=True),
                notes=(),
            ),
            executed_commands=(("python3", "-m", "venv", str(repo_dir / ".venv")),),
        )
        goose_result = GooseRunResult(
            command=["goose", "run"],
            exit_code=0,
            transcript_path=root / "goose-transcript.log",
            changed_files=[".axle-bootstrap.json", ".venv/"],
            diff_text="",
            summary="Goose run completed.",
        )
        run_goose_mock = mock.Mock(side_effect=[goose_result, goose_result])

        with mock.patch.object(session, "prepare_workspace", return_value=workspace):
            with mock.patch.object(session, "bootstrap_python_workspace", return_value=fake_bootstrap):
                with mock.patch.object(session, "run_goose", run_goose_mock):
                    with mock.patch.object(session, "source_changed_files", return_value=[]):
                        with mock.patch.object(session, "repo_tree", return_value="repo"):
                            runner = session.SessionRunner(config=config, terminal=session.Terminal(verbose=False))
                            summary = runner.run(request)

        self.assertEqual(summary.status, "failed")
        self.assertEqual(summary.failure, "Goose completed without producing any source code changes.")
        self.assertEqual(summary.changed_files, [])
        self.assertIn("managed workspace artifacts", summary.risks[0])
        self.assertEqual(run_goose_mock.call_count, 2)

    def test_session_runner_auto_detects_and_saves_default_test_command(self):
        self._temp_runtime()
        _, _, _, _, _ = self._load_runtime_modules()

        session = importlib.import_module("axle_cli.session")
        importlib.reload(session)
        from axle_cli.models import GooseRunResult, RunRequest, TestResult, Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        (repo_dir / "manage.py").write_text("print('ok')\n", encoding="utf-8")
        workspace = Workspace(
            run_id="run-5678",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            github_token="gh-token",
            repository="git@github.com:acme/widgets.git",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )
        request = RunRequest(
            task="Add filters",
            repository="git@github.com:acme/widgets.git",
            base_branch="main",
            max_repair_attempts=0,
        )
        fake_bootstrap = types.SimpleNamespace(
            plan=types.SimpleNamespace(
                snapshot=types.SimpleNamespace(is_python_project=True),
                notes=("Django project detected without a dependency manifest; bootstrapping with `pip install django`.",),
            ),
            executed_commands=tuple(),
        )
        goose_result = GooseRunResult(
            command=["goose", "run"],
            exit_code=0,
            transcript_path=root / "goose-transcript.log",
            changed_files=["todos/views.py"],
            diff_text="diff --git a/todos/views.py b/todos/views.py",
            summary="Applied todo filter changes.",
        )
        test_result = TestResult(command="python manage.py test", exit_code=0, output="OK\n")

        with mock.patch.object(session, "prepare_workspace", return_value=workspace):
            with mock.patch.object(session, "bootstrap_python_workspace", return_value=fake_bootstrap):
                with mock.patch.object(session, "run_goose", return_value=goose_result):
                    with mock.patch.object(session, "source_changed_files", return_value=["todos/views.py"]):
                        with mock.patch.object(session, "run_test_command", return_value=test_result) as run_test:
                            with mock.patch.object(session, "repo_tree", return_value="repo"):
                                runner = session.SessionRunner(config=config, terminal=session.Terminal(verbose=False))
                                summary = runner.run(request)

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.test_result.command, "python manage.py test")
        self.assertEqual(config.default_test_command, "python manage.py test")
        run_test.assert_called_once()

    def test_session_runner_retries_with_fallback_model_after_noop(self):
        self._temp_runtime()
        _, _, _, _, _ = self._load_runtime_modules()

        session = importlib.import_module("axle_cli.session")
        importlib.reload(session)
        from axle_cli.models import GooseRunResult, RunRequest, TestResult, Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        target = repo_dir / "todos.py"
        target.write_text("before\n", encoding="utf-8")
        workspace = Workspace(
            run_id="run-fallback",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            github_token="gh-token",
            repository="git@github.com:acme/widgets.git",
            llm_provider="ollama",
            llm_model="small-model",
            llm_fallback_model="bigger-model",
        )
        request = RunRequest(
            task="Add filters",
            repository="git@github.com:acme/widgets.git",
            base_branch="main",
        )
        fake_bootstrap = types.SimpleNamespace(
            plan=types.SimpleNamespace(
                snapshot=types.SimpleNamespace(is_python_project=True),
                project_kind="python",
                notes=(),
            ),
            executed_commands=tuple(),
        )
        first_result = GooseRunResult(
            command=["goose", "run"],
            exit_code=0,
            transcript_path=root / "goose-transcript.log",
            changed_files=[],
            diff_text="",
            summary="No edits",
            output_kind="file-snippets",
            transcript_excerpt="Update todos.py",
            likely_files=["todos.py"],
            file_write_candidates={},
        )
        fallback_result = GooseRunResult(
            command=["goose", "run"],
            exit_code=0,
            transcript_path=root / "goose-transcript.log",
            changed_files=[],
            diff_text="",
            summary="Applied fallback edits",
            output_kind="file-snippets",
            transcript_excerpt="Update todos.py",
            likely_files=["todos.py"],
            file_write_candidates={"todos.py": "after\n"},
        )
        test_result = TestResult(command="pytest -q", exit_code=0, output="OK\n")

        with mock.patch.object(session, "prepare_workspace", return_value=workspace):
            with mock.patch.object(session, "bootstrap_python_workspace", return_value=fake_bootstrap):
                with mock.patch.object(session, "run_goose", side_effect=[first_result, fallback_result]) as run_goose:
                    with mock.patch.object(session, "source_changed_files", side_effect=[[], ["todos.py"]]):
                        with mock.patch.object(session, "infer_test_command", return_value="pytest -q"):
                            with mock.patch.object(session, "run_test_command", return_value=test_result):
                                with mock.patch.object(session, "repo_tree", return_value="repo"):
                                    runner = session.SessionRunner(config=config, terminal=session.Terminal(verbose=False))
                                    summary = runner.run(request)

        self.assertEqual(summary.status, "completed")
        self.assertEqual(summary.changed_files, ["todos.py"])
        self.assertEqual(run_goose.call_args_list[1].kwargs["model"], "bigger-model")

    def test_goose_task_prompt_requires_file_edits_or_explicit_blocker(self):
        prompts = importlib.import_module("axle_cli.prompts")
        importlib.reload(prompts)

        system_prompt = prompts.goose_system_prompt()
        prompt = prompts.goose_task_prompt(
            "Implement filtering",
            "todos/\nmanage.py",
            "python manage.py test",
        )
        noop_prompt = prompts.noop_repair_prompt(
            "Implement filtering",
            "python manage.py test",
            "### Step 1: Inspect repository\nHere is a plan but no edits.",
            ["todos/views.py"],
            transcript_excerpt="### Step 1: Inspect repository\nHere is a plan but no edits.",
            likely_files=["todos/views.py"],
            output_kind="plan-only",
        )

        self.assertIn("Do not answer with only JSON, a plan, or an analysis summary.", system_prompt)
        self.assertIn("Either make concrete source-file edits for the task", system_prompt)
        self.assertIn("Workspace-only artifacts", system_prompt)
        self.assertIn("If you start with a plan or code snippets, immediately apply them to files before replying.", system_prompt)
        self.assertIn("python manage.py test", prompt)
        self.assertIn("Task:", prompt)
        self.assertIn("Transcript classification: plan-only", noop_prompt)
        self.assertIn("Likely target files:", noop_prompt)
        self.assertIn("todos/views.py", noop_prompt)
        self.assertIn("Transcript excerpt:", noop_prompt)

    def test_goose_runner_builds_expected_command(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        config = SavedConfig(
            goose_binary="goose",
            goose_builtin="developer,github",
            llm_provider="openrouter",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/goose"):
            command = goose_runner.build_command(
                config,
                "inspect the repo",
                model="model-override",
                provider="provider-override",
                session_name="axle-workspace-key",
                resume_session=True,
            )

        self.assertEqual(
            command,
            [
                "/usr/local/bin/goose",
                "run",
                "--no-profile",
                "--output-format",
                "stream-json",
                "--recipe",
                str(Path(goose_runner.__file__).resolve().parent / "recipes" / "axle_run.yaml"),
                "--params",
                "task_prompt=inspect the repo",
                "--name",
                "axle-workspace-key",
                "--resume",
                "--with-builtin",
                "developer",
                "--provider",
                "provider-override",
                "--model",
                "model-override",
            ],
        )

    def test_goose_runner_maps_docker_model_runner_provider_to_openai_cli(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        config = SavedConfig(
            goose_binary="goose",
            goose_builtin="developer",
            llm_provider="docker-model-runner",
            llm_model="huggingface.co/qwen/qwen2.5-coder-3b-instruct-gguf",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/goose"):
            command = goose_runner.build_command(
                config,
                "inspect the repo",
                provider="docker-model-runner",
            )

        provider_index = command.index("--provider")
        self.assertEqual(command[provider_index + 1], "openai")

    def test_goose_runner_uses_smoke_recipe_for_smoke_purpose(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        config = SavedConfig(
            goose_binary="goose",
            goose_builtin="developer",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/goose"):
            command = goose_runner.build_command(
                config,
                "inspect the repo",
                session_name="axle-workspace-key",
                recipe_purpose="smoke",
            )

        self.assertIn("--recipe", command)
        recipe_path = command[command.index("--recipe") + 1]
        self.assertEqual(recipe_path, str(Path(goose_runner.__file__).resolve().parent / "recipes" / "axle_smoke.yaml"))

    def test_goose_runner_sets_openai_host_and_base_path_for_docker_model_runner(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        workspace = Workspace(
            run_id="run-dmr",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="goose",
            goose_builtin="developer",
            llm_provider="docker-model-runner",
            llm_model="hf.co/Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:q4_k_m",
            llm_base_url="http://localhost:12434",
            llm_base_path="/engines/llama.cpp/v1/chat/completions",
        )

        env = goose_runner._goose_env(config, workspace, provider="docker-model-runner")

        self.assertEqual(env["OPENAI_HOST"], "http://localhost:12434")
        self.assertEqual(env["OPENAI_BASE_PATH"], "/engines/llama.cpp/v1/chat/completions")
        self.assertNotIn("OPENAI_BASE_URL", env)

    def test_goose_runner_prefers_explicit_recipe_override_over_configured_recipe(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        configured_recipe = root / "configured.yaml"
        explicit_recipe = root / "smoke.yaml"
        configured_recipe.write_text("version: 1.0.0\n", encoding="utf-8")
        explicit_recipe.write_text("version: 1.0.0\n", encoding="utf-8")

        config = SavedConfig(
            goose_binary="goose",
            goose_recipe=str(configured_recipe),
            goose_builtin="developer,github",
            llm_provider="openrouter",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/goose"):
            command = goose_runner.build_command(
                config,
                "inspect the repo",
                recipe=str(explicit_recipe),
                session_name="axle-workspace-key",
            )

        recipe_index = command.index("--recipe")
        self.assertEqual(command[recipe_index + 1], str(explicit_recipe))
        self.assertNotIn(str(configured_recipe), command)

    def test_goose_runner_captures_transcript_and_diff(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        target = repo_dir / "README.md"
        target.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        script = root / "fake_goose.py"
        script.write_text(
            "\n".join(
                [
                    "from pathlib import Path",
                    "import sys",
                    "print('Inspecting repository')",
                    "Path('README.md').write_text('after\\n', encoding='utf-8')",
                    "print('Applied README update')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        workspace = Workspace(
            run_id="run-1234",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_api_key="provider-key",
            llm_provider="openrouter",
            llm_base_url="https://openrouter.ai/api/v1",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(
            goose_runner,
            "build_command",
            return_value=["python3", str(script)],
        ):
            result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertEqual(result.exit_code, 0)
        self.assertIn("Applied README update", result.summary)
        self.assertEqual(result.changed_files, ["README.md"])
        self.assertIn("--- a/README.md", result.diff_text)
        self.assertTrue(workspace.prompt_path.exists())
        self.assertIn("inspect the repo", workspace.prompt_path.read_text(encoding="utf-8"))
        self.assertIn("Applied README update", workspace.transcript_path.read_text(encoding="utf-8"))

    def test_goose_runner_applies_recoverable_patch_from_transcript(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        target = repo_dir / "README.md"
        target.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=repo_dir, check=True, capture_output=True, text=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-m", "initial"],
            cwd=repo_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        workspace = Workspace(
            run_id="run-4321",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_api_key="provider-key",
            llm_provider="openrouter",
            llm_base_url="https://openrouter.ai/api/v1",
            llm_model="qwen/qwen3-coder:free",
        )
        script = root / "fake_goose.py"
        script.write_text(
            "\n".join(
                [
                    "print('Here is the patch.')",
                    "print('```diff')",
                    "print('diff --git a/README.md b/README.md')",
                    "print('--- a/README.md')",
                    "print('+++ b/README.md')",
                    "print('@@ -1 +1 @@')",
                    "print('-before')",
                    "print('+after')",
                    "print('```')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        with mock.patch.object(goose_runner, "build_command", return_value=["python3", str(script)]):
            result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.changed_files, ["README.md"])
        self.assertIn("recoverable patch", result.summary)
        self.assertIn("--- a/README.md", result.diff_text)
        self.assertIn("+after", result.diff_text)

    def test_goose_runner_uses_clear_noop_summary_when_no_source_changes(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        script = root / "fake_goose.py"
        script.write_text(
            "\n".join(
                [
                    "print('### Step 1: Inspect repository')",
                    "print('Here is a plan but no edits.')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        workspace = Workspace(
            run_id="run-9999",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_provider="openrouter",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/python3"):
            with mock.patch.object(goose_runner.subprocess, "Popen") as popen:
                process = mock.Mock()
                process.stdout = io.StringIO("### Step 1: Inspect repository\nHere is a plan but no edits.\n")
                process.wait.return_value = 0
                process.kill.return_value = None
                process.communicate.return_value = ("", "")
                popen.return_value = process
                with mock.patch.object(goose_runner, "source_changed_files", return_value=[]):
                    with mock.patch.object(goose_runner, "diff_text", return_value=""):
                        result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.output_kind, "plan-only")
        self.assertIn("without source-file changes", result.summary)
        self.assertIn("Step 1", result.transcript_excerpt)
        self.assertEqual(result.likely_files, [])

    def test_goose_runner_times_out_and_reports_clean_failure(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        script = root / "slow_goose.py"
        script.write_text(
            "\n".join(
                [
                    "import time",
                    "print('{\"message\":\"starting\"}', flush=True)",
                    "time.sleep(3)",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        workspace = Workspace(
            run_id="run-timeout",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:7b",
        )

        original_timeout = goose_runner.settings.goose_timeout_seconds
        object.__setattr__(goose_runner.settings, "goose_timeout_seconds", 1)
        self.addCleanup(lambda: object.__setattr__(goose_runner.settings, "goose_timeout_seconds", original_timeout))

        with mock.patch.object(goose_runner, "build_command", return_value=["python3", str(script)]):
            result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertEqual(result.exit_code, 124)
        self.assertEqual(result.output_kind, "timeout")
        self.assertIn("timed out", result.summary)

    def test_session_runner_uses_fallback_model_on_noop_retry(self):
        self._temp_runtime()
        _, _, _, _, _ = self._load_runtime_modules()

        session = importlib.import_module("axle_cli.session")
        importlib.reload(session)
        from axle_cli.models import GooseRunResult, RunRequest, TestResult, Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        (repo_dir / "manage.py").write_text("print('ok')\n", encoding="utf-8")
        workspace = Workspace(
            run_id="run-fallback",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            github_token="gh-token",
            repository="git@github.com:acme/widgets.git",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:1.5b",
            llm_fallback_model="qwen2.5-coder:14b",
        )
        request = RunRequest(
            task="Add filters",
            repository="git@github.com:acme/widgets.git",
            base_branch="main",
        )
        fake_bootstrap = types.SimpleNamespace(
            plan=types.SimpleNamespace(
                snapshot=types.SimpleNamespace(is_python_project=True),
                notes=(),
            ),
            executed_commands=tuple(),
        )
        noop_result = GooseRunResult(
            command=["goose", "run"],
            exit_code=0,
            transcript_path=root / "goose-transcript.log",
            changed_files=[],
            diff_text="",
            summary="Goose run completed.",
            output_kind="plan-only",
            transcript_excerpt="### Step 1: Inspect repository",
            likely_files=["todos/views.py"],
        )
        repair_result = GooseRunResult(
            command=["goose", "run"],
            exit_code=0,
            transcript_path=root / "goose-transcript.log",
            changed_files=["todos/views.py"],
            diff_text="diff --git a/todos/views.py b/todos/views.py",
            summary="Applied fallback changes.",
            output_kind="source-edits",
            transcript_excerpt="Applied changes.",
            likely_files=["todos/views.py"],
        )
        test_result = TestResult(command="python manage.py test", exit_code=0, output="OK\n")
        run_goose_mock = mock.Mock(side_effect=[noop_result, repair_result])

        with mock.patch.object(session, "prepare_workspace", return_value=workspace):
            with mock.patch.object(session, "bootstrap_python_workspace", return_value=fake_bootstrap):
                with mock.patch.object(session, "run_goose", run_goose_mock):
                    with mock.patch.object(session, "source_changed_files", side_effect=[[], ["todos/views.py"]]):
                        with mock.patch.object(session, "repo_tree", return_value="repo"):
                            with mock.patch.object(session, "run_test_command", return_value=test_result):
                                runner = session.SessionRunner(config=config, terminal=session.Terminal(verbose=False))
                                summary = runner.run(request)

        self.assertEqual(summary.status, "completed")
        self.assertEqual(run_goose_mock.call_count, 2)
        self.assertEqual(run_goose_mock.call_args_list[0].kwargs["model"], "qwen2.5-coder:1.5b")
        self.assertEqual(run_goose_mock.call_args_list[1].kwargs["model"], "qwen2.5-coder:14b")
        self.assertEqual(summary.changed_files, ["todos/views.py"])

    def test_run_with_pr_flag_requests_pr_creation_and_reports_pr_url(self):
        self._temp_runtime()
        _, state, _, _, main = self._load_runtime_modules()
        config = state.load_config()
        config.repository = "git@github.com:acme/widgets.git"
        state.save_config(config)
        captured_request: dict[str, object] = {}

        class FakeSessionRunner:
            def __init__(self, config, terminal):
                self.config = config
                self.terminal = terminal

            def run(self, request):
                captured_request["create_pr"] = request.create_pr
                captured_request["task"] = request.task
                from axle_cli.models import RunSummary

                return RunSummary(
                    status="completed",
                    repository=request.repository,
                    workspace="/tmp/workspace",
                    changed_files=["todos/views.py"],
                    branch_name="axle/add-filters-12345678",
                    pr_url="https://github.com/acme/widgets/pull/1",
                    diff_path="/tmp/workspace/changes.patch",
                )

        stdout = io.StringIO()
        args = argparse.Namespace(
            task="Add filters",
            jira=None,
            repo=None,
            path=None,
            branch=None,
            setup="auto",
            test=None,
            pr=True,
            model=None,
            provider=None,
            repair_attempts=1,
            no_reuse_workspace=False,
            quiet=True,
        )

        with mock.patch.dict(sys.modules, {"axle_cli.session": types.SimpleNamespace(SessionRunner=FakeSessionRunner)}):
            with contextlib.redirect_stdout(stdout):
                exit_code = main.handle_run(args, main.Terminal(verbose=False))

        output = stdout.getvalue()
        self.assertEqual(exit_code, 0)
        self.assertTrue(captured_request["create_pr"])
        self.assertEqual(captured_request["task"], "Add filters")
        self.assertIn("Branch: axle/add-filters-12345678", output)
        self.assertIn("PR: https://github.com/acme/widgets/pull/1", output)

    def test_goose_runner_extracts_file_write_candidates_from_transcript(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        (repo_dir / "todos").mkdir()
        (repo_dir / "todos" / "views.py").write_text("before\n", encoding="utf-8")

        workspace = Workspace(
            run_id="run-4242",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )

        transcript = "\n".join(
            [
                "### Step 1: Update `todos/views.py`",
                "```python",
                "print('after')",
                "```",
            ]
        )
        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/python3"):
            with mock.patch.object(goose_runner.subprocess, "Popen") as popen:
                process = mock.Mock()
                process.stdout = io.StringIO(transcript + "\n")
                process.wait.return_value = 0
                process.kill.return_value = None
                popen.return_value = process
                with mock.patch.object(goose_runner, "source_changed_files", return_value=[]):
                    with mock.patch.object(goose_runner, "diff_text", return_value=""):
                        result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertEqual(result.output_kind, "file-snippets")
        self.assertEqual(result.likely_files[0], "todos/views.py")
        self.assertIn("todos/views.py", result.file_write_candidates)
        self.assertIn("print('after')", result.file_write_candidates["todos/views.py"])

    def test_goose_runner_contract_exposes_session_recipe_and_output_format_hooks(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)

        signature = inspect.signature(goose_runner.build_command)

        self.assertIn("session_name", signature.parameters)
        self.assertIn("resume_session", signature.parameters)
        self.assertIn("system_prompt", signature.parameters)
        self.assertIn("output_format", signature.parameters)
        self.assertEqual(signature.parameters["output_format"].default, "stream-json")
        self.assertEqual(signature.parameters["session_name"].default, None)

    def test_goose_runner_contract_parses_structured_json_output(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        script = root / "fake_goose.py"
        script.write_text(
            "\n".join(
                [
                    "print('{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Inspecting repository\"}]}}')",
                    "print('{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Update `README.md`\"}]}}')",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        script.chmod(script.stat().st_mode | stat.S_IXUSR)

        workspace = Workspace(
            run_id="run-structured",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_api_key="provider-key",
            llm_provider="openrouter",
            llm_base_url="https://openrouter.ai/api/v1",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(goose_runner, "build_command", return_value=["python3", str(script)]):
            with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/python3"):
                with mock.patch.object(goose_runner.subprocess, "Popen") as popen:
                    process = mock.Mock()
                    process.stdout = io.StringIO(
                        "{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Inspecting repository\"}]}}\n"
                        "{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Update `README.md`\"}]}}\n"
                    )
                    process.wait.return_value = 0
                    process.kill.return_value = None
                    popen.return_value = process
                    with mock.patch.object(goose_runner, "source_changed_files", return_value=[]):
                        with mock.patch.object(goose_runner, "diff_text", return_value=""):
                            result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertTrue(result.structured_output)
        self.assertEqual(result.session_name, "axle-workspace-key")
        self.assertEqual(result.likely_files, ["README.md"])
        self.assertIn("Update `README.md`", result.transcript_excerpt)

    def test_goose_runner_parses_richer_structured_stream_json_events(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)
        (repo_dir / "README.md").write_text("before\n", encoding="utf-8")

        workspace = Workspace(
            run_id="run-stream-json",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_api_key="provider-key",
            llm_provider="openrouter",
            llm_base_url="https://openrouter.ai/api/v1",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/python3"):
            with mock.patch.object(goose_runner.subprocess, "Popen") as popen:
                process = mock.Mock()
                process.stdout = io.StringIO(
                    "{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Inspecting repository\"}]}}\n"
                    "{\"type\":\"tool\",\"tool\":\"text_editor\",\"status\":\"running\"}\n"
                    "{\"type\":\"error\",\"message\":\"temporary provider warning\"}\n"
                    "{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Update `README.md`\"}],\"metadata\":{\"userVisible\":true,\"agentVisible\":true}},\"files\":[\"README.md\"],\"kind\":\"result\",\"status\":\"source-edits\"}\n"
                )
                process.wait.return_value = 0
                process.kill.return_value = None
                popen.return_value = process
                with mock.patch.object(goose_runner, "source_changed_files", return_value=[]):
                    with mock.patch.object(goose_runner, "diff_text", return_value=""):
                        result = goose_runner.run_goose(
                            config,
                            workspace,
                            "inspect the repo",
                            goose_runner.Terminal(verbose=False),
                        )

        self.assertTrue(result.structured_output)
        self.assertEqual(result.output_kind, "structured-json")
        self.assertEqual(result.session_name, "axle-workspace-key")
        self.assertEqual(result.used_recipe, str(Path(goose_runner.__file__).resolve().parent / "recipes" / "axle_run.yaml"))
        self.assertEqual(result.likely_files, ["README.md"])
        self.assertIn("source-edits", result.summary)
        self.assertIn("Update `README.md`", result.transcript_excerpt)
        self.assertIn("tool:text_editor", result.assistant_text)
        self.assertIn("temporary provider warning", result.assistant_text)
        self.assertEqual(result.file_write_candidates, {})

    def test_goose_runner_detects_structured_events_after_text_preamble(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        workspace = Workspace(
            run_id="run-preamble",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(
            goose_binary="python3",
            goose_builtin="developer",
            llm_api_key="provider-key",
            llm_provider="openrouter",
            llm_base_url="https://openrouter.ai/api/v1",
            llm_model="qwen/qwen3-coder:free",
        )

        with mock.patch.object(goose_runner.shutil, "which", return_value="/usr/local/bin/python3"):
            with mock.patch.object(goose_runner.subprocess, "Popen") as popen:
                process = mock.Mock()
                process.stdout = io.StringIO(
                    "Loading recipe: Axle Coding Session\n"
                    "Description: Axle-managed coding workflow.\n"
                    "{\"type\":\"message\",\"message\":{\"content\":[{\"text\":\"Inspecting repository\"}]}}\n"
                    "{\"type\":\"tool\",\"tool\":\"tree\",\"status\":\"running\"}\n"
                    "{\"type\":\"complete\",\"total_tokens\":10}\n"
                )
                process.wait.return_value = 0
                process.kill.return_value = None
                popen.return_value = process
                with mock.patch.object(goose_runner, "source_changed_files", return_value=[]):
                    with mock.patch.object(goose_runner, "diff_text", return_value=""):
                        result = goose_runner.run_goose(
                            config,
                            workspace,
                            "inspect the repo",
                            goose_runner.Terminal(verbose=False),
                        )

        self.assertTrue(result.structured_output)
        self.assertIn("message", result.status_events)
        self.assertIn("tool", result.status_events)
        self.assertIn("complete", result.status_events)
        self.assertIn("tree", result.tool_events)
        self.assertIn("Inspecting repository", result.assistant_text)

    def test_goose_runner_classifies_printed_tool_payload_as_noop(self):
        goose_runner = importlib.import_module("axle_cli.goose_runner")
        importlib.reload(goose_runner)
        from axle_cli.models import Workspace

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        repo_dir = root / "repo"
        repo_dir.mkdir()
        subprocess.run(["git", "init"], cwd=repo_dir, check=True, capture_output=True, text=True)

        workspace = Workspace(
            run_id="run-tool-payload",
            workspace_key="workspace-key",
            root=root,
            repo_dir=repo_dir,
            transcript_path=root / "goose-transcript.log",
            prompt_path=root / "goose-prompt.md",
            diff_path=root / "changes.patch",
            test_log_path=root / "test.log",
        )
        config = SavedConfig(goose_binary="python3", goose_builtin="developer", llm_provider="ollama", llm_model="qwen2.5-coder:14b")

        with mock.patch.object(goose_runner, "build_command", return_value=["python3", "-c", "print('noop')"]):
            with mock.patch.object(goose_runner.subprocess, "Popen") as popen:
                process = mock.Mock()
                process.stdout = io.StringIO("tool:text_editor\nfunction_name: shell\ncommand: git log -1\n")
                process.wait.return_value = 0
                process.kill.return_value = None
                popen.return_value = process
                with mock.patch.object(goose_runner, "source_changed_files", return_value=[]):
                    with mock.patch.object(goose_runner, "diff_text", return_value=""):
                        result = goose_runner.run_goose(config, workspace, "inspect the repo", goose_runner.Terminal(verbose=False))

        self.assertEqual(result.output_kind, "tool-payload")
        self.assertEqual(result.changed_files, [])
        self.assertIn("tool payload text", result.summary)

    def test_source_changed_files_ignores_runtime_database(self):
        repo_module = importlib.import_module("axle_cli.repo")
        importlib.reload(repo_module)

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_dir = Path(tempdir.name)

        with mock.patch.object(repo_module, "changed_files", return_value=["db.sqlite3", "todos/views.py", ".venv/bin/python"]):
            filtered = repo_module.source_changed_files(repo_dir)

        self.assertEqual(filtered, ["todos/views.py"])

    def test_commit_and_push_stages_only_source_files(self):
        repo_module = importlib.import_module("axle_cli.repo")
        importlib.reload(repo_module)

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        repo_dir = Path(tempdir.name)

        commands: list[list[str]] = []

        def fake_run_git(command, _repo_dir):
            commands.append(command)
            if command[:3] == ["diff", "--cached", "--name-only"]:
                return CompletedProcess(command, 0, stdout="todos/templates/todos/index.html\n", stderr="")
            return CompletedProcess(command, 0, stdout="", stderr="")

        with mock.patch.object(repo_module, "source_changed_files", return_value=["todos/templates/todos/index.html"]), mock.patch.object(
            repo_module, "run_git", side_effect=fake_run_git
        ), mock.patch.object(repo_module, "looks_like_remote", return_value=True):
            staged = repo_module.commit_and_push(
                repo_dir,
                "axle/demo-branch",
                "demo commit",
                "gh-token",
                "https://github.com/acme/widgets",
                "Axle CLI",
                "axle@example.com",
            )

        self.assertEqual(staged, ["todos/templates/todos/index.html"])
        self.assertIn(["add", "--", "todos/templates/todos/index.html"], commands)
        self.assertNotIn(["add", "-A"], commands)

    def test_build_pr_body_includes_issue_context_and_changed_files(self):
        pr_module = importlib.import_module("axle_cli.pr")
        importlib.reload(pr_module)

        body = pr_module.build_pr_body(
            "Update Todo List to My Tasks in todos/templates/todos/index.html only.",
            issue_key="KAN-9703",
            issue_url="https://jira.example.com/browse/KAN-9703",
            issue_labels=["axle-run", "demo"],
            changed_files=["todos/templates/todos/index.html"],
            test_command="python manage.py test",
            provider="openai",
            model="gpt-5-mini",
        )

        self.assertIn("## Summary", body)
        self.assertIn("[KAN-9703](https://jira.example.com/browse/KAN-9703)", body)
        self.assertIn("Labels: axle-run, demo", body)
        self.assertIn("`todos/templates/todos/index.html`", body)
        self.assertIn("Validation: `python manage.py test`", body)
        self.assertIn("Model: openai / gpt-5-mini", body)

    def test_repo_tree_omits_managed_workspace_artifacts(self):
        repo_module = importlib.import_module("axle_cli.repo")
        importlib.reload(repo_module)

        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        root = Path(tempdir.name)
        (root / "todos").mkdir()
        (root / "todos" / "views.py").write_text("print('ok')\n", encoding="utf-8")
        (root / ".venv" / "bin").mkdir(parents=True)
        (root / ".venv" / "bin" / "python").write_text("", encoding="utf-8")
        (root / ".goose").mkdir()
        (root / "node_modules").mkdir()
        (root / ".axle-bootstrap.json").write_text("{}", encoding="utf-8")
        (root / "db.sqlite3").write_text("", encoding="utf-8")

        rendered = repo_module.repo_tree(root, 20)

        self.assertIn("todos/", rendered)
        self.assertIn("todos/views.py", rendered)
        self.assertNotIn(".venv/", rendered)
        self.assertNotIn(".goose/", rendered)
        self.assertNotIn("node_modules/", rendered)
        self.assertNotIn(".axle-bootstrap.json", rendered)
        self.assertNotIn("db.sqlite3", rendered)

    def test_real_provider_smoke_run_hook(self):
        repo_path = os.getenv("AXLE_SMOKE_REPO")
        task = os.getenv("AXLE_SMOKE_TASK")
        test_command = os.getenv("AXLE_SMOKE_TEST_COMMAND")
        provider = os.getenv("AXLE_SMOKE_PROVIDER")
        model = os.getenv("AXLE_SMOKE_MODEL")
        if not all((repo_path, task, test_command, provider, model)):
            self.skipTest("Set AXLE_SMOKE_REPO, AXLE_SMOKE_TASK, AXLE_SMOKE_TEST_COMMAND, AXLE_SMOKE_PROVIDER, and AXLE_SMOKE_MODEL to run this smoke test.")

        repo_dir = Path(repo_path).expanduser().resolve()
        if not repo_dir.exists():
            self.skipTest(f"Smoke repo does not exist: {repo_dir}")

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "axle_cli.main",
                "run",
                "--path",
                str(repo_dir),
                "--task",
                task,
                "--test",
                test_command,
                "--provider",
                provider,
                "--model",
                model,
                "--quiet",
                "--repair-attempts",
                "0",
                "--no-reuse-workspace",
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, msg=(result.stdout or "") + (result.stderr or ""))
        self.assertIn("Final outcome: COMPLETED", result.stdout)


if __name__ == "__main__":
    unittest.main()
