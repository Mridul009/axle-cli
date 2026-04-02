from __future__ import annotations

import os
from typing import Sequence

from .main import main as axle_main

DEMO_HOST_ENV = "AXLE_DEMO_HOST"
DEMO_PORT_ENV = "AXLE_DEMO_PORT"
DEMO_ADMIN_TOKEN_ENV = "AXLE_DEMO_ADMIN_TOKEN"
DEMO_WORKER_TOKEN_ENV = "AXLE_DEMO_WORKER_TOKEN"
DEMO_WEBHOOK_SECRET_ENV = "AXLE_DEMO_WEBHOOK_SECRET"
DEMO_COORDINATOR_URL_ENV = "AXLE_DEMO_COORDINATOR_URL"
DEMO_RUN_STORE_ENV = "AXLE_DEMO_RUN_STORE"


def _env(name: str, default: str) -> str:
    value = os.getenv(name, "").strip()
    return value or default


def _demo_host() -> str:
    return _env(DEMO_HOST_ENV, "127.0.0.1")


def _demo_port() -> str:
    return _env(DEMO_PORT_ENV, "8080")


def _demo_admin_token() -> str:
    return _env(DEMO_ADMIN_TOKEN_ENV, "demo-admin")


def _demo_worker_token() -> str:
    return _env(DEMO_WORKER_TOKEN_ENV, "demo-worker")


def _demo_webhook_secret() -> str:
    return _env(DEMO_WEBHOOK_SECRET_ENV, "demo-webhook")


def _demo_coordinator_url() -> str:
    return _env(DEMO_COORDINATOR_URL_ENV, f"http://{_demo_host()}:{_demo_port()}")


def _ensure_demo_store_mode() -> None:
    os.environ.setdefault("AXLE_RUN_STORE", _env(DEMO_RUN_STORE_ENV, "file"))


def coordinator_main(argv: Sequence[str] | None = None) -> int:
    _ensure_demo_store_mode()
    args = [
        "coordinator",
        "serve",
        "--host",
        _demo_host(),
        "--port",
        _demo_port(),
        "--admin-token",
        _demo_admin_token(),
        "--worker-token",
        _demo_worker_token(),
        "--webhook-secret",
        _demo_webhook_secret(),
        *(list(argv) if argv is not None else []),
    ]
    return axle_main(args)


def worker_main(argv: Sequence[str] | None = None) -> int:
    _ensure_demo_store_mode()
    args = [
        "worker",
        "poll",
        "--coordinator-url",
        _demo_coordinator_url(),
        "--worker-token",
        _demo_worker_token(),
        *(list(argv) if argv is not None else []),
    ]
    return axle_main(args)
