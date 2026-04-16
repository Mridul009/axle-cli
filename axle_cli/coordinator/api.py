from __future__ import annotations

import json
from typing import Any
from pathlib import Path
from uuid import uuid4

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
        self.webhook_events_dir = Path(store_root) / "webhook-events" if store_root else None
        if self.webhook_events_dir is not None:
            self.webhook_events_dir.mkdir(parents=True, exist_ok=True)

    def _webhook_event_path(self, issue_key: str) -> Path | None:
        if self.webhook_events_dir is None:
            return None
        normalized = issue_key.strip().upper()
        if not normalized:
            return None
        return self.webhook_events_dir / f"{normalized}.json"

    def _write_json_atomic(self, path: Path, payload: dict[str, Any]) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)

    def record_webhook_event(
        self,
        issue_key: str,
        payload: dict[str, Any],
        *,
        processing_status: str = "received",
        run_id: str | None = None,
        error: str | None = None,
    ) -> None:
        path = self._webhook_event_path(issue_key)
        if path is None:
            return
        existing: dict[str, Any] = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        received_at = str(existing.get("received_at") or utc_now().isoformat())
        body = {
            "issue_key": issue_key.strip().upper(),
            "received_at": received_at,
            "updated_at": utc_now().isoformat(),
            "processing_status": processing_status,
            "run_id": run_id,
            "error": error,
            "payload": payload,
        }
        self._write_json_atomic(path, body)

    def latest_webhook_event_for_issue(self, issue_key: str) -> dict[str, Any] | None:
        path = self._webhook_event_path(issue_key)
        if path is None or not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def latest_run_for_issue(self, issue_key: str) -> AutomationRun | None:
        if not hasattr(self.store, "latest_run_for_issue"):
            return None
        return self.store.latest_run_for_issue(issue_key)  # type: ignore[attr-defined]

    def list_runs(self) -> list[AutomationRun]:
        if not hasattr(self.store, "list_runs"):
            return []
        runs = self.store.list_runs()  # type: ignore[attr-defined]
        return sorted(runs, key=lambda run: (run.created_at, run.updated_at or run.created_at, run.run_id), reverse=True)

    def issue_status(self, issue_key: str) -> dict[str, Any] | None:
        run = self.latest_run_for_issue(issue_key)
        if run is None:
            return None
        last_event = run.events[-1] if run.events else None
        return {
            "issue_key": run.issue_key,
            "run_id": run.run_id,
            "status": run.status,
            "result": run.result,
            "last_stage": last_event.stage if last_event else None,
            "last_event": last_event.message if last_event else None,
            "pr_url": run.pr_url,
            "changed_files": run.changed_files,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
        }

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

    def list_queues(self) -> list[dict[str, object]]:
        if hasattr(self.store, "list_queues"):
            return self.store.list_queues()  # type: ignore[attr-defined]
        queues: dict[str, dict[str, object]] = {}
        for run in self.store.list_runs():
            queue_name = run.queue_name or "default"
            queue = queues.setdefault(queue_name, {"queue_name": queue_name, "total": 0})
            queue[run.status] = int(queue.get(run.status, 0)) + 1
            queue["total"] = int(queue.get("total", 0)) + 1
        return sorted(queues.values(), key=lambda item: str(item["queue_name"]))

    def run_events(self, run_id: str) -> list[dict[str, Any]]:
        run = self.store.get_run(run_id)
        return [event.to_json() for event in run.events]

    def run_timeline(self, run_id: str) -> dict[str, Any]:
        run = self.store.get_run(run_id)
        return {
            "run_id": run.run_id,
            "issue_key": run.issue_key,
            "status": run.status,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "updated_at": run.updated_at.isoformat() if run.updated_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
            "events": [event.to_json() for event in run.events],
        }

    def run_artifacts(self, run_id: str) -> dict[str, Any]:
        self.store.get_run(run_id)
        return self.artifact_store.load_manifest(run_id)

    def read_run_artifact(self, run_id: str, name: str) -> str:
        self.store.get_run(run_id)
        return self.artifact_store.read_artifact(run_id, name)

    def pause_run(self, run_id: str, *, reason: str | None = None) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.status not in {"queued", "planned", "launching"}:
            raise ValueError(f"Run `{run_id}` cannot be paused from status `{run.status}`.")
        previous_status = run.status
        run.status = "paused"
        run.paused_reason = (reason or "").strip() or None
        run.paused_from_status = previous_status
        run.updated_at = utc_now()
        run.events.append(
            RunEvent(
                kind="paused",
                stage="Queue",
                message=f"Run paused from `{previous_status}`.",
                details={"reason": run.paused_reason or ""},
            )
        )
        return self.store.create_run(run)

    def resume_run(self, run_id: str) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.status != "paused":
            raise ValueError(f"Run `{run_id}` is not paused.")
        resumed_status = run.paused_from_status if run.paused_from_status in {"queued", "planned", "launching"} else "queued"
        run.status = resumed_status
        run.paused_reason = None
        run.paused_from_status = None
        run.updated_at = utc_now()
        run.events.append(RunEvent(kind="resumed", stage="Queue", message=f"Run resumed as `{resumed_status}`."))
        return self.store.create_run(run)

    def cancel_run(self, run_id: str, *, reason: str | None = None) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.status in {"completed", "failed", "cancelled"}:
            raise ValueError(f"Run `{run_id}` cannot be cancelled from status `{run.status}`.")
        now = utc_now()
        clean_reason = (reason or "").strip() or None
        if run.status == "running":
            run.status = "cancel_requested"
            run.cancel_requested_at = now
            message = "Cancellation requested for running run."
            kind = "cancel_requested"
        else:
            run.status = "cancelled"
            run.cancelled_at = now
            run.finished_at = now
            run.result = "cancelled"
            message = "Run cancelled before execution."
            kind = "cancelled"
        run.updated_at = now
        run.events.append(RunEvent(kind=kind, stage="Queue", message=message, details={"reason": clean_reason or ""}))
        return self.store.create_run(run)

    def retry_run(self, run_id: str, *, force: bool = False) -> AutomationRun:
        run = self.store.get_run(run_id)
        if run.status not in {"failed", "cancelled", "completed"}:
            raise ValueError(f"Run `{run_id}` cannot be retried from status `{run.status}`.")
        if not force and run.retry_count >= run.max_retries:
            raise ValueError(f"Run `{run_id}` has reached its retry limit.")
        run.retry_count += 1
        run.status = "queued"
        run.result = None
        run.failure = None
        run.finished_at = None
        run.cancel_requested_at = None
        run.cancelled_at = None
        run.worker_instance_id = None
        run.updated_at = utc_now()
        run.events.append(RunEvent(kind="retry_requested", stage="Queue", message=f"Run requeued for retry {run.retry_count}."))
        return self.store.create_run(run)

    def update_run_priority(self, run_id: str, *, priority: int, queue_name: str | None = None) -> AutomationRun:
        run = self.store.get_run(run_id)
        run.priority = int(priority)
        if queue_name is not None:
            run.queue_name = queue_name.strip() or "default"
        run.updated_at = utc_now()
        run.events.append(
            RunEvent(
                kind="queue_updated",
                stage="Queue",
                message=f"Run priority set to {run.priority}.",
                details={"priority": str(run.priority), "queue_name": run.queue_name},
            )
        )
        return self.store.create_run(run)

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
        if run.status == "cancelled":
            run.cancelled_at = run.finished_at
        run.events.append(RunEvent(kind="complete", stage="Worker", message=f"Worker finished with status `{run.status}`."))
        saved = self.store.create_run(run)
        try:
            if saved.pr_url and not any(event.kind == "pr_created" for event in saved.events):
                notify_pr_created(self.config, saved)
            notify_run_completed(self.config, saved)
        except Exception:
            pass
        return saved
