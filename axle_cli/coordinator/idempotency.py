from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..models import utc_now
from .run_store import coordinator_root

IDEMPOTENCY_DIR_NAME = "idempotency"
WEBHOOK_DIR_NAME = "webhooks"


def _canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def normalize_webhook_event_type(payload: dict[str, Any]) -> str | None:
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    candidates = (
        payload.get("webhookEvent"),
        payload.get("webhook_event"),
        payload.get("webhook_event_type"),
        payload.get("event_type"),
        payload.get("eventType"),
        payload.get("issue_event_type_name"),
        payload.get("issueEventTypeName"),
        payload.get("event"),
        payload.get("type"),
        issue.get("eventType") if isinstance(issue, dict) else None,
    )
    for candidate in candidates:
        if candidate is None:
            continue
        text = str(candidate).strip().lower()
        if text:
            return text
    return None


def normalize_webhook_event_key(payload: dict[str, Any]) -> str:
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else {}
    event_type = normalize_webhook_event_type(payload)
    issue_key = str(
        payload.get("event_id")
        or payload.get("delivery_id")
        or payload.get("webhook_event_id")
        or payload.get("webhookEvent")
        or payload.get("webhook_event")
        or payload.get("issue_key")
        or payload.get("issueKey")
        or (issue.get("key") if isinstance(issue, dict) else "")
        or payload.get("key")
        or ""
    ).strip()
    updated_value: Any = payload.get("updated") or payload.get("timestamp")
    if not updated_value and isinstance(issue, dict):
        fields = issue.get("fields")
        if isinstance(fields, dict):
            updated_value = fields.get("updated")
    updated = str(updated_value or "").strip()
    if event_type and issue_key and updated:
        return f"{event_type}:{issue_key}:{updated}"
    if event_type and issue_key:
        return f"{event_type}:{issue_key}"
    if issue_key and updated:
        return f"{issue_key}:{updated}"
    if issue_key:
        return issue_key
    return _hash_text(_canonical_json(payload))


def normalize_webhook_payload_digest(payload: dict[str, Any]) -> str:
    return _hash_text(_canonical_json(payload))


@dataclass(frozen=True)
class IdempotencyReservation:
    event_key: str
    run_id: str
    duplicate: bool
    status: str
    event_type: str | None
    payload_digest: str
    signature_digest: str | None
    marker_path: Path


class FileWebhookIdempotencyLedger:
    def __init__(self, root: Path | str | None = None) -> None:
        self.root = coordinator_root(root)
        self.ledger_dir = self.root / IDEMPOTENCY_DIR_NAME / WEBHOOK_DIR_NAME
        self.ledger_dir.mkdir(parents=True, exist_ok=True)

    def _marker_path(self, event_key: str) -> Path:
        return self.ledger_dir / f"{_hash_text(event_key)}.json"

    def reserve(
        self,
        event_key: str,
        run_id: str,
        *,
        payload: dict[str, Any] | None = None,
        event_type: str | None = None,
        signature_digest: str | None = None,
    ) -> IdempotencyReservation:
        marker_path = self._marker_path(event_key)
        now = utc_now().isoformat()
        payload_digest = normalize_webhook_payload_digest(payload or {})
        record = {
            "event_key": event_key,
            "run_id": run_id,
            "status": "reserved",
            "event_type": event_type,
            "payload_digest": payload_digest,
            "signature_digest": signature_digest,
            "reserved_at": now,
            "committed_at": None,
        }
        try:
            with marker_path.open("x", encoding="utf-8") as handle:
                json.dump(record, handle, indent=2)
                handle.write("\n")
            return IdempotencyReservation(
                event_key=event_key,
                run_id=run_id,
                duplicate=False,
                status="reserved",
                event_type=event_type,
                payload_digest=payload_digest,
                signature_digest=signature_digest,
                marker_path=marker_path,
            )
        except FileExistsError:
            existing = json.loads(marker_path.read_text(encoding="utf-8"))
            existing_digest = str(existing.get("payload_digest") or "")
            if existing_digest and existing_digest != payload_digest:
                raise ValueError(f"Webhook replay conflict detected for event `{event_key}`.")
            return IdempotencyReservation(
                event_key=str(existing.get("event_key") or event_key),
                run_id=str(existing.get("run_id") or run_id),
                duplicate=True,
                status=str(existing.get("status") or "reserved"),
                event_type=str(existing.get("event_type") or event_type or "").strip() or None,
                payload_digest=existing_digest or payload_digest,
                signature_digest=str(existing.get("signature_digest") or signature_digest or "").strip() or None,
                marker_path=marker_path,
            )

    def commit(self, reservation: IdempotencyReservation) -> IdempotencyReservation:
        if not reservation.marker_path.exists():
            return reservation
        payload = json.loads(reservation.marker_path.read_text(encoding="utf-8"))
        payload["status"] = "committed"
        payload["committed_at"] = utc_now().isoformat()
        payload["run_id"] = reservation.run_id
        payload["event_type"] = reservation.event_type
        payload["payload_digest"] = reservation.payload_digest
        payload["signature_digest"] = reservation.signature_digest
        reservation.marker_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return IdempotencyReservation(
            event_key=reservation.event_key,
            run_id=reservation.run_id,
            duplicate=reservation.duplicate,
            status="committed",
            event_type=reservation.event_type,
            payload_digest=reservation.payload_digest,
            signature_digest=reservation.signature_digest,
            marker_path=reservation.marker_path,
        )

    def lookup(self, event_key: str) -> dict[str, Any] | None:
        marker_path = self._marker_path(event_key)
        if not marker_path.exists():
            return None
        return json.loads(marker_path.read_text(encoding="utf-8"))


def reserve_webhook_event(payload: dict[str, Any], run_id: str, root: Path | str | None = None) -> IdempotencyReservation:
    ledger = FileWebhookIdempotencyLedger(root)
    return ledger.reserve(
        normalize_webhook_event_key(payload),
        run_id,
        payload=payload,
        event_type=normalize_webhook_event_type(payload),
    )
