from __future__ import annotations

from functools import lru_cache
from pathlib import Path


_PROMPT_DIR = Path(__file__).resolve().parent / "recipes"


@lru_cache(maxsize=1)
def goose_system_prompt() -> str:
    path = _PROMPT_DIR / "axle_system_prompt.txt"
    return path.read_text(encoding="utf-8").strip()


def goose_task_prompt(task: str, repo_tree: str, test_command: str | None) -> str:
    validation = test_command or "not configured"
    return "\n".join(
        [
            "Task:",
            task,
            "",
            "Validation command:",
            validation,
            "",
            "Repository tree excerpt:",
            repo_tree,
        ]
    )


def smoke_task_prompt(task: str, repo_tree: str) -> str:
    return "\n".join(
        [
            "Smoke task:",
            task,
            "",
            "Repository tree excerpt:",
            repo_tree,
            "",
            "Rules:",
            "- Inspect first and keep the response short.",
            "- Do not modify repository files during a smoke run.",
            "- Use tools directly instead of printing tool payload JSON or shell payloads.",
        ]
    )


def repair_prompt(task: str, test_command: str, failure_excerpt: str, changed_files: list[str]) -> str:
    changed = "\n".join(f"- {path}" for path in changed_files) or "- none"
    return "\n".join(
        [
            "The previous coding attempt completed but validation failed.",
            "Inspect the current repository state, make the minimum additional changes required, and keep updates short.",
            "Do not reply with only a plan, JSON, or an analysis summary.",
            "Make concrete source-file edits now. Write them to disk in the working tree instead of describing them.",
            "If you are blocked by a repository limitation, explain the blocker precisely and briefly.",
            "",
            f"Original task: {task}",
            f"Validation command: {test_command}",
            "",
            "Currently changed files:",
            changed,
            "",
            "Failure excerpt:",
            failure_excerpt,
        ]
    )


def noop_repair_prompt(
    task: str,
    test_command: str | None,
    goose_summary: str,
    changed_files: list[str],
    *,
    transcript_excerpt: str = "",
    likely_files: list[str] | None = None,
    output_kind: str = "unknown",
) -> str:
    changed = "\n".join(f"- {path}" for path in changed_files) or "- none"
    likely = "\n".join(f"- {path}" for path in (likely_files or [])) or "- none"
    validation = test_command or "not configured"
    excerpt = transcript_excerpt.strip() or "(no useful transcript excerpt available)"
    return "\n".join(
        [
            "The previous coding attempt described a solution but did not modify any source files.",
            "Apply the missing edits directly in the working tree now. Do not only restate the plan.",
            "Write the edits to disk before responding.",
            "If the task cannot be completed, explain the blocking reason briefly instead of re-summarizing the ticket.",
            "",
            f"Transcript classification: {output_kind}",
            "",
            f"Original task: {task}",
            f"Validation command: {validation}",
            "",
            "Likely target files:",
            likely,
            "",
            "Currently changed files:",
            changed,
            "",
            "Transcript excerpt:",
            excerpt,
            "",
            "Goose summary:",
            goose_summary,
        ]
    )
