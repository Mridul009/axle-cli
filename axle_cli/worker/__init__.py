from __future__ import annotations

import argparse
import socket
import sys
import time
from uuid import uuid4

from ..coordinator.store_factory import open_run_store
from .runtime import (
    claim_remote_run,
    execute_remote_run,
    execute_run,
    fetch_run_payload,
    post_callback,
    register_remote_worker,
)


def load_worker_payload(*args, **kwargs):
    return fetch_run_payload(*args, **kwargs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="axle-worker", description="Axle worker entrypoint.")
    subparsers = parser.add_subparsers(dest="worker_command")

    run = subparsers.add_parser("run", help="Execute a stored automation run.")
    run.add_argument("--run-id", required=True, help="Coordinator run id.")
    run.add_argument("--callback-token", help="Run callback token.")
    run.add_argument("--store-root", help="Local coordinator store root for demo mode.")

    poll = subparsers.add_parser("poll", help="Register this worker and execute queued remote runs.")
    poll.add_argument("--coordinator-url", required=True, help="Coordinator base URL.")
    poll.add_argument("--worker-token", help="Shared worker registration token.")
    poll.add_argument("--worker-id", default=f"{socket.gethostname()}-{uuid4().hex[:8]}", help="Stable worker identifier.")
    poll.add_argument("--poll-interval", type=float, default=5.0, help="Seconds between queue polls.")
    poll.add_argument("--once", action="store_true", help="Claim at most one run and exit.")
    return parser


def _run_local(args):
    store = open_run_store(root=args.store_root)
    payload = load_worker_payload(args.run_id, store=store, callback_token=args.callback_token)
    result = execute_run(payload, store=store)
    post_callback(
        args.run_id,
        {"status": getattr(result, "status", "completed"), "result": "ok" if getattr(result, "status", "") == "completed" else "failed"},
        store=store,
        callback_token=args.callback_token or (payload.get("callback_token") if isinstance(payload, dict) else payload.callback_token),
    )
    return result


def _poll_remote(args):
    register_remote_worker(args.coordinator_url, args.worker_token, args.worker_id)
    last_summary = None
    while True:
        run = claim_remote_run(args.coordinator_url, args.worker_token, args.worker_id)
        if run is not None:
            last_summary = execute_remote_run(run, args.coordinator_url)
            if args.once:
                return last_summary
            continue
        if args.once:
            return {"status": "idle", "worker_id": args.worker_id}
        time.sleep(max(1.0, args.poll_interval))


def main(argv: list[str] | None = None):
    raw_argv = list(argv) if argv is not None else list(sys.argv[1:])
    if raw_argv and raw_argv[0].startswith("--"):
        raw_argv = ["run", *raw_argv]
    args = build_parser().parse_args(raw_argv)
    if getattr(args, "worker_command", None) in {None, "run"}:
        if getattr(args, "worker_command", None) is None:
            raise SystemExit("Pass a worker subcommand such as `run` or `poll`.")
        return _run_local(args)
    if args.worker_command == "poll":
        return _poll_remote(args)
    raise SystemExit(f"Unknown worker command: {args.worker_command}")


__all__ = [
    "build_parser",
    "claim_remote_run",
    "execute_remote_run",
    "execute_run",
    "fetch_run_payload",
    "load_worker_payload",
    "main",
    "post_callback",
    "register_remote_worker",
]
