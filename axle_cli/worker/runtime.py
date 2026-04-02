from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any

try:
    import requests
except ModuleNotFoundError:  # pragma: no cover
    requests = None

from ..models import AutomationRun, RunSummary, SavedConfig
from ..session import SessionRunner
from ..state import load_config
from ..terminal import Terminal
from ..coordinator.artifact_store import build_artifact_store
from ..coordinator.store_factory import open_run_store


_STAGE_PROGRESS = {
    "Workspace": 10,
    "Setup": 20,
    "Testing": 35,
    "Inspect": 45,
    "Goose": 65,
    "Worker": 85,
}


def _is_run_store(store: Any) -> bool:
    return hasattr(store, "get_run") and hasattr(store, "create_run")


def _require_requests():
    if requests is None:  # pragma: no cover
        raise RuntimeError("The `requests` package is required for networked worker mode.")
    return requests


def fetch_run_payload(
    run_id: str | Any,
    store: Any | str,
    callback_token: str | None = None,
) -> AutomationRun:
    from ..coordinator.api import CoordinatorService

    if _is_run_store(run_id):
        run_id, store = store, run_id
    if not isinstance(run_id, str):
        raise TypeError("run_id must be a string.")
    if not _is_run_store(store):
        raise TypeError("store must provide the run-store interface.")
    service = CoordinatorService(load_config(), store=store)
    run = store.get_run(run_id)
    return service.get_worker_payload(run_id, callback_token or run.callback_token)


def post_callback(
    run_id: str | Any,
    payload: dict[str, object] | str,
    store: Any | None = None,
    callback_token: str | None = None,
    **kwargs,
):
    from ..coordinator.api import CoordinatorService

    if _is_run_store(run_id):
        if not isinstance(payload, str):
            raise TypeError("run_id must be a string when store is passed positionally.")
        run_id, store, payload = payload, run_id, (store if isinstance(store, (dict, str)) else (kwargs or {}))
        if kwargs and isinstance(payload, dict):
            payload = {**payload, **kwargs}
    if store is None:
        store = kwargs.pop("store", None)
    if store is None or not _is_run_store(store):
        raise TypeError("store must provide the run-store interface.")
    if kwargs and isinstance(payload, str):
        payload = {"status": payload, **kwargs}
    elif kwargs and isinstance(payload, dict):
        payload = {**payload, **kwargs}

    service = CoordinatorService(load_config(), store=store)
    run = store.get_run(run_id)
    token = callback_token or run.callback_token
    if isinstance(payload, str):
        return service.complete_run(run_id, token, {"status": payload, "result": "ok" if payload == "completed" else payload})
    status = payload.get("status")
    if status in {"completed", "failed"}:
        return service.complete_run(run_id, token, payload)
    return service.post_event(
        run_id,
        token,
        kind=str(payload.get("kind") or "info"),
        stage=str(payload.get("stage")) if payload.get("stage") is not None else None,
        message=str(payload.get("message") or payload.get("result") or "worker callback"),
        details={key: value for key, value in payload.items() if key not in {"kind", "stage", "message"}},
    )


def register_remote_worker(coordinator_url: str, worker_token: str | None, worker_id: str, *, version: str = "0.1.0") -> dict[str, Any]:
    client = _require_requests()
    response = client.post(
        f"{coordinator_url.rstrip('/')}/api/workers/register",
        json={
            "worker_id": worker_id,
            "hostname": socket.gethostname(),
            "capabilities": {"executor": "axle"},
            "version": version,
        },
        headers=_auth_headers(worker_token),
        timeout=15,
    )
    response.raise_for_status()
    return dict(response.json())


def claim_remote_run(coordinator_url: str, worker_token: str | None, worker_id: str) -> AutomationRun | None:
    client = _require_requests()
    response = client.post(
        f"{coordinator_url.rstrip('/')}/api/workers/claim",
        json={"worker_id": worker_id},
        headers=_auth_headers(worker_token),
        timeout=30,
    )
    response.raise_for_status()
    payload = dict(response.json())
    run = payload.get("run")
    if not isinstance(run, dict):
        return None
    return AutomationRun.from_json(run)


def post_remote_callback(coordinator_url: str, run_id: str, callback_token: str, payload: dict[str, object]) -> dict[str, Any]:
    client = _require_requests()
    status = str(payload.get("status") or "")
    suffix = "complete" if status in {"completed", "failed"} else "events"
    response = client.post(
        f"{coordinator_url.rstrip('/')}/api/runs/{run_id}/{suffix}",
        json={"callback_token": callback_token, **payload},
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json())


def post_remote_artifacts(coordinator_url: str, run_id: str, callback_token: str, payload: dict[str, object]) -> dict[str, Any]:
    client = _require_requests()
    response = client.post(
        f"{coordinator_url.rstrip('/')}/api/runs/{run_id}/artifacts",
        json={"callback_token": callback_token, **payload},
        timeout=30,
    )
    response.raise_for_status()
    return dict(response.json())


def _auth_headers(token: str | None) -> dict[str, str]:
    if not token:
        return {}
    return {"Authorization": f"Bearer {token}"}


