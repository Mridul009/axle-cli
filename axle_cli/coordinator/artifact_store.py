from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..config import settings
from ..models import RunSummary, TestResult, utc_now

ARTIFACT_STORE_BACKEND_ENV = "AXLE_ARTIFACT_STORE_BACKEND"
ARTIFACT_STORE_ROOT_ENV = "AXLE_ARTIFACT_STORE_ROOT"
DEFAULT_ARTIFACT_BACKEND = "file"
DEFAULT_ARTIFACT_ROOT = settings.cli_home / "coordinator" / "artifacts"


def _path_from_value(value: str | Path | None) -> Path | None:
    if value is None:
        return None
    if isinstance(value, Path):
        return value.expanduser().resolve()
    text = value.strip()
    return Path(text).expanduser().resolve() if text else None


def artifact_store_settings(*, backend: str | None = None, root: str | Path | None = None) -> tuple[str, Path]:
    selected_backend = (backend or os.getenv(ARTIFACT_STORE_BACKEND_ENV) or DEFAULT_ARTIFACT_BACKEND).strip().lower()
    selected_root = _path_from_value(root) or _path_from_value(os.getenv(ARTIFACT_STORE_ROOT_ENV)) or DEFAULT_ARTIFACT_ROOT.resolve()
    return selected_backend, selected_root


def _ensure_backend_supported(backend: str) -> None:
    if backend != "file":
        raise ValueError(f"Unsupported artifact store backend: {backend}. Only `file` is supported in this build.")


def _serialize_test_result(test_result: TestResult | dict[str, Any] | None) -> dict[str, Any] | None:
    if test_result is None:
        return None
    if isinstance(test_result, dict):
        return dict(test_result)
    return {
        "command": test_result.command,
        "exit_code": test_result.exit_code,
        "output": test_result.output,
        "timed_out": test_result.timed_out,
        "ok": test_result.ok,
    }


def _serialize_summary(summary: RunSummary | dict[str, Any]) -> dict[str, Any]:
    if isinstance(summary, dict):
        payload = dict(summary)
    else:
        payload = asdict(summary)
    started_at = payload.get("started_at")
    payload["started_at"] = started_at.isoformat() if hasattr(started_at, "isoformat") else started_at
    finished_at = payload.get("finished_at")
    payload["finished_at"] = finished_at.isoformat() if hasattr(finished_at, "isoformat") else finished_at
    payload["test_result"] = _serialize_test_result(payload.get("test_result"))
    return payload


class FileArtifactStore:
    def __init__(self, root: str | Path | None = None, *, backend: str | None = None) -> None:
        selected_backend, selected_root = artifact_store_settings(backend=backend, root=root)
        _ensure_backend_supported(selected_backend)
        self.backend = selected_backend
        self.root = selected_root
        self.root.mkdir(parents=True, exist_ok=True)

    def run_root(self, run_id: str) -> Path:
        path = self.root / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _write_text_source(self, destination: Path, source: str | Path | None, *, default_source: Path | None = None) -> bool:
        if source is None and default_source is None:
            return False
        destination.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, Path):
            if not source.exists():
                return False
            if source.is_file():
                shutil.copy2(source, destination)
            else:
                destination.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            return True
        if isinstance(source, str):
            destination.write_text(source, encoding="utf-8")
            return True
        if default_source is None or not default_source.exists():
            return False
        if default_source.is_file():
            shutil.copy2(default_source, destination)
        else:
            destination.write_text(default_source.read_text(encoding="utf-8"), encoding="utf-8")
        return True

    def _write_json(self, destination: Path, payload: dict[str, Any]) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def store_run_artifacts(
        self,
        run_id: str,
        *,
        transcript: str | Path | None = None,
        diff: str | Path | None = None,
        test_log: str | Path | None = None,
        summary: RunSummary | dict[str, Any] | None = None,
        source_dir: str | Path | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        run_root = self.run_root(run_id)
        source_root = _path_from_value(source_dir)
        manifest: dict[str, str] = {}
        source_defaults = {
            "transcript": source_root / "goose-transcript.log" if source_root else None,
            "diff": source_root / "changes.patch" if source_root else None,
            "test_log": source_root / "test.log" if source_root else None,
        }
        target_paths = {
            "transcript": run_root / "goose-transcript.log",
            "diff": run_root / "changes.patch",
            "test_log": run_root / "test.log",
        }
        for key, source_value in (("transcript", transcript), ("diff", diff), ("test_log", test_log)):
            if self._write_text_source(target_paths[key], source_value, default_source=source_defaults[key]):
                manifest[key] = str(target_paths[key])
        if summary is not None:
            summary_path = run_root / "summary.json"
            self._write_json(summary_path, _serialize_summary(summary))
            manifest["summary"] = str(summary_path)
        manifest_path = run_root / "manifest.json"
        manifest_payload = {
            "run_id": run_id,
            "backend": self.backend,
            "root": str(self.root),
            "created_at": utc_now().isoformat(),
            "artifacts": manifest,
            "metadata": metadata or {},
        }
        self._write_json(manifest_path, manifest_payload)
        manifest["manifest"] = str(manifest_path)
        return manifest

    def load_manifest(self, run_id: str) -> dict[str, Any]:
        manifest_path = self.run_root(run_id) / "manifest.json"
        return json.loads(manifest_path.read_text(encoding="utf-8"))

    def artifact_path(self, run_id: str, name: str) -> Path:
        filenames = {
            "transcript": "goose-transcript.log",
            "diff": "changes.patch",
            "test_log": "test.log",
            "summary": "summary.json",
            "manifest": "manifest.json",
        }
        if name not in filenames:
            raise KeyError(f"Unknown artifact name: {name}")
        return self.run_root(run_id) / filenames[name]


def build_artifact_store(*, backend: str | None = None, root: str | Path | None = None) -> FileArtifactStore:
    return FileArtifactStore(root=root, backend=backend)
