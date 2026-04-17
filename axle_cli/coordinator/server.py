from __future__ import annotations

import json
import sys
import os
import threading
import time
from importlib import resources
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from ..state import load_config
from .api import CoordinatorService
from .artifact_store import build_artifact_store
from .docker_worker_pool import DockerWorkerPoolManager, build_docker_worker_pool
from .store_factory import open_run_store
from .webhook_service import create_run_from_webhook, handle_jira_webhook
from .worker_launcher import build_worker_launcher, resolve_worker_launch_mode


DASHBOARD_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
}


def _json_response(handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    encoded = json.dumps(payload).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _text_response(handler: BaseHTTPRequestHandler, status: int, content: str, content_type: str = "text/plain; charset=utf-8") -> None:
    encoded = content.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(encoded)))
    handler.end_headers()
    handler.wfile.write(encoded)


def _bytes_response(handler: BaseHTTPRequestHandler, status: int, content: bytes, content_type: str) -> None:
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(content)))
    handler.end_headers()
    handler.wfile.write(content)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("Content-Length", "0"))
    raw = handler.rfile.read(length) if length else b"{}"
    if not raw:
        return {}
    return dict(json.loads(raw.decode("utf-8")))


def _bearer_token(handler: BaseHTTPRequestHandler) -> str | None:
    header = handler.headers.get("Authorization", "")
    if header.startswith("Bearer "):
        return header[7:].strip()
    return None


def _webhook_secret(handler: BaseHTTPRequestHandler) -> str | None:
    for header_name in ("X-Axle-Jira-Webhook-Secret", "X-Axle-Webhook-Secret"):
        value = handler.headers.get(header_name, "").strip()
        if value:
            return value
    return None


def _webhook_signature(handler: BaseHTTPRequestHandler) -> str | None:
    for header_name in ("X-Axle-Webhook-Signature", "X-Axle-Webhook-Sha256", "X-Hub-Signature-256"):
        value = handler.headers.get(header_name, "").strip()
        if value:
            return value
    return None


def _webhook_timestamp(handler: BaseHTTPRequestHandler) -> str | None:
    for header_name in ("X-Axle-Webhook-Timestamp", "X-Axle-Timestamp"):
        value = handler.headers.get(header_name, "").strip()
        if value:
            return value
    return None


def _event_details(payload: dict[str, Any]) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for key, value in payload.items():
        if key in {"callback_token", "kind", "stage", "message"}:
            continue
        if isinstance(value, list):
            details[key] = [str(item) for item in value]
        elif isinstance(value, dict):
            details[key] = {str(sub_key): str(sub_value) for sub_key, sub_value in value.items()}
        else:
            details[key] = str(value)
    return details


def _issue_key_from_webhook_payload(payload: dict[str, Any]) -> str:
    issue = payload.get("issue")
    if isinstance(issue, dict):
        issue_key = str(issue.get("key") or "").strip().upper()
        if issue_key:
            return issue_key
    return str(payload.get("issue_key") or payload.get("issueKey") or "").strip().upper()


def _server_log(message: str) -> None:
    print(message, file=sys.stdout, flush=True)


def _dashboard_asset(name: str) -> tuple[bytes, str] | None:
    safe_name = name.strip("/").split("/")[-1] or "index.html"
    if safe_name not in {"index.html", "app.css", "app.js", "mark.svg"}:
        return None
    try:
        asset = resources.files("axle_cli.dashboard").joinpath(safe_name)
        content = asset.read_bytes()
    except (FileNotFoundError, ModuleNotFoundError):
        return None
    suffix = os.path.splitext(safe_name)[1]
    return content, DASHBOARD_CONTENT_TYPES.get(suffix, "application/octet-stream")


