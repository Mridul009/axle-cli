from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..models import AutomationRun
from .idempotency import FileWebhookIdempotencyLedger, normalize_webhook_event_key
from .recovery import cleanup_orphan_runs, recover_stale_runs, reconcile_ec2_runs, snapshot_recovery_state
from .worker_launcher import Ec2WorkerLauncher


@dataclass
class LifecycleManager:
    store: Any

    @property
    def ledger(self) -> FileWebhookIdempotencyLedger:
        root = getattr(self.store, "root", None)
        return FileWebhookIdempotencyLedger(root)

    def dedupe_webhook_event(self, payload: dict[str, Any], *, run_id: str) -> tuple[bool, str]:
        reservation = self.ledger.reserve(normalize_webhook_event_key(payload), run_id, payload=payload)
        return reservation.duplicate, reservation.run_id

    def commit_webhook_event(self, payload: dict[str, Any], run_id: str) -> None:
        reservation = self.ledger.reserve(normalize_webhook_event_key(payload), run_id, payload=payload)
        self.ledger.commit(reservation)

    def recover_stale_runs(self, *, stale_after_seconds: int = 3600, max_retries: int | None = None, launcher: Ec2WorkerLauncher | None = None) -> list[AutomationRun]:
        return recover_stale_runs(self.store, stale_after_seconds=stale_after_seconds, max_retries=max_retries, launcher=launcher)

    def cleanup_orphan_runs(self, *, stale_after_seconds: int = 300, max_retries: int | None = None, launcher: Ec2WorkerLauncher | None = None) -> list[AutomationRun]:
        return cleanup_orphan_runs(self.store, stale_after_seconds=stale_after_seconds, max_retries=max_retries, launcher=launcher)

    def reconcile_ec2_runs(self, *, launcher: Ec2WorkerLauncher | None = None) -> list[dict[str, Any]]:
        return reconcile_ec2_runs(self.store, launcher)

    def snapshot(self, *, stale_after_seconds: int = 3600, launcher: Ec2WorkerLauncher | None = None) -> dict[str, Any]:
        return snapshot_recovery_state(self.store, stale_after_seconds=stale_after_seconds, launcher=launcher)
