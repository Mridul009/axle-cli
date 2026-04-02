from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Iterable

from ..models import AutomationRun, RunEvent, utc_now
from .worker_launcher import Ec2WorkerLauncher


@dataclass(frozen=True)
class RecoveryResult:
    run_id: str
    status: str
    retry_count: int
    changed: bool
    reason: str


def _as_timestamp(run: AutomationRun) -> Any:
    return run.updated_at or run.created_at


def _worker_is_active(worker: dict[str, Any], *, stale_after_seconds: int) -> bool:
    from datetime import datetime, timezone

    last_seen = worker.get("last_seen_at")
    if not last_seen:
        return False
    try:
        last_seen_at = datetime.fromisoformat(str(last_seen))
    except ValueError:
        return False
    if last_seen_at.tzinfo is None:
        last_seen_at = last_seen_at.replace(tzinfo=timezone.utc)
    age = utc_now() - last_seen_at
    return age <= timedelta(seconds=stale_after_seconds)


def active_worker_ids(store, *, stale_after_seconds: int = 300) -> set[str]:
    if not hasattr(store, "list_workers"):
        return set()
    workers = store.list_workers()  # type: ignore[attr-defined]
    active: set[str] = set()
    for worker in workers:
        if not isinstance(worker, dict):
            continue
        worker_id = str(worker.get("worker_id") or "").strip()
        if worker_id and _worker_is_active(worker, stale_after_seconds=stale_after_seconds):
            active.add(worker_id)
    return active


def _is_local_worker_id(worker_id: str) -> bool:
    text = worker_id.strip()
    return text.startswith("local-") and text[6:].isdigit()


def _local_worker_is_alive(worker_id: str) -> bool:
    if not _is_local_worker_id(worker_id):
        return False
    try:
        pid = int(worker_id.split("-", 1)[1])
        import os

        os.kill(pid, 0)
    except (ValueError, OSError):
        return False
    return True


def reconcile_ec2_runs(store, launcher: Ec2WorkerLauncher | None) -> list[dict[str, Any]]:
    if launcher is None:
        return []
    reconciled: list[dict[str, Any]] = []
    for run in getattr(store, "list_runs")():
        if not isinstance(run, AutomationRun):
            continue
        if not (run.worker_launch_spec or run.worker_instance_id):
            continue
        reconciled.append(launcher.reconcile_run_instance(run))
    return reconciled


def _requeue_run(
    run: AutomationRun,
    reason: str,
    *,
    clear_worker: bool = True,
    details: dict[str, str] | None = None,
) -> AutomationRun:
    run.retry_count += 1
    run.last_recovery_at = utc_now()
    run.recovery_state = "queued"
    run.recovery_reason = reason
    run.status = "queued" if run.retry_count <= run.max_retries else "failed"
    if clear_worker:
        run.worker_instance_id = None
    if run.status == "failed":
        run.recovery_state = "failed"
    event_details = {"retry_count": str(run.retry_count)}
    if details:
        event_details.update(details)
    run.events.append(RunEvent(kind="recovery", stage="Coordinator", message=reason, details=event_details))
    return run


def recover_stale_runs(
    store,
    *,
    stale_after_seconds: int = 3600,
    max_retries: int | None = None,
    launcher: Ec2WorkerLauncher | None = None,
) -> list[AutomationRun]:
    recovered: list[AutomationRun] = []
    now = utc_now()
    for run in getattr(store, "list_runs")():
        if not isinstance(run, AutomationRun):
            continue
        if run.status not in {"queued", "launching", "running"}:
            continue
        if launcher is None and run.status in {"launching", "running"} and str(run.worker_instance_id or "").strip():
            continue
        timestamp = _as_timestamp(run)
        if timestamp is None:
            continue
        if (now - timestamp).total_seconds() < stale_after_seconds:
            continue
        if max_retries is not None:
            run.max_retries = max_retries
        termination_note = ""
        if launcher is not None:
            try:
                reconciliation = launcher.terminate_run_instance(run)
                if reconciliation.get("terminated"):
                    termination_note = (
                        f" EC2 instance `{reconciliation.get('instance_id')}` was terminated."
                        if reconciliation.get("instance_id")
                        else " EC2 instance was terminated."
                    )
                elif reconciliation.get("instance_state"):
                    termination_note = f" EC2 instance state: {reconciliation.get('instance_state')}."
            except Exception:
                pass
        reason = f"Run was stale for more than {stale_after_seconds} seconds.{termination_note}"
        saved = store.create_run(_requeue_run(run, reason))  # type: ignore[attr-defined]
        recovered.append(saved)
    return recovered


def cleanup_orphan_runs(
    store,
    *,
    stale_after_seconds: int = 300,
    max_retries: int | None = None,
    launcher: Ec2WorkerLauncher | None = None,
) -> list[AutomationRun]:
    active_ids = active_worker_ids(store, stale_after_seconds=stale_after_seconds)
    cleaned: list[AutomationRun] = []
    for run in getattr(store, "list_runs")():
        if not isinstance(run, AutomationRun):
            continue
        if run.status not in {"launching", "running"}:
            continue
        worker_id = str(run.worker_instance_id or "").strip()
        if not worker_id:
            continue
        if worker_id in active_ids:
            continue
        if _local_worker_is_alive(worker_id):
            continue
        if max_retries is not None:
            run.max_retries = max_retries
        termination_note = ""
        if launcher is not None:
            try:
                reconciliation = launcher.terminate_run_instance(run)
                if reconciliation.get("terminated"):
                    termination_note = (
                        f" EC2 instance `{reconciliation.get('instance_id')}` was terminated."
                        if reconciliation.get("instance_id")
                        else " EC2 instance was terminated."
                    )
                elif reconciliation.get("instance_state"):
                    termination_note = f" EC2 instance state: {reconciliation.get('instance_state')}."
            except Exception:
                pass
        run.orphaned_at = utc_now()
        reason = f"Worker `{worker_id}` was orphaned and the run was requeued.{termination_note}"
        saved = store.create_run(_requeue_run(run, reason, details={"worker_id": worker_id}))  # type: ignore[attr-defined]
        cleaned.append(saved)
    return cleaned


def snapshot_recovery_state(
    store,
    *,
    stale_after_seconds: int = 3600,
    launcher: Ec2WorkerLauncher | None = None,
) -> dict[str, Any]:
    runs = getattr(store, "list_runs")()
    stale = recover_stale_runs(store, stale_after_seconds=stale_after_seconds, launcher=launcher)
    orphaned = cleanup_orphan_runs(store, stale_after_seconds=stale_after_seconds, launcher=launcher)
    reconciled = reconcile_ec2_runs(store, launcher)
    return {
        "total_runs": len(runs),
        "recovered_runs": [run.run_id for run in stale],
        "cleaned_runs": [run.run_id for run in orphaned],
        "ec2_reconciled": reconciled,
    }