class CoordinatorHttpServer(ThreadingHTTPServer):
    def __init__(
        self,
        server_address: tuple[str, int],
        *,
        store_root: str | None = None,
        artifact_root: str | None = None,
        artifact_backend: str | None = None,
        admin_token: str | None = None,
        worker_token: str | None = None,
        webhook_secret: str | None = None,
        worker_launch_mode: str | None = None,
        coordinator_url: str | None = None,
        watchdog_interval_seconds: int = 30,
        watchdog_stale_after_seconds: int = 180,
    ) -> None:
        self.store = open_run_store(root=store_root)
        resolved_artifact_root = artifact_root or (str(self.store.root / "artifacts") if getattr(self.store, "root", None) else None)
        self.artifact_store = build_artifact_store(root=resolved_artifact_root, backend=artifact_backend)
        self.worker_launch_mode = resolve_worker_launch_mode(worker_launch_mode)
        self.coordinator_url = (coordinator_url or os.getenv("AXLE_COORDINATOR_URL") or "").strip() or None
        if self.worker_launch_mode == "ec2" and not self.coordinator_url:
            raise ValueError("`--coordinator-url` or `AXLE_COORDINATOR_URL` is required when EC2 worker launching is enabled.")
        self.service = CoordinatorService(
            load_config(),
            store=self.store,
            launcher=build_worker_launcher(mode=self.worker_launch_mode, store_root=store_root),
            artifact_store=self.artifact_store,
        )
        self.watchdog_interval_seconds = max(5, int(watchdog_interval_seconds))
        self.watchdog_stale_after_seconds = max(self.watchdog_interval_seconds * 2, int(watchdog_stale_after_seconds))
        self._watchdog_stop = threading.Event()
        self._watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self._watchdog_thread.start()
        self.admin_token = (admin_token or "").strip() or None
        self.worker_token = (worker_token or "").strip() or self.admin_token
        env_secret = (webhook_secret or os.getenv("AXLE_JIRA_WEBHOOK_SECRET") or os.getenv("AXLE_WEBHOOK_SECRET") or "").strip()
        self.webhook_secret = env_secret or None
        self.docker_worker_pool: DockerWorkerPoolManager = build_docker_worker_pool(
            self.service,
            coordinator_url=self.coordinator_url,
            worker_token=self.worker_token,
        )
        self._autoscaler_stop = threading.Event()
        self._autoscaler_thread: threading.Thread | None = None
        if self.docker_worker_pool.enabled:
            self._autoscaler_thread = threading.Thread(target=self._autoscaler_loop, daemon=True)
            self._autoscaler_thread.start()
        super().__init__(server_address, CoordinatorRequestHandler)

    def _watchdog_loop(self) -> None:
        while not self._watchdog_stop.wait(self.watchdog_interval_seconds):
            try:
                self.service.cleanup_orphan_runs(
                    stale_after_seconds=self.watchdog_stale_after_seconds,
                    max_retries=1,
                )
                self.service.recover_stale_runs(
                    stale_after_seconds=self.watchdog_stale_after_seconds,
                    max_retries=1,
                )
            except Exception:
                continue

    def _autoscaler_loop(self) -> None:
        interval = max(1, int(self.docker_worker_pool.config.poll_interval_seconds))
        while not self._autoscaler_stop.wait(interval):
            try:
                summary = self.docker_worker_pool.reconcile_once()
                if summary.get("started") or summary.get("stopped"):
                    _server_log(
                        "[autoscale] "
                        f"queued={summary.get('queued')} "
                        f"desired={summary.get('desired')} "
                        f"running={summary.get('running')} "
                        f"started={summary.get('started')} "
                        f"stopped={summary.get('stopped')}"
                    )
            except Exception as exc:
                _server_log(f"[autoscale] error={exc}")
                continue

    def server_close(self) -> None:
        self._autoscaler_stop.set()
        if self._autoscaler_thread is not None and self._autoscaler_thread.is_alive():
            self._autoscaler_thread.join(timeout=1.0)
        self._watchdog_stop.set()
        if self._watchdog_thread.is_alive():
            self._watchdog_thread.join(timeout=1.0)
        super().server_close()