def _read_optional_text(path: Path | None) -> str | None:
    if path is None or not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _summary_payload(summary: RunSummary) -> dict[str, object]:
    return {
        "run_id": summary.run_id,
        "status": summary.status,
        "repository": summary.repository,
        "workspace": summary.workspace,
        "changed_files": summary.changed_files,
        "pr_url": summary.pr_url,
        "branch_name": summary.branch_name,
        "diff_path": summary.diff_path,
        "goose_session_name": summary.goose_session_name,
        "risks": summary.risks,
        "failure": summary.failure,
        "started_at": summary.started_at.isoformat() if summary.started_at else None,
        "finished_at": summary.finished_at.isoformat() if summary.finished_at else None,
        "test_result": {
            "command": summary.test_result.command,
            "exit_code": summary.test_result.exit_code,
            "output": summary.test_result.output,
            "timed_out": summary.test_result.timed_out,
        }
        if summary.test_result
        else None,
    }


def _progress_payload(event: dict[str, str]) -> dict[str, str]:
    payload = dict(event)
    stage = str(payload.get("stage") or "").strip()
    kind = str(payload.get("kind") or "").strip().lower()
    progress = _STAGE_PROGRESS.get(stage)
    if kind == "complete":
        progress = 100
    if progress is not None:
        payload["progress_percent"] = str(progress)
    return payload


@contextmanager
def _heartbeat_loop(
    emitter,
    *,
    interval_seconds: float = 15.0,
    progress_percent: int = 75,
):
    stop_event = threading.Event()

    def _run() -> None:
        while not stop_event.wait(interval_seconds):
            try:
                emitter(
                    {
                        "kind": "heartbeat",
                        "stage": "Worker",
                        "message": "Worker heartbeat.",
                        "progress_percent": str(progress_percent),
                    }
                )
            except Exception:
                return

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=1.0)


def execute_run(run: AutomationRun | dict[str, object], config: SavedConfig | None = None, store: Any | None = None) -> RunSummary:
    runtime_config = config or load_config()
    record = run if isinstance(run, AutomationRun) else AutomationRun.from_json(dict(run))
    callback_store = store or open_run_store()
    callback_root = getattr(callback_store, "root", None)
    artifact_root = Path(callback_root) / "artifacts" if callback_root else None
    artifact_store = build_artifact_store(root=artifact_root)

    def emit(event: dict[str, str]) -> None:
        post_callback(record.run_id, _progress_payload(event), callback_store, callback_token=record.callback_token)

    terminal = Terminal(verbose=False, event_handler=emit)
    runner = SessionRunner(runtime_config, terminal)
    with _heartbeat_loop(emit):
        summary = runner.run(record.to_run_request())
    if summary.pr_url:
        post_callback(
            record.run_id,
            {
                "kind": "pr_created",
                "stage": "Worker",
                "message": "Pull request created.",
                "pr_url": summary.pr_url,
                "branch_name": summary.branch_name or "",
                "changed_files": summary.changed_files,
            },
            callback_store,
            callback_token=record.callback_token,
        )
    artifact_store.store_run_artifacts(
        record.run_id,
        source_dir=summary.workspace,
        summary=summary,
    )
    post_callback(
        record.run_id,
        {
            "status": summary.status,
            "workspace": summary.workspace or "",
            "changed_files": summary.changed_files,
            "branch_name": summary.branch_name or "",
            "pr_url": summary.pr_url or "",
            "failure": summary.failure or "",
            "result": "ok" if summary.status == "completed" else "failed",
        },
        callback_store,
        callback_token=record.callback_token,
    )
    return summary


def execute_remote_run(run: AutomationRun, coordinator_url: str, config: SavedConfig | None = None) -> RunSummary:
    runtime_config = config or load_config()
    artifact_payload: dict[str, Any] = {}

    def emit(event: dict[str, str]) -> None:
        post_remote_callback(coordinator_url, run.run_id, run.callback_token, _progress_payload(event))

    terminal = Terminal(verbose=False, event_handler=emit)
    runner = SessionRunner(runtime_config, terminal)
    with _heartbeat_loop(emit):
        summary = runner.run(run.to_run_request())
    if summary.pr_url:
        post_remote_callback(
            coordinator_url,
            run.run_id,
            run.callback_token,
            {
                "kind": "pr_created",
                "stage": "Worker",
                "message": "Pull request created.",
                "pr_url": summary.pr_url,
                "branch_name": summary.branch_name or "",
                "changed_files": summary.changed_files,
            },
        )
    workspace_root = Path(summary.workspace) if summary.workspace else None
    post_remote_artifacts(
        coordinator_url,
        run.run_id,
        run.callback_token,
        {
            "transcript": _read_optional_text(workspace_root / "goose-transcript.log") if workspace_root else None,
            "diff": _read_optional_text(workspace_root / "changes.patch") if workspace_root else None,
            "test_log": _read_optional_text(workspace_root / "test.log") if workspace_root else None,
            "summary": _summary_payload(summary),
        },
    )
    post_remote_callback(
        coordinator_url,
        run.run_id,
        run.callback_token,
        {
            "status": summary.status,
            "workspace": summary.workspace or "",
            "changed_files": summary.changed_files,
            "branch_name": summary.branch_name or "",
            "pr_url": summary.pr_url or "",
            "failure": summary.failure or "",
            "result": "ok" if summary.status == "completed" else "failed",
        },
    )
    return summary
