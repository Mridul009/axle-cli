from __future__ import annotations

from datetime import datetime
import sys
from typing import Callable, Protocol

from .doctor import DoctorReport
from .models import RunSummary


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class ExecutionSink(Protocol):
    def info(self, message: str) -> None: ...
    def step(self, title: str, message: str) -> None: ...
    def stream(self, prefix: str, line: str) -> None: ...
    def error(self, message: str) -> None: ...
    def command(self, command: str) -> None: ...


class Terminal:
    def __init__(self, verbose: bool = True, event_handler: Callable[[dict[str, str]], None] | None = None) -> None:
        self.verbose = verbose
        self.event_handler = event_handler

    def _emit(self, kind: str, **payload: str) -> None:
        if self.event_handler:
            event = {"kind": kind, **payload}
            self.event_handler(event)

    def info(self, message: str) -> None:
        self._emit("info", message=message)
        print(f"[{_stamp()}] {message}")

    def step(self, title: str, message: str) -> None:
        self._emit("step", stage=title, message=message)
        print(f"[{_stamp()}] {title}: {message}")

    def stream(self, prefix: str, line: str) -> None:
        self._emit("stream", prefix=prefix, message=line)
        if not self.verbose:
            return
        print(f"[{_stamp()}] {prefix} | {line}")

    def error(self, message: str) -> None:
        self._emit("error", message=message)
        print(f"[{_stamp()}] ERROR: {message}", file=sys.stderr)

    def command(self, command: str) -> None:
        self._emit("command", command=command)
        print(f"[{_stamp()}] $ {command}")

    def summary(self, summary: RunSummary) -> None:
        status = summary.status.upper()
        print("")
        print(f"[{_stamp()}] Final outcome: {status}")
        if summary.failure:
            print(f"Failure: {summary.failure}")
        if summary.workspace:
            print(f"Workspace: {summary.workspace}")
        if summary.changed_files:
            print("Changed files:")
            for path in summary.changed_files:
                print(f"- {path}")
        else:
            print("Changed files: none")
        if summary.test_result:
            print(
                "Verification: "
                f"`{summary.test_result.command}` exited with {summary.test_result.exit_code}"
                + (" (timeout)" if summary.test_result.timed_out else "")
            )
        if summary.branch_name:
            print(f"Branch: {summary.branch_name}")
        if summary.pr_url:
            print(f"PR: {summary.pr_url}")
        if summary.diff_path:
            print(f"Diff: {summary.diff_path}")
        if summary.risks:
            print("Remaining risk:")
            for risk in summary.risks:
                print(f"- {risk}")

    def doctor(self, report: DoctorReport) -> None:
        print("")
        print(f"[{_stamp()}] Doctor report")
        for check in report.checks:
            prefix = {
                "ok": "OK",
                "warn": "WARN",
                "fail": "FAIL",
            }[check.level]
            print(f"{prefix}: {check.name} - {check.message}")
            for key, value in check.details.items():
                print(f"  {key}: {value}")


class BufferedExecutionSink:
    def __init__(self) -> None:
        self.events: list[dict[str, str]] = []

    def _append(self, kind: str, **payload: str) -> None:
        self.events.append({"kind": kind, **payload})

    def info(self, message: str) -> None:
        self._append("info", message=message)

    def step(self, title: str, message: str) -> None:
        self._append("step", stage=title, message=message)

    def stream(self, prefix: str, line: str) -> None:
        self._append("stream", prefix=prefix, message=line)

    def error(self, message: str) -> None:
        self._append("error", message=message)

    def command(self, command: str) -> None:
        self._append("command", command=command)