class CoordinatorRequestHandler(BaseHTTPRequestHandler):
    server: CoordinatorHttpServer

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/dashboard"}:
            asset = _dashboard_asset("index.html")
            if asset is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "dashboard_not_found"})
                return
            content, content_type = asset
            _bytes_response(self, HTTPStatus.OK, content, content_type)
            return
        if parsed.path.startswith("/dashboard/"):
            asset = _dashboard_asset(parsed.path.removeprefix("/dashboard/"))
            if asset is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "dashboard_asset_not_found"})
                return
            content, content_type = asset
            _bytes_response(self, HTTPStatus.OK, content, content_type)
            return
        if parsed.path == "/healthz":
            _json_response(self, HTTPStatus.OK, {"ok": True})
            return
        if parsed.path == "/api/workers":
            if not self._require_admin():
                return
            _json_response(self, HTTPStatus.OK, {"workers": self.server.service.list_workers()})
            return
        if parsed.path == "/api/queues":
            if not self._require_admin():
                return
            _json_response(self, HTTPStatus.OK, {"queues": self.server.service.list_queues()})
            return
        if parsed.path == "/api/runs":
            if not self._require_admin():
                return
            runs = [run.to_json() for run in self.server.service.list_runs()]
            _json_response(self, HTTPStatus.OK, {"runs": runs})
            return
        if parsed.path.startswith("/api/issues/") and parsed.path.endswith("/runs/latest"):
            if not self._require_admin():
                return
            issue_key = parsed.path.split("/")[3]
            run = self.server.service.latest_run_for_issue(issue_key)
            if run is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "issue_run_not_found", "issue_key": issue_key.upper()})
                return
            _json_response(self, HTTPStatus.OK, run.to_json())
            return
        if parsed.path.startswith("/api/issues/") and parsed.path.endswith("/status"):
            if not self._require_admin():
                return
            issue_key = parsed.path.split("/")[3]
            status = self.server.service.issue_status(issue_key)
            if status is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "issue_run_not_found", "issue_key": issue_key.upper()})
                return
            _json_response(self, HTTPStatus.OK, status)
            return
        if parsed.path.startswith("/api/issues/") and parsed.path.endswith("/webhook/latest"):
            if not self._require_admin():
                return
            issue_key = parsed.path.split("/")[3]
            event = self.server.service.latest_webhook_event_for_issue(issue_key)
            if event is None:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "issue_webhook_not_found", "issue_key": issue_key.upper()})
                return
            _json_response(self, HTTPStatus.OK, event)
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/events"):
            if not self._require_admin():
                return
            run_id = parsed.path.split("/")[3]
            try:
                events = self.server.service.run_events(run_id)
            except FileNotFoundError:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            _json_response(self, HTTPStatus.OK, {"run_id": run_id, "events": events})
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/timeline"):
            if not self._require_admin():
                return
            run_id = parsed.path.split("/")[3]
            try:
                timeline = self.server.service.run_timeline(run_id)
            except FileNotFoundError:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            _json_response(self, HTTPStatus.OK, timeline)
            return
        if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/artifacts"):
            if not self._require_admin():
                return
            run_id = parsed.path.split("/")[3]
            try:
                artifacts = self.server.service.run_artifacts(run_id)
            except FileNotFoundError:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            _json_response(self, HTTPStatus.OK, artifacts)
            return
        for artifact_name in ("transcript", "diff", "test-log", "test_log"):
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith(f"/{artifact_name}"):
                if not self._require_admin():
                    return
                run_id = parsed.path.split("/")[3]
                try:
                    content = self.server.service.read_run_artifact(run_id, artifact_name)
                except FileNotFoundError:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": "artifact_not_found"})
                    return
                except KeyError as exc:
                    _json_response(self, HTTPStatus.NOT_FOUND, {"error": str(exc)})
                    return
                _text_response(self, HTTPStatus.OK, content)
                return
        if parsed.path.startswith("/api/runs/"):
            if not self._require_admin():
                return
            run_id = parsed.path.rsplit("/", 1)[-1]
            try:
                run = self.server.store.get_run(run_id)
            except FileNotFoundError:
                _json_response(self, HTTPStatus.NOT_FOUND, {"error": "run_not_found"})
                return
            _json_response(self, HTTPStatus.OK, run.to_json())
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        body: dict[str, Any] = {}
        issue_key = ""
        try:
            body = _read_json_body(self)
            if parsed.path == "/api/webhooks/runs":
                if not self._require_admin():
                    return
                issue_key = _issue_key_from_webhook_payload(body)
                event_type = str(body.get("webhookEvent") or body.get("webhook_event") or body.get("event_type") or "unknown").strip()
                _server_log(
                    f"[webhook] received path={parsed.path} event={event_type or 'unknown'} issue={issue_key or '-'}"
                )
                if issue_key:
                    self.server.service.record_webhook_event(issue_key, body)
                provided_secret = _webhook_secret(self)
                provided_signature = _webhook_signature(self)
                webhook_timestamp = _webhook_timestamp(self)
                if body.get("issue") or body.get("issue_key"):
                    run = handle_jira_webhook(
                        body,
                        self.server.service.config,
                        store=self.server.store,
                        launcher=self.server.service.launcher,
                        launch_worker=self.server.worker_launch_mode != "planned",
                        coordinator_url=self.server.coordinator_url,
                        provided_secret=provided_secret,
                        provided_signature=provided_signature,
                        webhook_timestamp=webhook_timestamp,
                        webhook_secret=self.server.webhook_secret,
                    )
                else:
                    run = create_run_from_webhook(
                        body,
                        store=self.server.store,
                        launcher=self.server.service.launcher,
                        launch_worker=self.server.worker_launch_mode != "planned",
                        coordinator_url=self.server.coordinator_url,
                        provided_secret=provided_secret,
                        provided_signature=provided_signature,
                        webhook_timestamp=webhook_timestamp,
                        webhook_secret=self.server.webhook_secret,
                    )
                payload = run.to_json() if hasattr(run, "to_json") else dict(run)
                if issue_key:
                    self.server.service.record_webhook_event(
                        issue_key,
                        body,
                        processing_status="run_created",
                        run_id=str(payload.get("run_id") or ""),
                    )
                _server_log(
                    "[webhook] run created "
                    f"issue={payload.get('issue_key') or issue_key or '-'} "
                    f"run_id={payload.get('run_id') or '-'} "
                    f"status={payload.get('status') or '-'}"
                )
                _json_response(self, HTTPStatus.CREATED, payload)
                return
            if parsed.path == "/api/workers/register":
                if not self._require_worker():
                    return
                worker_id = str(body.get("worker_id") or "").strip()
                if not worker_id:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "worker_id_required"})
                    return
                registered = self.server.service.register_worker(
                    worker_id,
                    hostname=str(body.get("hostname") or "") or None,
                    capabilities=dict(body.get("capabilities") or {}),
                    version=str(body.get("version") or "") or None,
                )
                _json_response(self, HTTPStatus.OK, registered)
                return
            if parsed.path == "/api/workers/claim":
                if not self._require_worker():
                    return
                worker_id = str(body.get("worker_id") or "").strip()
                if not worker_id:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "worker_id_required"})
                    return
                run = self.server.service.claim_next_run(worker_id)
                if run is None:
                    _json_response(self, HTTPStatus.OK, {"run": None})
                    return
                _json_response(self, HTTPStatus.OK, {"run": run.to_json()})
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/pause"):
                if not self._require_admin():
                    return
                run_id = parsed.path.split("/")[3]
                run = self.server.service.pause_run(run_id, reason=str(body.get("reason") or "") or None)
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/resume"):
                if not self._require_admin():
                    return
                run_id = parsed.path.split("/")[3]
                run = self.server.service.resume_run(run_id)
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/cancel"):
                if not self._require_admin():
                    return
                run_id = parsed.path.split("/")[3]
                run = self.server.service.cancel_run(run_id, reason=str(body.get("reason") or "") or None)
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/retry"):
                if not self._require_admin():
                    return
                run_id = parsed.path.split("/")[3]
                run = self.server.service.retry_run(run_id, force=bool(body.get("force", False)))
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/priority"):
                if not self._require_admin():
                    return
                run_id = parsed.path.split("/")[3]
                if "priority" not in body:
                    _json_response(self, HTTPStatus.BAD_REQUEST, {"error": "priority_required"})
                    return
                run = self.server.service.update_run_priority(
                    run_id,
                    priority=int(body.get("priority") or 0),
                    queue_name=str(body.get("queue_name")) if body.get("queue_name") is not None else None,
                )
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/events"):
                run_id = parsed.path.split("/")[3]
                token = str(body.get("callback_token") or _bearer_token(self) or "").strip()
                if not token:
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "callback_token_required"})
                    return
                run = self.server.service.post_event(
                    run_id,
                    token,
                    kind=str(body.get("kind") or "info"),
                    stage=str(body.get("stage")) if body.get("stage") is not None else None,
                    message=str(body.get("message") or "worker callback"),
                    details=_event_details(body),
                )
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/complete"):
                run_id = parsed.path.split("/")[3]
                token = str(body.get("callback_token") or _bearer_token(self) or "").strip()
                if not token:
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "callback_token_required"})
                    return
                run = self.server.service.complete_run(run_id, token, body)
                _json_response(self, HTTPStatus.OK, run.to_json())
                return
            if parsed.path.startswith("/api/runs/") and parsed.path.endswith("/artifacts"):
                run_id = parsed.path.split("/")[3]
                token = str(body.get("callback_token") or _bearer_token(self) or "").strip()
                if not token:
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "callback_token_required"})
                    return
                run = self.server.store.get_run(run_id)
                if run.callback_token != token:
                    _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "worker_auth_required"})
                    return
                artifacts = self.server.service.store_run_artifacts(
                    run_id,
                    transcript=body.get("transcript") if isinstance(body.get("transcript"), str) else None,
                    diff=body.get("diff") if isinstance(body.get("diff"), str) else None,
                    test_log=body.get("test_log") if isinstance(body.get("test_log"), str) else None,
                    summary=body.get("summary") if isinstance(body.get("summary"), dict) else None,
                    metadata={key: value for key, value in body.items() if key not in {"callback_token", "transcript", "diff", "test_log", "summary"}},
                )
                _json_response(self, HTTPStatus.OK, {"run_id": run_id, "artifacts": artifacts})
                return
        except ValueError as exc:
            if parsed.path == "/api/webhooks/runs" and issue_key:
                self.server.service.record_webhook_event(
                    issue_key,
                    body,
                    processing_status="rejected",
                    error=str(exc),
                )
            _server_log(f"[webhook] bad request path={parsed.path} error={exc}")
            _json_response(self, HTTPStatus.BAD_REQUEST, {"error": str(exc)})
            return
        except Exception as exc:
            if parsed.path == "/api/webhooks/runs" and issue_key:
                self.server.service.record_webhook_event(
                    issue_key,
                    body,
                    processing_status="error",
                    error=str(exc),
                )
            _server_log(f"[webhook] internal error path={parsed.path} error={exc}")
            _json_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "internal_error", "message": str(exc)})
            return
        _json_response(self, HTTPStatus.NOT_FOUND, {"error": "not_found"})

    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _require_admin(self) -> bool:
        if not self.server.admin_token:
            return True
        token = _bearer_token(self)
        if token == self.server.admin_token:
            return True
        _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "admin_auth_required"})
        return False

    def _require_worker(self) -> bool:
        if not self.server.worker_token:
            return True
        token = _bearer_token(self)
        if token == self.server.worker_token:
            return True
        _json_response(self, HTTPStatus.UNAUTHORIZED, {"error": "worker_auth_required"})
        return False


def serve_coordinator(
    *,
    host: str,
    port: int,
    store_root: str | None = None,
    artifact_root: str | None = None,
    artifact_backend: str | None = None,
    admin_token: str | None = None,
    worker_token: str | None = None,
    webhook_secret: str | None = None,
    worker_launch_mode: str | None = None,
    coordinator_url: str | None = None,
) -> None:
    server = CoordinatorHttpServer(
        (host, port),
        store_root=store_root,
        artifact_root=artifact_root,
        artifact_backend=artifact_backend,
        admin_token=admin_token,
        worker_token=worker_token,
        webhook_secret=webhook_secret,
        worker_launch_mode=worker_launch_mode,
        coordinator_url=coordinator_url,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
