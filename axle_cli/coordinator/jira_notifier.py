from __future__ import annotations

from ..jira import add_jira_comment
from ..models import AutomationRun, SavedConfig


def _comment_lines(title: str, run: AutomationRun) -> list[str]:
    return [
        title,
        "",
        f"Run ID: {run.run_id}",
        f"Issue: {run.issue_key}",
        f"Repository: {run.repository}",
        f"Base branch: {run.base_branch}",
    ]


def notify_run_started(config: SavedConfig, run: AutomationRun) -> None:
    add_jira_comment(
        config,
        run.issue_key,
        "\n".join(_comment_lines("Axle run started.", run)),
    )


def notify_worker_launched(config: SavedConfig, run: AutomationRun) -> None:
    lines = _comment_lines("Axle worker launched.", run)
    if run.worker_launch_spec:
        lines.extend(
            [
                f"Worker launch status: {run.worker_launch_spec.status}",
                f"Worker instance name: {run.worker_launch_spec.instance_name}",
            ]
        )
        if run.worker_launch_spec.instance_id:
            lines.append(f"Worker instance id: {run.worker_launch_spec.instance_id}")
        if run.worker_launch_spec.coordinator_url:
            lines.append(f"Coordinator URL: {run.worker_launch_spec.coordinator_url}")
    add_jira_comment(config, run.issue_key, "\n".join(lines))


def notify_pr_created(config: SavedConfig, run: AutomationRun) -> None:
    if not run.pr_url:
        return
    lines = _comment_lines("Axle pull request created.", run)
    lines.append(f"PR: {run.pr_url}")
    if run.branch_name:
        lines.append(f"Branch: {run.branch_name}")
    if run.changed_files:
        lines.append("Changed files:")
        lines.extend(f"- {path}" for path in run.changed_files)
    add_jira_comment(config, run.issue_key, "\n".join(lines))


def notify_run_completed(config: SavedConfig, run: AutomationRun) -> None:
    lines = _comment_lines("Axle run completed.", run)
    lines.append(f"Status: {run.status}")
    if run.pr_url:
        lines.append(f"PR: {run.pr_url}")
    if run.failure:
        lines.append("Failure details:")
        lines.append(run.failure)
    if run.changed_files:
        lines.append("Changed files:")
        lines.extend(f"- {path}" for path in run.changed_files)
    add_jira_comment(config, run.issue_key, "\n".join(lines))


def _recovery_lines(title: str, run: AutomationRun, reason: str | None = None) -> list[str]:
    lines = _comment_lines(title, run)
    lines.append(f"Status: {run.status}")
    lines.append(f"Retry count: {run.retry_count}")
    lines.append(f"Max retries: {run.max_retries}")
    if run.recovery_state:
        lines.append(f"Recovery state: {run.recovery_state}")
    if run.recovery_reason:
        lines.append(f"Recovery reason: {run.recovery_reason}")
    if reason and reason != run.recovery_reason:
        lines.append(f"Reason: {reason}")
    if run.worker_instance_id:
        lines.append(f"Worker instance: {run.worker_instance_id}")
    if run.pr_url:
        lines.append(f"PR: {run.pr_url}")
    return lines


def notify_run_requeued(config: SavedConfig, run: AutomationRun, reason: str | None = None) -> None:
    add_jira_comment(config, run.issue_key, "\n".join(_recovery_lines("Axle run requeued.", run, reason)))


def notify_run_recovered(config: SavedConfig, run: AutomationRun, reason: str | None = None) -> None:
    add_jira_comment(config, run.issue_key, "\n".join(_recovery_lines("Axle run recovered.", run, reason)))


def notify_orphan_cleanup(config: SavedConfig, run: AutomationRun, reason: str | None = None) -> None:
    add_jira_comment(config, run.issue_key, "\n".join(_recovery_lines("Axle orphaned worker cleaned up.", run, reason)))
