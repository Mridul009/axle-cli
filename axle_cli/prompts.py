from __future__ import annotations


def goose_task_prompt(task: str, repo_tree: str, test_command: str | None) -> str:
    lines = [
        "You are Axle CLI running a terminal-first coding session.",
        "Follow this workflow strictly:",
        "1. Inspect the repository before editing.",
        "2. Explain what you are about to do in short updates.",
        "3. Edit files directly in the working tree.",
        "4. Keep changes focused on the task.",
        "5. Do not make destructive git history changes.",
        "6. Do not modify files outside the repository.",
    ]
    if test_command:
        lines.append(f"7. Aim to satisfy this validation command after edits: `{test_command}`.")
    lines.extend(
        [
            "",
            "Repository tree excerpt:",
            repo_tree,
            "",
            "Task:",
            task,
        ]
    )
    return "\n".join(lines)


def repair_prompt(task: str, test_command: str, failure_excerpt: str, changed_files: list[str]) -> str:
    changed = "\n".join(f"- {path}" for path in changed_files) or "- none"
    return "\n".join(
        [
            "The previous coding attempt completed but validation failed.",
            "Inspect the current repository state, make the minimum additional changes required, and keep updates short.",
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
