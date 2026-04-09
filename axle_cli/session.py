from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import types

from .bootstrap import BootstrapError, bootstrap_python_workspace, infer_test_command, workspace_command_env
from .config import settings
from .goose_runner import GooseError, run_goose
from .models import RunRequest, RunSummary, SavedConfig
from .pr import create_pull_request
from .prompts import goose_task_prompt, noop_repair_prompt, repair_prompt
from .repo import (
    RepoError,
    branch_name_for,
    commit_and_push,
    prepare_workspace,
    repo_tree,
    source_changed_files,
)
from .terminal import ExecutionSink, Terminal
from .tests import run_test_command


class SessionRunner:
    def __init__(self, config: SavedConfig, terminal: ExecutionSink) -> None:
        self.config = config
        self.terminal = terminal

    def _retry_model(self, primary_model: str | None) -> str | None:
        fallback_model = (self.config.llm_fallback_model or "").strip() or None
        if fallback_model and fallback_model != (primary_model or "").strip():
            return fallback_model
        return primary_model

    def _prepare_execution(self, request: RunRequest):
        self.terminal.step("Workspace", "Creating or reusing the isolated repository workspace.")
        workspace = prepare_workspace(request, self.config.github_token, self.config)
        source_label = "local project copy" if workspace.source_kind == "local" else "git checkout"
        self.terminal.step("Workspace", f"Workspace ready at `{workspace.root}` using the prepared {source_label}.")

        runtime = None
        if getattr(request, "setup_mode", "auto") != "skip":
            self.terminal.step("Setup", "Preparing the project workspace and environment.")
            bootstrap_execution = bootstrap_python_workspace(workspace.repo_dir)
            runtime = types.SimpleNamespace(
                bootstrap=bootstrap_execution,
                command_env=workspace_command_env(workspace.repo_dir),
                test_command=(request.test_command or self.config.default_test_command or infer_test_command(workspace.repo_dir)),
            )
            project_kind = getattr(
                runtime.bootstrap.plan,
                "project_kind",
                "python" if getattr(runtime.bootstrap.plan.snapshot, "is_python_project", False) else "none",
            )
            if project_kind in {"python", "node", "mixed"}:
                if runtime.bootstrap.executed_commands:
                    self.terminal.step("Setup", "Workspace bootstrap completed.")
                else:
                    self.terminal.step("Setup", "Reusing the prepared workspace environment.")
                for note in runtime.bootstrap.plan.notes:
                    self.terminal.info(f"Setup note: {note}")
            else:
                self.terminal.step("Setup", "No Python or Node bootstrap was required for this workspace.")

        test_command = runtime.test_command if runtime else (request.test_command or self.config.default_test_command)
        if test_command and not (request.test_command or self.config.default_test_command):
            self.config.default_test_command = test_command
            self.terminal.step("Testing", f"Detected validation command `{test_command}`.")
        elif test_command:
            self.terminal.step("Testing", f"Using validation command `{test_command}`.")
        else:
            self.terminal.step("Testing", "No validation command was configured or detected.")

        self.terminal.step("Inspect", "Goose has an isolated repository workspace.")
        tree = repo_tree(workspace.repo_dir, settings.repo_tree_limit)
        self.terminal.step("Inspect", "Goose collected a bounded repository tree snapshot.")
        return workspace, runtime, test_command, tree

    def _run_validation(self, workspace, runtime, test_command, summary: RunSummary):
        if not test_command:
            return None
        test_env = runtime.command_env if runtime else workspace_command_env(workspace.repo_dir)
        test_result = run_test_command(
            workspace.repo_dir,
            test_command,
            workspace.test_log_path,
            self.terminal,
            env=test_env,
        )
        summary.test_result = test_result
        return test_result

    def run(self, request: RunRequest) -> RunSummary:
        summary = RunSummary(status="running", repository=request.repository)
        remaining_repair_attempts = max(0, int(request.max_repair_attempts))
        task_subject = request.task_context or request.task
        try:
            workspace, runtime, test_command, tree = self._prepare_execution(request)
            summary.run_id = workspace.run_id
            summary.workspace = str(workspace.root)
            summary.goose_session_name = workspace.goose_session_name

            primary_model = (request.model or self.config.llm_model or "").strip() or None
            repair_model = self._retry_model(primary_model)
            task_context = goose_task_prompt(task_subject, tree, test_command)

            self.terminal.step("Goose", "Starting the task with recipe-backed instructions.")
            result = run_goose(
                self.config,
                workspace,
                task_context if not request.resume_goose_session else request.task,
                self.terminal,
                model=primary_model,
                provider=request.provider,
                resume_session=request.resume_goose_session,
                recipe_purpose="implement",
            )
        except (RepoError, GooseError, BootstrapError) as exc:
            summary.status = "failed"
            summary.failure = str(exc)
            summary.risks.append("The workspace could not be prepared for a coding run, so no verified file edits were attempted.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        if result.exit_code != 0:
            summary.status = "failed"
            summary.failure = result.summary
            summary.changed_files = source_changed_files(workspace.repo_dir)
            summary.diff_path = str(workspace.diff_path)
            summary.risks.append("Goose exited non-zero before the run could be verified.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        summary.changed_files = source_changed_files(workspace.repo_dir)
        summary.diff_path = str(workspace.diff_path)

        if not summary.changed_files and remaining_repair_attempts > 0:
            retry_label = f" with fallback model `{repair_model}`" if repair_model and repair_model != primary_model else ""
            self.terminal.step("Goose", f"No source changes were produced. Asking Goose for one bounded repair pass{retry_label}.")
            repair_text = noop_repair_prompt(
                request.task,
                test_command,
                result.summary,
                summary.changed_files,
                transcript_excerpt=result.transcript_excerpt,
                likely_files=result.likely_files,
                output_kind=result.output_kind,
            )
            repair_result = run_goose(
                self.config,
                workspace,
                repair_text,
                self.terminal,
                model=repair_model,
                provider=request.provider,
                resume_session=True,
                recipe_purpose="implement",
            )
            if repair_result.exit_code != 0:
                summary.status = "failed"
                summary.failure = repair_result.summary
                summary.changed_files = source_changed_files(workspace.repo_dir)
                summary.diff_path = str(workspace.diff_path)
                summary.risks.append("Goose repair failed before any source changes were produced.")
                summary.finished_at = datetime.now(timezone.utc)
                return summary
            summary.changed_files = source_changed_files(workspace.repo_dir)
            summary.diff_path = str(workspace.diff_path)
            remaining_repair_attempts -= 1

        if not summary.changed_files:
            fallback_model = (self.config.llm_fallback_model or "").strip()
            current_model = (request.model or self.config.llm_model or "").strip()
            if fallback_model and fallback_model != current_model:
                self.terminal.step("Goose", f"No source changes were produced. Retrying once with fallback model `{fallback_model}`.")
                fallback_result = run_goose(
                    self.config,
                    workspace,
                    "Continue the existing task in this same repository session. Inspect the current repository state and make the missing source-file edits now.",
                    self.terminal,
                    model=fallback_model,
                    provider=request.provider,
                    resume_session=True,
                    recipe_purpose="implement",
                )
                if fallback_result.exit_code == 0:
                    summary.changed_files = source_changed_files(workspace.repo_dir)
                    summary.diff_path = str(workspace.diff_path)
                else:
                    summary.risks.append(f"Fallback model `{fallback_model}` exited before producing source edits.")

        if not summary.changed_files:
            summary.status = "failed"
            summary.failure = "Goose completed without producing any source code changes."
            summary.risks.append("Goose only modified managed workspace artifacts, not repository source files.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        test_result = self._run_validation(workspace, runtime, test_command, summary)
        if test_result and not test_result.ok and remaining_repair_attempts > 0:
            self.terminal.step("Goose", "Validation failed. Asking Goose for one bounded repair pass.")
            failure_excerpt = "\n".join(test_result.output.splitlines()[-40:])
            repair_text = repair_prompt(request.task, test_command, failure_excerpt, summary.changed_files)
            repair_result = run_goose(
                self.config,
                workspace,
                repair_text,
                self.terminal,
                model=repair_model,
                provider=request.provider,
                resume_session=True,
                recipe_purpose="implement",
            )
            if repair_result.exit_code != 0:
                summary.status = "failed"
                summary.failure = repair_result.summary
                summary.changed_files = source_changed_files(workspace.repo_dir)
                summary.diff_path = str(workspace.diff_path)
                summary.risks.append("Goose repair failed before validation could be rerun.")
                summary.finished_at = datetime.now(timezone.utc)
                return summary
            summary.changed_files = source_changed_files(workspace.repo_dir)
            summary.diff_path = str(workspace.diff_path)
            remaining_repair_attempts -= 1
            test_result = self._run_validation(workspace, runtime, test_command, summary)

        if summary.test_result and not summary.test_result.ok:
            summary.status = "failed"
            summary.failure = f"Validation failed: `{test_command}` exited with {summary.test_result.exit_code}."
            summary.risks.append("The repository contains unverified changes after a failed Goose validation pass.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        if request.create_pr:
            try:
                summary = self.create_pr(request, summary)
            except RepoError as exc:
                summary.status = "failed"
                summary.failure = str(exc)
                summary.risks.append("Goose completed locally, but push or PR creation failed.")
                summary.finished_at = datetime.now(timezone.utc)
                return summary

        summary.status = "completed"
        summary.finished_at = datetime.now(timezone.utc)
        return summary

    def resume(self, request: RunRequest, summary: RunSummary, instruction: str) -> RunSummary:
        if not summary.workspace:
            raise RepoError("A workspace is required before continuing a Goose session.")

        request.resume_goose_session = True
        workspace, runtime, test_command, _tree = self._prepare_execution(request)
        self.terminal.step("Goose", "Continuing the existing Goose session.")
        result = run_goose(
            self.config,
            workspace,
            instruction,
            self.terminal,
            model=request.model or self.config.llm_model,
            provider=request.provider,
            resume_session=True,
            recipe_purpose="implement",
        )

        summary.run_id = workspace.run_id
        summary.workspace = str(workspace.root)
        summary.goose_session_name = workspace.goose_session_name
        summary.diff_path = str(workspace.diff_path)
        summary.changed_files = source_changed_files(workspace.repo_dir)

        if result.exit_code != 0:
            summary.status = "failed"
            summary.failure = result.summary
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        self._run_validation(workspace, runtime, test_command, summary)
        if summary.test_result and not summary.test_result.ok:
            summary.status = "failed"
            summary.failure = f"Validation failed: `{test_command}` exited with {summary.test_result.exit_code}."
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        summary.status = "completed"
        summary.failure = None
        summary.finished_at = datetime.now(timezone.utc)
        return summary

    def create_pr(self, request: RunRequest, summary: RunSummary) -> RunSummary:
        if summary.pr_url:
            return summary
        if not summary.workspace:
            raise RepoError("A workspace is required before a PR can be created.")
        if not summary.changed_files:
            raise RepoError("No repository changes are available to open a PR.")

        repo_dir = Path(summary.workspace) / "repo"
        branch_name = branch_name_for((request.task_context or request.task)[:48], summary.run_id)
        staged = commit_and_push(
            repo_dir,
            branch_name,
            f"Axle CLI output for {summary.run_id[:8]}",
            self.config.github_token,
            request.repository,
            self.config.git_author_name,
            self.config.git_author_email,
        )
        summary.branch_name = branch_name
        summary.changed_files = staged
        summary.pr_url = create_pull_request(
            request.repository,
            branch_name,
            request.base_branch,
            f"Axle CLI output: {summary.run_id[:8]}",
            request.task_context or request.task,
            self.config.github_token,
            issue_key=request.issue_key,
            issue_url=request.issue_url,
            issue_labels=list(getattr(request, "issue_labels", []) or []),
            changed_files=summary.changed_files,
            test_command=request.test_command,
            provider=request.provider,
            model=request.model,
        )
        return summary
