from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

RunStoreMode = Literal["file", "sqlite"]

RUN_STORE_ENV = "AXLE_RUN_STORE"
SQLITE_PATH_ENV = "AXLE_SQLITE_PATH"

_FILE_MODES = {"file", "filesystem", "demo", "local"}
_SQLITE_MODES = {"sqlite", "db", "database"}


def _normalize_mode(value: str | None) -> RunStoreMode | None:
    raw = (value or "").strip().lower()
    if not raw:
        return None
    if raw in _FILE_MODES:
        return "file"
    if raw in _SQLITE_MODES:
        return "sqlite"
    raise ValueError(f"Unknown run store mode `{value}`. Use `file` or `sqlite`.")


def configured_run_store_mode(explicit_mode: str | None = None) -> RunStoreMode:
    env_mode = _normalize_mode(explicit_mode) or _normalize_mode(os.getenv(RUN_STORE_ENV))
    if env_mode:
        return env_mode
    if os.getenv(SQLITE_PATH_ENV, "").strip():
        return "sqlite"
    return "file"


def configured_sqlite_path(root: Path, explicit_path: Path | str | None = None) -> Path:
    if explicit_path is not None:
        return Path(explicit_path).expanduser().resolve()
    env_sqlite_path = os.getenv(SQLITE_PATH_ENV, "").strip()
    if env_sqlite_path:
        return Path(env_sqlite_path).expanduser().resolve()
    return (root / "coordinator.sqlite3").resolve()


def open_run_store(*, root: Path | str | None = None, mode: str | None = None, sqlite_path: Path | str | None = None):
    from .run_store import FileBackedRunStore, SqliteRunStore, coordinator_root

    resolved_root = coordinator_root(root)
    selected_mode = configured_run_store_mode(mode)
    if selected_mode == "sqlite":
        return SqliteRunStore(root=resolved_root, mode="sqlite", db_path=configured_sqlite_path(resolved_root, sqlite_path))
    return FileBackedRunStore(root=resolved_root, mode="file")
