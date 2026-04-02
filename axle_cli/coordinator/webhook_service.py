from __future__ import annotations

import hashlib
import hmac
import json
import os
from datetime import datetime, timezone
from uuid import NAMESPACE_URL, uuid5
from typing import Any

from ..jira import normalize_jira_issue
from ..models import AutomationRun, SavedConfig
from .idempotency import (
    FileWebhookIdempotencyLedger,
    normalize_webhook_event_key,
    normalize_webhook_event_type,
)
from .run_store import FileBackedRunStore, SqliteRunStore
from .worker_launcher import Ec2WorkerLauncher

WEBHOOK_SECRET_ENV_VARS = ("AXLE_JIRA_WEBHOOK_SECRET", "AXLE_WEBHOOK_SECRET")
WEBHOOK_SIGNATURE_HEADER_NAMES = (
    "X-Axle-Webhook-Signature",
    "X-Axle-Webhook-Sha256",
    "X-Hub-Signature-256",
)
WEBHOOK_TIMESTAMP_HEADER_NAMES = ("X-Axle-Webhook-Timestamp", "X-Axle-Timestamp")
SUPPORTED_WEBHOOK_EVENT_TYPES = {
    "jira:issue_created",
    "jira:issue_updated",
    "issue_created",
    "issue_updated",
}
CLOCK_SKEW_SECONDS = 300


def _configured_webhook_secret(explicit: str | None = None) -> str | None:
    if explicit is not None:
        secret = explicit.strip()
        return secret or None
    for env_name in WEBHOOK_SECRET_ENV_VARS:
        value = os.getenv(env_name, "").strip()
        if value:
            return value
    return None


def validate_webhook_secret(provided_secret: str | None, webhook_secret: str | None = None) -> None:
    expected = _configured_webhook_secret(webhook_secret)
    if not expected:
        return
    provided = (provided_secret or "").strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise ValueError("Jira webhook secret is invalid.")


