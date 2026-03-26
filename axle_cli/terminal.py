from __future__ import annotations

from datetime import datetime
import sys

from .doctor import DoctorReport
from .models import RunSummary


def _stamp() -> str:
    return datetime.now().strftime("%H:%M:%S")


class Terminal:
    def __init__(self, verbose: bool = True) -> None:
        self.verbose = verbose

    def info(self, message: str) -> None:
        print(f"[{_stamp()}] {message}")

    def step(self, title: str, message: str) -> None:
        print(f"[{_stamp()}] {title}: {message}")

    def stream(self, prefix: str, line: str) -> None:
        if not self.verbose:
            return
        print(f"[{_stamp()}] {prefix} | {line}")

    def error(self, message: str) -> None:
        print(f"[{_stamp()}] ERROR: {message}", file=sys.stderr)

    def command(self, command: str) -> None:
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
