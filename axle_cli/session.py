from __future__ import annotations

from datetime import datetime, timezone

from .config import settings
from .goose_runner import GooseError, run_goose
from .models import RunRequest, RunSummary, SavedConfig
from .pr import create_pull_request
from .prompts import goose_task_prompt, repair_prompt
from .repo import (
    RepoError,
    branch_name_for,
    changed_files,
    commit_and_push,
    prepare_workspace,
    repo_tree,
)
from .terminal import Terminal
from .tests import run_test_command


class SessionRunner:
    def __init__(self, config: SavedConfig, terminal: Terminal) -> None:
        self.config = config
        self.terminal = terminal

    def run(self, request: RunRequest) -> RunSummary:
        summary = RunSummary(status="running", repository=request.repository)
        workspace = prepare_workspace(request, self.config.github_token)
        summary.run_id = workspace.run_id
        summary.workspace = str(workspace.root)

        self.terminal.step("Inspect", "Goose has an isolated repository workspace.")
        tree = repo_tree(workspace.repo_dir, settings.repo_tree_limit)
        self.terminal.step("Inspect", "Goose collected a bounded repository tree snapshot.")
        prompt_text = goose_task_prompt(request.task, tree, request.test_command)

        try:
            self.terminal.step("Goose", "Starting the task with inspect-first instructions.")
            result = run_goose(
                self.config,
                workspace,
                prompt_text,
                self.terminal,
                model=request.model,
                provider=request.provider,
            )
        except (RepoError, GooseError) as exc:
            summary.status = "failed"
            summary.failure = str(exc)
            summary.risks.append("Goose could not start, so no file edits were attempted.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        if result.exit_code != 0:
            summary.status = "failed"
            summary.failure = result.summary
            summary.changed_files = result.changed_files
            summary.diff_path = str(workspace.diff_path)
            summary.risks.append("Goose exited non-zero before the run could be verified.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        summary.changed_files = changed_files(workspace.repo_dir)
        summary.diff_path = str(workspace.diff_path)

        if request.test_command:
            test_result = run_test_command(workspace.repo_dir, request.test_command, workspace.test_log_path, self.terminal)
            summary.test_result = test_result
            if not test_result.ok and request.max_repair_attempts > 0:
                self.terminal.step("Goose", "Validation failed. Asking Goose for one bounded repair pass.")
                failure_excerpt = "\n".join(test_result.output.splitlines()[-40:])
                repair_text = repair_prompt(request.task, request.test_command, failure_excerpt, summary.changed_files)
                repair_result = run_goose(
                    self.config,
                    workspace,
                    repair_text,
                    self.terminal,
                    model=request.model,
                    provider=request.provider,
                )
                if repair_result.exit_code != 0:
                    summary.status = "failed"
                    summary.failure = repair_result.summary
                    summary.changed_files = repair_result.changed_files
                    summary.diff_path = str(workspace.diff_path)
                    summary.risks.append("Goose repair failed before validation could be rerun.")
                    summary.finished_at = datetime.now(timezone.utc)
                    return summary
                summary.changed_files = repair_result.changed_files
                summary.diff_path = str(workspace.diff_path)
                test_result = run_test_command(workspace.repo_dir, request.test_command, workspace.test_log_path, self.terminal)
                summary.test_result = test_result
            if not summary.test_result.ok:
                summary.status = "failed"
                summary.failure = f"Validation failed: `{request.test_command}` exited with {summary.test_result.exit_code}."
                summary.risks.append("The repository contains unverified changes after a failed Goose validation pass.")
                summary.finished_at = datetime.now(timezone.utc)
                return summary

        if not summary.changed_files:
            summary.status = "failed"
            summary.failure = "No repository changes were produced by the run."
            summary.risks.append("Goose completed the task without modifying any files.")
            summary.finished_at = datetime.now(timezone.utc)
            return summary

        if request.create_pr:
            branch_name = branch_name_for(request.task[:48], workspace.run_id)
            try:
                staged = commit_and_push(
                    workspace.repo_dir,
                    branch_name,
                    f"Axle CLI output for {workspace.run_id[:8]}",
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
                    f"Axle CLI output: {workspace.run_id[:8]}",
                    request.task,
                    self.config.github_token,
                )
            except RepoError as exc:
                summary.status = "failed"
                summary.failure = str(exc)
                summary.risks.append("Goose completed locally, but push or PR creation failed.")
                summary.finished_at = datetime.now(timezone.utc)
                return summary

        summary.status = "completed"
        summary.finished_at = datetime.now(timezone.utc)
        return summary