def _canonical_webhook_payload(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _normalize_signature(signature: str | None) -> str | None:
    if signature is None:
        return None
    text = signature.strip()
    if not text:
        return None
    for prefix in ("sha256=", "sha256:", "SHA256=", "SHA256:"):
        if text.startswith(prefix):
            return text[len(prefix) :].strip() or None
    return text


def _signature_payload(timestamp: str | None, payload: dict[str, Any]) -> str:
    body = _canonical_webhook_payload(payload)
    if timestamp:
        return f"{timestamp.strip()}.{body}"
    return body


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        try:
            return datetime.fromtimestamp(float(text), tz=timezone.utc)
        except (TypeError, ValueError):
            return None


def sign_webhook_payload(
    payload: dict[str, Any],
    webhook_secret: str,
    *,
    timestamp: str | None = None,
) -> str:
    secret = webhook_secret.strip()
    if not secret:
        raise ValueError("A webhook secret is required to sign payloads.")
    digest = hmac.new(secret.encode("utf-8"), _signature_payload(timestamp, payload).encode("utf-8"), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def validate_webhook_signature(
    payload: dict[str, Any],
    provided_signature: str | None,
    webhook_secret: str | None = None,
    *,
    timestamp: str | None = None,
    max_clock_skew_seconds: int = CLOCK_SKEW_SECONDS,
) -> None:
    expected = _configured_webhook_secret(webhook_secret)
    if not expected:
        return
    signature = _normalize_signature(provided_signature)
    if provided_signature is not None and not signature:
        raise ValueError("Webhook signature is invalid.")
    if not signature:
        return
    parsed_timestamp = _parse_timestamp(timestamp)
    if parsed_timestamp is not None:
        now = datetime.now(timezone.utc)
        skew = abs((now - parsed_timestamp).total_seconds())
        if skew > max_clock_skew_seconds:
            raise ValueError("Webhook signature timestamp is too old.")
    digest = hmac.new(
        expected.encode("utf-8"),
        _signature_payload(timestamp, payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, digest):
        raise ValueError("Webhook signature is invalid.")


def validate_webhook_auth(
    payload: dict[str, Any],
    *,
    provided_secret: str | None = None,
    provided_signature: str | None = None,
    webhook_secret: str | None = None,
    timestamp: str | None = None,
) -> None:
    expected = _configured_webhook_secret(webhook_secret)
    if not expected:
        return
    if provided_signature:
        validate_webhook_signature(payload, provided_signature, expected, timestamp=timestamp)
        return
    validate_webhook_secret(provided_secret, expected)


def ensure_supported_webhook_event(payload: dict[str, Any]) -> str | None:
    event_type = normalize_webhook_event_type(payload)
    if not event_type:
        return None
    normalized = event_type.strip().lower()
    if normalized in SUPPORTED_WEBHOOK_EVENT_TYPES:
        return normalized
    if normalized.startswith("jira:issue_") or normalized.startswith("issue_"):
        raise ValueError(f"Unsupported Jira webhook event type `{event_type}`. Only issue-created and issue-updated events can create runs.")
    raise ValueError(f"Unsupported webhook event type `{event_type}`.")


def normalize_webhook_issue_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return normalize_jira_issue(payload)


def _derived_run_id(event_key: str) -> str:
    return str(uuid5(NAMESPACE_URL, event_key))


def handle_jira_webhook(
    payload: dict[str, object],
    config: SavedConfig,
    store: FileBackedRunStore | SqliteRunStore | None = None,
    launcher: Ec2WorkerLauncher | None = None,
    *,
    launch_worker: bool = False,
    coordinator_url: str | None = None,
    provided_secret: str | None = None,
    provided_signature: str | None = None,
    webhook_timestamp: str | None = None,
    webhook_secret: str | None = None,
) -> AutomationRun:
    from .api import CoordinatorService

    validate_webhook_auth(
        payload,
        provided_secret=provided_secret,
        provided_signature=provided_signature,
        webhook_secret=webhook_secret,
        timestamp=webhook_timestamp,
    )
    event_type = ensure_supported_webhook_event(payload)
    issue = normalize_webhook_issue_payload(payload)
    issue_key = str(issue.get("key") or "").strip().upper()
    if not issue_key:
        raise ValueError("Jira webhook payload did not include an issue key.")
    ledger = FileWebhookIdempotencyLedger(getattr(store, "root", None))
    event_key = normalize_webhook_event_key(payload)
    reservation = ledger.reserve(
        event_key,
        _derived_run_id(event_key),
        payload=payload,
        event_type=event_type,
        signature_digest=_normalize_signature(provided_signature),
    )
    service = CoordinatorService(config, store=store, launcher=launcher)
    if reservation.duplicate:
        try:
            return service.store.get_run(reservation.run_id)
        except FileNotFoundError:
            pass
    try:
        run = service.create_run_from_issue_key(
            issue_key,
            launch_worker=launch_worker,
            coordinator_url=coordinator_url,
            run_id=reservation.run_id,
        )
    except Exception:
        run = service.create_run_from_issue(
            issue,
            launch_worker=launch_worker,
            coordinator_url=coordinator_url,
            run_id=reservation.run_id,
            event_stage="Webhook",
        )
    ledger.commit(reservation)
    return run


def create_run_from_webhook(
    payload: dict[str, object],
    store: FileBackedRunStore | SqliteRunStore | None = None,
    launcher: Ec2WorkerLauncher | None = None,
    *,
    launch_worker: bool = False,
    coordinator_url: str | None = None,
    provided_secret: str | None = None,
    provided_signature: str | None = None,
    webhook_timestamp: str | None = None,
    webhook_secret: str | None = None,
) -> dict[str, object]:
    from ..state import load_config
    from .api import CoordinatorService

    validate_webhook_auth(
        payload,
        provided_secret=provided_secret,
        provided_signature=provided_signature,
        webhook_secret=webhook_secret,
        timestamp=webhook_timestamp,
    )
    event_type = ensure_supported_webhook_event(payload)
    ledger = FileWebhookIdempotencyLedger(getattr(store, "root", None))
    service = CoordinatorService(load_config(), store=store, launcher=launcher)
    run = dict(payload)
    normalized_issue = normalize_webhook_issue_payload(run) if run.get("issue") or run.get("fields") else {}
    if normalized_issue.get("key"):
        run.setdefault("issue_key", normalized_issue["key"])
        run.setdefault("issue_url", normalized_issue.get("url") or "")
        run.setdefault("task", normalized_issue.get("summary") or "")
    run.setdefault("status", "queued")
    event_key = normalize_webhook_event_key(run)
    reservation = ledger.reserve(
        event_key,
        _derived_run_id(event_key),
        payload=run,
        event_type=event_type,
        signature_digest=_normalize_signature(provided_signature),
    )
    if "run_id" not in run:
        run["run_id"] = reservation.run_id
    if reservation.duplicate:
        try:
            return service.store.get_run(reservation.run_id).to_json()
        except FileNotFoundError:
            pass
    saved = service.create_run_from_payload(
        run,
        launch_worker=launch_worker,
        coordinator_url=coordinator_url,
        run_id=reservation.run_id,
    )
    ledger.commit(reservation)
    return saved.to_json()
