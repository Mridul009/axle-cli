from __future__ import annotations

from typing import Any
from pathlib import Path

from ..jira import fetch_jira_issue
from ..models import AutomationRun, RunEvent, RunSummary, SavedConfig, utc_now
from .artifact_store import FileArtifactStore, build_artifact_store
from .lifecycle import LifecycleManager
from .jira_notifier import (
    notify_orphan_cleanup,
    notify_pr_created,
    notify_run_completed,
    notify_run_recovered,
    notify_run_requeued,
    notify_run_started,
    notify_worker_launched,
)
from .router import route_issue_to_run
from .run_store import FileBackedRunStore, SqliteRunStore
from .store_factory import open_run_store
from .worker_launcher import Ec2WorkerLauncher

RunStore = FileBackedRunStore | SqliteRunStore


class CoordinatorService:
    def __init__(
        self,
        config: SavedConfig,
        store: RunStore | None = None,
        launcher: Ec2WorkerLauncher | None = None,
        artifact_store: FileArtifactStore | None = None,
    ) -> None:
        self.config = config
        self.store = store or open_run_store()
        self.launcher = launcher or Ec2WorkerLauncher()
        self.lifecycle = LifecycleManager(self.store)
        store_root = getattr(self.store, "root", None)
        default_artifact_root = Path(store_root) / "artifacts" if store_root else None
        self.artifact_store = artifact_store or build_artifact_store(root=default_artifact_root)

    def create_run_from_issue_key(self, issue_key: str, *, launch_worker: bool = False, coordinator_url: str | None = None, run_id: str | None = None) -> AutomationRun:
        issue = fetch_jira_issue(self.config, issue_key)
        return self.create_run_from_issue(issue, launch_worker=launch_worker, coordinator_url=coordinator_url, run_id=run_id, event_stage="Jira")

    def create_run_from_issue(
        self,
        issue: dict[str, Any],
        *,
        launch_worker: bool = False,
        coordinator_url: str | None = None,
        run_id: str | None = None,
        event_stage: str = "Jira",
    ) -> AutomationRun:
        run = route_issue_to_run(self.config, issue)
        if run_id:
            run.run_id = run_id
        issue_key = str(issue.get("key") or issue.get("issue_key") or run.issue_key)
        run.events.append(RunEvent(kind="queued", stage=event_stage, message=f"Run created from issue {issue_key}."))
        saved = self.store.create_run(run)
        try:
            notify_run_started(self.config, saved)
        except Exception:
            pass
        if launch_worker:
            spec = self.launcher.launch(saved, coordinator_url=coordinator_url)
            saved = self.store.attach_launch_spec(saved.run_id, spec)
            try:
                notify_worker_launched(self.config, saved)
            except Exception:
                pass
        return saved

    def create_run_from_payload(self, payload: dict[str, object], *, launch_worker: bool = False, coordinator_url: str | None = None, run_id: str | None = None) -> AutomationRun:
        if run_id:
            payload = dict(payload)
            payload["run_id"] = run_id
        run = self.store.create_run(payload)
        run.events.append(RunEvent(kind="queued", stage="Webhook", message="Run created from webhook payload."))
        saved = self.store.create_run(run)
        if launch_worker:
            spec = self.launcher.launch(saved, coordinator_url=coordinator_url)
            saved = self.store.attach_launch_spec(saved.run_id, spec)
            try:
                notify_worker_launched(self.config, saved)
            except Exception:
                pass
        return saved

    def get_worker_payload(self, run_id: str, callback_token: str) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.callback_token != callback_token:
            raise ValueError("Worker callback token is invalid.")
        if run.status in {"queued", "launching", "planned"}:
            run.status = "running"
            run.updated_at = utc_now()
            run.events.append(RunEvent(kind="worker", stage="Worker", message="Worker claimed run payload."))
            run = self.store.create_run(run)
        return run

    def recover_stale_runs(self, *, stale_after_seconds: int = 3600, max_retries: int | None = None) -> list[AutomationRun]:
        recovered = self.lifecycle.recover_stale_runs(stale_after_seconds=stale_after_seconds, max_retries=max_retries, launcher=self.launcher)
        for run in recovered:
            try:
                notify_run_recovered(self.config, run, run.recovery_reason)
                notify_run_requeued(self.config, run, run.recovery_reason)
            except Exception:
                pass
        return recovered

    def cleanup_orphan_runs(self, *, stale_after_seconds: int = 300, max_retries: int | None = None) -> list[AutomationRun]:
        cleaned = self.lifecycle.cleanup_orphan_runs(stale_after_seconds=stale_after_seconds, max_retries=max_retries, launcher=self.launcher)
        for run in cleaned:
            try:
                notify_orphan_cleanup(self.config, run, run.recovery_reason)
                notify_run_requeued(self.config, run, run.recovery_reason)
            except Exception:
                pass
        return cleaned

    def snapshot_recovery_state(self, *, stale_after_seconds: int = 3600) -> dict[str, Any]:
        return self.lifecycle.snapshot(stale_after_seconds=stale_after_seconds, launcher=self.launcher)

    def reconcile_ec2_runs(self) -> list[dict[str, Any]]:
        if hasattr(self.lifecycle, "reconcile_ec2_runs"):
            return self.lifecycle.reconcile_ec2_runs(launcher=self.launcher)  # type: ignore[attr-defined]
        return []

    def claim_next_run(self, worker_id: str) -> AutomationRun | None:
        if not hasattr(self.store, "claim_next_queued_run"):
            raise ValueError("The configured run store does not support queue claiming.")
        return self.store.claim_next_queued_run(worker_id)  # type: ignore[attr-defined]

    def register_worker(self, worker_id: str, *, hostname: str | None = None, capabilities: dict[str, object] | None = None, version: str | None = None) -> dict[str, object]:
        if not hasattr(self.store, "register_worker"):
            raise ValueError("The configured run store does not support worker registration.")
        return self.store.register_worker(worker_id, hostname=hostname, capabilities=capabilities, version=version)  # type: ignore[attr-defined]

    def list_workers(self) -> list[dict[str, object]]:
        if not hasattr(self.store, "list_workers"):
            return []
        return self.store.list_workers()  # type: ignore[attr-defined]

    def store_run_artifacts(
        self,
        run_id: str,
        *,
        summary: RunSummary | dict[str, Any] | None = None,
        source_dir: str | None = None,
        transcript: str | None = None,
        diff: str | None = None,
        test_log: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        self.store.get_run(run_id)
        return self.artifact_store.store_run_artifacts(
            run_id,
            transcript=transcript,
            diff=diff,
            test_log=test_log,
            summary=summary,
            source_dir=source_dir,
            metadata=metadata,
        )

    def post_event(self, run_id: str, callback_token: str, *, kind: str = "info", stage: str | None = None, message: str, details: dict[str, Any] | None = None) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.callback_token != callback_token:
            raise ValueError("Worker callback token is invalid.")
        saved = self.store.append_event(run_id, RunEvent(kind=kind, stage=stage, message=message, details=details or {}))
        if kind == "pr_created":
            if details:
                if isinstance(details.get("pr_url"), str) and details["pr_url"].strip():
                    saved.pr_url = details["pr_url"].strip()
                if isinstance(details.get("branch_name"), str) and details["branch_name"].strip():
                    saved.branch_name = details["branch_name"].strip()
                changed_files = details.get("changed_files")
                if isinstance(changed_files, list):
                    saved.changed_files = [str(item) for item in changed_files if str(item).strip()]
                saved = self.store.create_run(saved)
            try:
                if saved.pr_url:
                    notify_pr_created(self.config, saved)
            except Exception:
                pass
        return saved

    def complete_run(self, run_id: str, callback_token: str, summary: RunSummary | dict[str, Any]) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.callback_token != callback_token:
            raise ValueError("Worker callback token is invalid.")
        payload = summary if isinstance(summary, dict) else {
            "status": summary.status,
            "workspace": summary.workspace,
            "changed_files": summary.changed_files,
            "branch_name": summary.branch_name,
            "pr_url": summary.pr_url,
            "failure": summary.failure,
            "result": "ok" if summary.status == "completed" else "failed",
        }
        run.status = str(payload.get("status") or run.status)
        run.workspace = payload.get("workspace") if isinstance(payload.get("workspace"), str) else run.workspace
        run.changed_files = list(payload.get("changed_files") or run.changed_files)
        run.branch_name = payload.get("branch_name") if isinstance(payload.get("branch_name"), str) else run.branch_name
        run.pr_url = payload.get("pr_url") if isinstance(payload.get("pr_url"), str) else run.pr_url
        run.failure = payload.get("failure") if isinstance(payload.get("failure"), str) else run.failure
        run.result = payload.get("result") if isinstance(payload.get("result"), str) else run.result
        run.finished_at = utc_now()
        run.updated_at = run.finished_at
        run.events.append(RunEvent(kind="complete", stage="Worker", message=f"Worker finished with status `{run.status}`."))
        saved = self.store.create_run(run)
        try:
            if saved.pr_url and not any(event.kind == "pr_created" for event in saved.events):
                notify_pr_created(self.config, saved)
            notify_run_completed(self.config, saved)
        except Exception:
            pass
        return saved
