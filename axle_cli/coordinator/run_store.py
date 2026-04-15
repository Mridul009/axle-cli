from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from json import JSONDecodeError
from uuid import uuid4

from ..config import settings
from ..models import AutomationRun, RunEvent, WorkerLaunchSpec, utc_now
from .store_factory import configured_run_store_mode, configured_sqlite_path


def coordinator_root(root: Path | str | None = None) -> Path:
    base = Path(root).expanduser() if isinstance(root, str) else (root or (settings.cli_home / "coordinator"))
    return base.resolve()


class _FileRunStoreBackend:
    backend_kind = "file"

    def __init__(self, root: Path | str | None = None) -> None:
        self.root = coordinator_root(root)
        self.runs_dir = self.root / "runs"
        self.workers_dir = self.root / "workers"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.workers_dir.mkdir(parents=True, exist_ok=True)

    def _path_for(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _worker_path(self, worker_id: str) -> Path:
        sanitized = worker_id.replace("/", "_")
        return self.workers_dir / f"{sanitized}.json"

    def _write_json_atomic(self, path: Path, payload: dict[str, object]) -> None:
        temp_path = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)

    def _read_run_path(self, path: Path) -> AutomationRun:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return AutomationRun.from_json(payload)
        except (JSONDecodeError, TypeError, KeyError, ValueError) as exc:
            raise ValueError(f"Run file `{path}` is not a valid run record.") from exc

    def _coerce_run(self, run: AutomationRun | dict[str, object]) -> AutomationRun:
        if isinstance(run, AutomationRun):
            return run
        payload = dict(run)
        normalized = {
            "run_id": str(payload.get("run_id") or uuid4()),
            "issue_key": str(payload.get("issue_key") or payload.get("event_id") or payload.get("run_id") or "WEBHOOK"),
            "issue_url": str(payload.get("issue_url") or payload.get("callback_url") or ""),
            "repository": str(payload.get("repository") or ""),
            "base_branch": str(payload.get("base_branch") or "main"),
            "task": str(payload.get("task") or ""),
            "callback_url": payload.get("callback_url"),
            "status": str(payload.get("status") or "queued"),
            "result": payload.get("result"),
            "callback_token": str(payload.get("callback_token") or uuid4().hex),
        }
        return AutomationRun.from_json(normalized)

    def create_run(self, run: AutomationRun | dict[str, object]) -> AutomationRun:
        record = self._coerce_run(run)
        now = utc_now()
        if record.created_at is None:
            record.created_at = now
        if record.updated_at is None:
            record.updated_at = now
        self._write_json_atomic(self._path_for(record.run_id), record.to_json())
        return record

    save_run = create_run

    def get_run(self, run_id: str) -> AutomationRun:
        path = self._path_for(run_id)
        return self._read_run_path(path)

    load_run = get_run

    def list_runs(self) -> list[AutomationRun]:
        runs: list[AutomationRun] = []
        for path in sorted(self.runs_dir.glob("*.json")):
            try:
                runs.append(self._read_run_path(path))
            except (OSError, ValueError):
                continue
        return runs

    def latest_run_for_issue(self, issue_key: str) -> AutomationRun | None:
        normalized = issue_key.strip().upper()
        if not normalized:
            return None
        matches = [run for run in self.list_runs() if run.issue_key.strip().upper() == normalized]
        if not matches:
            return None
        matches.sort(key=lambda run: (run.created_at, run.updated_at or run.created_at, run.run_id), reverse=True)
        return matches[0]

    def append_event(self, run_id: str, event: RunEvent) -> AutomationRun:
        run = self.get_run(run_id)
        run.events.append(event)
        run.updated_at = utc_now()
        return self.create_run(run)

    def update_run(self, run_id: str, **changes: object) -> AutomationRun:
        run = self.get_run(run_id)
        for key, value in changes.items():
            if hasattr(run, key):
                setattr(run, key, value)
        run.updated_at = utc_now()
        return self.create_run(run)

    def attach_launch_spec(self, run_id: str, spec: WorkerLaunchSpec) -> AutomationRun:
        return self.update_run(run_id, worker_launch_spec=spec, worker_instance_id=spec.instance_id, status="launching")

    def register_worker(self, worker_id: str, *, hostname: str | None = None, capabilities: dict[str, object] | None = None, version: str | None = None) -> dict[str, object]:
        now = utc_now().isoformat()
        payload = {
            "worker_id": worker_id,
            "hostname": hostname,
            "capabilities": capabilities or {},
            "version": version,
            "status": "online",
            "last_seen_at": now,
            "created_at": now,
            "updated_at": now,
        }
        self._worker_path(worker_id).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload

    def claim_next_queued_run(self, worker_id: str) -> AutomationRun | None:
        candidates = sorted(
            self.runs_dir.glob("*.json"),
            key=lambda path: (path.stat().st_mtime, path.name),
        )
        for path in candidates:
            try:
                run = self._read_run_path(path)
            except (OSError, ValueError):
                continue
            if run.status != "queued":
                continue
            run.status = "running"
            run.worker_instance_id = worker_id
            run.updated_at = utc_now()
            run.events.append(RunEvent(kind="worker", stage="Coordinator", message=f"Assigned to worker `{worker_id}`."))
            return self.create_run(run)
        return None

    def list_workers(self) -> list[dict[str, object]]:
        if not self.workers_dir.exists():
            return []
        workers: list[dict[str, object]] = []
        for path in sorted(self.workers_dir.glob("*.json")):
            workers.append(dict(json.loads(path.read_text(encoding="utf-8"))))
        return workers


