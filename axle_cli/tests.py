from __future__ import annotations

import subprocess
from pathlib import Path

from .config import settings
from .models import TestResult
from .terminal import Terminal


def run_test_command(repo_dir: Path, command: str, log_path: Path, terminal: Terminal) -> TestResult:
    terminal.step("Testing", f"Running `{command}`")
    process = subprocess.Popen(
        ["sh", "-lc", command],
        cwd=repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    lines: list[str] = [f"$ {command}", ""]
    timed_out = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.rstrip("\n")
            lines.append(line)
            terminal.stream("test", line)
        exit_code = process.wait(timeout=settings.test_timeout_seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        process.kill()
        remaining = process.communicate()[0] or ""
        for line in remaining.splitlines():
            lines.append(line)
            terminal.stream("test", line)
        lines.append(f"[axle] Command timed out after {settings.test_timeout_seconds}s.")
        exit_code = 124
    output = "\n".join(lines).strip() + "\n"
    log_path.write_text(output, encoding="utf-8")
    return TestResult(command=command, exit_code=exit_code, output=output, timed_out=timed_out)