class _SqliteRunStoreBackend:
    backend_kind = "sqlite"

    def __init__(self, root: Path | str | None = None, db_path: Path | str | None = None) -> None:
        base_root = coordinator_root(root)
        base_root.mkdir(parents=True, exist_ok=True)
        self.root = base_root
        raw_db_path = configured_sqlite_path(base_root, db_path)
        self.db_path = raw_db_path.resolve()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    worker_id TEXT,
                    claimed_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS worker_registrations (
                    worker_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_seen_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_runs_status_created_at ON runs(status, created_at);
                """
            )

    def _coerce_run(self, run: AutomationRun | dict[str, object]) -> AutomationRun:
        return _FileRunStoreBackend(self.root)._coerce_run(run)

    def create_run(self, run: AutomationRun | dict[str, object]) -> AutomationRun:
        record = self._coerce_run(run)
        now = utc_now()
        if record.created_at is None:
            record.created_at = now
        if record.updated_at is None:
            record.updated_at = now
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO runs(run_id, payload_json, status, worker_id, claimed_at, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    worker_id=excluded.worker_id,
                    claimed_at=excluded.claimed_at,
                    updated_at=excluded.updated_at
                """,
                (
                    record.run_id,
                    json.dumps(record.to_json()),
                    record.status,
                    record.worker_instance_id,
                    None,
                    record.created_at.isoformat(),
                    record.updated_at.isoformat(),
                ),
            )
        return record

    save_run = create_run

    def get_run(self, run_id: str) -> AutomationRun:
        with self._connect() as connection:
            row = connection.execute("SELECT payload_json FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise FileNotFoundError(f"Run `{run_id}` was not found.")
        return AutomationRun.from_json(json.loads(row["payload_json"]))

    load_run = get_run

    def list_runs(self) -> list[AutomationRun]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM runs ORDER BY created_at DESC").fetchall()
        return [AutomationRun.from_json(json.loads(row["payload_json"])) for row in rows]

    def latest_run_for_issue(self, issue_key: str) -> AutomationRun | None:
        normalized = issue_key.strip().upper()
        if not normalized:
            return None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM runs ORDER BY created_at DESC"
            ).fetchall()
        for row in rows:
            run = AutomationRun.from_json(json.loads(row["payload_json"]))
            if run.issue_key.strip().upper() == normalized:
                return run
        return None

    def append_event(self, run_id: str, event: RunEvent) -> AutomationRun:
        run = self.get_run(run_id)
        run.events.append(event)
        return self.create_run(run)

    def update_run(self, run_id: str, **changes: object) -> AutomationRun:
        run = self.get_run(run_id)
        for key, value in changes.items():
            if hasattr(run, key):
                setattr(run, key, value)
        return self.create_run(run)

    def attach_launch_spec(self, run_id: str, spec: WorkerLaunchSpec) -> AutomationRun:
        return self.update_run(run_id, worker_launch_spec=spec, worker_instance_id=spec.instance_id, status="launching")

    def register_worker(self, worker_id: str, *, hostname: str | None = None, capabilities: dict[str, object] | None = None, version: str | None = None) -> dict[str, object]:
        now = utc_now().isoformat()
        payload = {
            "worker_id": worker_id,
            "hostname": hostname,
            "capabilities": capabilities or {},
            "version": version,
            "status": "online",
            "last_seen_at": now,
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO worker_registrations(worker_id, payload_json, status, last_seen_at, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(worker_id) DO UPDATE SET
                    payload_json=excluded.payload_json,
                    status=excluded.status,
                    last_seen_at=excluded.last_seen_at,
                    updated_at=excluded.updated_at
                """,
                (worker_id, json.dumps(payload), "online", now, now, now),
            )
        return payload

    def claim_next_queued_run(self, worker_id: str) -> AutomationRun | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id, payload_json FROM runs WHERE status = 'queued' ORDER BY created_at ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            run = AutomationRun.from_json(json.loads(row["payload_json"]))
            if run.status != "queued":
                return None
            run.status = "running"
            run.worker_instance_id = worker_id
            run.updated_at = utc_now()
            run.events.append(RunEvent(kind="worker", stage="Coordinator", message=f"Assigned to worker `{worker_id}`."))
            connection.execute(
                """
                UPDATE runs
                SET payload_json = ?, status = ?, worker_id = ?, claimed_at = ?, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    json.dumps(run.to_json()),
                    run.status,
                    worker_id,
                    utc_now().isoformat(),
                    run.updated_at.isoformat(),
                    run.run_id,
                ),
            )
        return run

    def list_workers(self) -> list[dict[str, object]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT payload_json FROM worker_registrations ORDER BY updated_at DESC").fetchall()
        return [dict(json.loads(row["payload_json"])) for row in rows]


class _DelegatingRunStore:
    def __init__(self, root: Path | str | None = None, *, mode: str | None = None, db_path: Path | str | None = None) -> None:
        self.root = coordinator_root(root)
        selected_mode = configured_run_store_mode(mode)
        if selected_mode == "sqlite":
            self._backend = _SqliteRunStoreBackend(root=self.root, db_path=db_path)
        else:
            self._backend = _FileRunStoreBackend(root=self.root)
        self.root = self._backend.root
        self.backend_kind = self._backend.backend_kind
        self.db_path = getattr(self._backend, "db_path", None)
        self.runs_dir = getattr(self._backend, "runs_dir", None)
        self.workers_dir = getattr(self._backend, "workers_dir", None)

    def __getattr__(self, name: str):
        return getattr(self._backend, name)

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | set(dir(self._backend)))


class FileBackedRunStore(_DelegatingRunStore):
    def __init__(self, root: Path | str | None = None, *, mode: str | None = "file", db_path: Path | str | None = None) -> None:
        super().__init__(root=root, mode=mode or "file", db_path=db_path)


class SqliteRunStore(FileBackedRunStore):
    def __init__(self, root: Path | str | None = None, *, mode: str | None = "sqlite", db_path: Path | str | None = None) -> None:
        super().__init__(root=root, mode=mode or "sqlite", db_path=db_path)
