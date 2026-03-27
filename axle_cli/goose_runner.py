from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .bootstrap import workspace_command_env
from .models import GooseRunResult, SavedConfig, Workspace
from .repo import diff_text, goose_session_name_for_workspace, source_changed_files
from .terminal import Terminal


class GooseError(RuntimeError):
    pass


DEFAULT_IMPLEMENT_RECIPE_PATH = Path(__file__).resolve().parent / "recipes" / "axle_run.yaml"
DEFAULT_SMOKE_RECIPE_PATH = Path(__file__).resolve().parent / "recipes" / "axle_smoke.yaml"


@dataclass(frozen=True)
class GooseInvocation:
    command: list[str]
    env: dict[str, str]
    session_name: str | None
    output_format: str
    recipe: str | None = None


@dataclass
class GooseEventSummary:
    rendered_lines: list[str]
    status_events: list[str]
    tool_events: list[str]
    error_events: list[str]
    files: list[str]


_FILE_PATH_RE = re.compile(r"(?<![\w./-])(?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]{1,8}(?![\w./-])")
_FENCED_PATCH_RE = re.compile(r"```(?:diff|patch)?\s*\n(.*?)\n```", re.DOTALL)
_PLAN_HINT_RE = re.compile(r"\b(plan|steps?|analysis|summary|inspect|suggested)\b", re.IGNORECASE)
_CODE_HINT_RE = re.compile(r"^```|```", re.MULTILINE)
_STEP_HINT_RE = re.compile(r"\bStep\s+\d+\b", re.IGNORECASE)
_TARGET_HINT_RE = re.compile(r"\b(update|modify|edit|change|apply)\s+`?([A-Za-z0-9_./-]+\.[A-Za-z0-9]{1,8})`?", re.IGNORECASE)
_STEP_FILE_RE = re.compile(r"(?:Step\s+\d+:\s+)?(?:Update|Modify|Edit|Change|Add(?: tests)?(?: in)?)\s+`([^`]+)`", re.IGNORECASE)
_TOOL_PAYLOAD_HINT_RE = re.compile(
    r"\b(function_name|function_arg|arguments|command|shell|tree|text_editor|developer|name)\b",
    re.IGNORECASE,
)


def _classify_transcript_output(transcript_lines: list[str], changed_files: list[str]) -> str:
    text = "\n".join(transcript_lines)
    if not text.strip():
        return "empty"
    if changed_files:
        return "source-edits"
    if _TOOL_PAYLOAD_HINT_RE.search(text):
        return "tool-payload"
    if _CODE_HINT_RE.search(text):
        return "snippet-heavy"
    if _STEP_HINT_RE.search(text) or _PLAN_HINT_RE.search(text):
        return "plan-only"
    if len(transcript_lines) <= 3:
        return "brief"
    return "general"


def _extract_likely_files(transcript_lines: list[str], repo_dir: Path) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    for line in transcript_lines:
        for match in _TARGET_HINT_RE.finditer(line):
            path = match.group(2).strip().strip(".,)")
            if path and path not in seen:
                candidates.append(path)
                seen.add(path)
        for match in _FILE_PATH_RE.finditer(line):
            path = match.group(0).strip().strip(".,)")
            if path and path not in seen:
                candidates.append(path)
                seen.add(path)
    if candidates:
        return candidates[:8]
    return []


def _transcript_excerpt(transcript_lines: list[str], likely_files: list[str], limit: int = 16) -> str:
    excerpt: list[str] = []
    seen: set[str] = set()
    target_set = set(likely_files)
    for line in transcript_lines:
        stripped = line.strip()
        if not stripped or stripped in seen:
            continue
        if (
            stripped.startswith("```")
            or _PLAN_HINT_RE.search(stripped)
            or _STEP_HINT_RE.search(stripped)
            or _TARGET_HINT_RE.search(stripped)
            or any(token in stripped for token in target_set)
        ):
            excerpt.append(stripped)
            seen.add(stripped)
        if len(excerpt) >= limit:
            break
    if not excerpt:
        for line in transcript_lines:
            stripped = line.strip()
            if stripped and stripped not in seen:
                excerpt.append(stripped)
                seen.add(stripped)
            if len(excerpt) >= min(limit, 8):
                break
    return "\n".join(excerpt).strip()


def _extract_file_write_candidates(transcript_lines: list[str]) -> dict[str, str]:
    candidates: dict[str, str] = {}
    current_file: str | None = None
    index = 0
    while index < len(transcript_lines):
        raw_line = transcript_lines[index]
        line = raw_line.strip()
        file_match = _STEP_FILE_RE.search(line)
        if file_match:
            current_file = file_match.group(1).strip()

        if line.startswith("```"):
            block_lines: list[str] = []
            index += 1
            while index < len(transcript_lines):
                block_line = transcript_lines[index]
                if block_line.strip().startswith("```"):
                    break
                block_lines.append(block_line)
                index += 1
            block = "\n".join(block_lines).rstrip()
            if current_file and block and current_file not in candidates:
                candidates[current_file] = block + "\n"
        index += 1
    return candidates


def _extract_recoverable_patch(transcript_text: str) -> str | None:
    for match in _FENCED_PATCH_RE.finditer(transcript_text):
        patch = match.group(1).rstrip()
        if patch and ("diff --git " in patch or "--- a/" in patch or "+++ b/" in patch or "@@ " in patch):
            return patch + "\n"
    return None


def _write_candidate_files(repo_dir: Path, file_write_candidates: dict[str, str]) -> bool:
    applied = False
    repo_root = repo_dir.resolve()
    for relative_path, content in file_write_candidates.items():
        if "diff --git " in content or "--- a/" in content or "+++ b/" in content or "@@ " in content:
            if _apply_recoverable_patch(repo_dir, content):
                applied = True
            continue
        target = (repo_dir / relative_path).resolve()
        if target == repo_root or repo_root not in target.parents:
            continue
        if target.exists():
            target.write_text(content, encoding="utf-8")
            applied = True
    return applied


def _apply_recoverable_patch(repo_dir: Path, patch_text: str) -> bool:
    git_bin = shutil.which("git")
    if git_bin is None:
        return False
    result = subprocess.run(
        [git_bin, "apply", "--whitespace=nowarn", "-"],
        cwd=repo_dir,
        input=patch_text,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return result.returncode == 0


def _provider_api_env(config: SavedConfig, provider: str | None) -> dict[str, str]:
    selected_provider = (provider or config.llm_provider or "openai").strip().lower()
    api_key = (config.llm_api_key or "").strip()
    if not api_key:
        return {}

    env: dict[str, str] = {}
    if selected_provider in {"openai", "openrouter"}:
        env["OPENAI_API_KEY"] = api_key
    if selected_provider == "openrouter":
        env["OPENROUTER_API_KEY"] = api_key
    if selected_provider == "anthropic":
        env["ANTHROPIC_API_KEY"] = api_key
    if selected_provider in {"azure", "azure_openai"}:
        env["AZURE_OPENAI_API_KEY"] = api_key
    return env


def _provider_base_url_env(config: SavedConfig, provider: str | None) -> dict[str, str]:
    base_url = (config.llm_base_url or "").strip()
    if not base_url:
        return {}

    selected_provider = (provider or config.llm_provider or "openai").strip().lower()
    env: dict[str, str] = {"OPENAI_BASE_URL": base_url}
    if selected_provider == "openrouter":
        env["OPENROUTER_BASE_URL"] = base_url
    return env


def _goose_env(config: SavedConfig, workspace: Workspace, provider: str | None = None) -> dict[str, str]:
    goose_home = workspace.root / ".goose"
    goose_home.mkdir(parents=True, exist_ok=True)

    env = workspace_command_env(workspace.repo_dir)
    env.update(
        {
            "HOME": str(goose_home),
            "XDG_CONFIG_HOME": str(goose_home / "config"),
            "XDG_CACHE_HOME": str(goose_home / "cache"),
            "XDG_STATE_HOME": str(goose_home / "state"),
        }
    )
    env.update(_provider_api_env(config, provider))
    env.update(_provider_base_url_env(config, provider))
    return env


def default_recipe_path(recipe_purpose: str = "implement") -> Path:
    return DEFAULT_SMOKE_RECIPE_PATH if recipe_purpose == "smoke" else DEFAULT_IMPLEMENT_RECIPE_PATH


def _resolved_recipe(config: SavedConfig, recipe_purpose: str = "implement") -> str | None:
    raw = (config.goose_recipe or "").strip()
    if not raw:
        return str(default_recipe_path(recipe_purpose))
    candidate = Path(raw).expanduser()
    if candidate.exists():
        return str(candidate.resolve())
    return raw


def _recipe_param_value(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\n", "\\n")


def build_command(
    config: SavedConfig,
    prompt_text: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    session_name: str | None = None,
    session_id: str | None = None,
    resume_session: bool = False,
    resume: bool | None = None,
    system_prompt: str | None = None,
    recipe: str | None = None,
    recipe_purpose: str = "implement",
    output_format: str = "stream-json",
) -> list[str]:
    goose_binary = (config.goose_binary or "").strip() or "goose"
    resolved = shutil.which(goose_binary)
    if resolved is None:
        raise GooseError(f"Goose binary `{goose_binary}` was not found on PATH.")

    selected_recipe = recipe or _resolved_recipe(config, recipe_purpose)
    command = [
        resolved,
        "run",
        "--no-profile",
        "--output-format",
        output_format,
    ]
    if selected_recipe:
        command.extend(["--recipe", selected_recipe, "--params", f"task_prompt={_recipe_param_value(prompt_text)}"])
    else:
        command.extend(["--text", prompt_text])
        if system_prompt:
            command.extend(["--system", system_prompt])

    should_resume = resume if resume is not None else resume_session
    if session_id:
        command.extend(["--session-id", session_id])
        if should_resume:
            command.append("--resume")
    elif session_name:
        command.extend(["--name", session_name])
        if should_resume:
            command.append("--resume")
    else:
        command.append("--no-session")

    builtins = config.effective_goose_builtins()
    if builtins:
        command.extend(["--with-builtin", builtins])

    selected_provider = (provider or config.llm_provider or "").strip()
    if selected_provider:
        command.extend(["--provider", selected_provider])

    selected_model = (model or config.llm_model or "").strip()
    if selected_model:
        command.extend(["--model", selected_model])

    return command


def start_run(
    config: SavedConfig,
    workspace: Workspace,
    prompt_text: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    system_prompt: str | None = None,
    recipe: str | None = None,
    recipe_purpose: str = "implement",
) -> GooseInvocation:
    session_name = workspace.goose_session_name or goose_session_name_for_workspace(workspace.workspace_key)
    command = build_command(
        config,
        prompt_text,
        model=model,
        provider=provider,
        session_name=session_name,
        resume_session=False,
        system_prompt=system_prompt,
        recipe=recipe,
        recipe_purpose=recipe_purpose,
    )
    return GooseInvocation(
        command=command,
        env=_goose_env(config, workspace, provider=provider),
        session_name=session_name,
        output_format="stream-json",
        recipe=recipe or _resolved_recipe(config, recipe_purpose),
    )


def resume_run(
    config: SavedConfig,
    workspace: Workspace,
    prompt_text: str,
    *,
    model: str | None = None,
    provider: str | None = None,
    system_prompt: str | None = None,
    recipe: str | None = None,
    recipe_purpose: str = "implement",
) -> GooseInvocation:
    session_name = workspace.goose_session_name or goose_session_name_for_workspace(workspace.workspace_key)
    command = build_command(
        config,
        prompt_text,
        model=model,
        provider=provider,
        session_name=session_name,
        resume_session=True,
        system_prompt=system_prompt,
        recipe=recipe,
        recipe_purpose=recipe_purpose,
    )
    return GooseInvocation(
        command=command,
        env=_goose_env(config, workspace, provider=provider),
        session_name=session_name,
        output_format="stream-json",
        recipe=recipe or _resolved_recipe(config, recipe_purpose),
    )


def _message_texts(payload: object) -> list[str]:
    if not isinstance(payload, dict):
        return []
    content = payload.get("content")
    if not isinstance(content, list):
        return []
    texts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text.strip():
            texts.append(text.strip())
    return texts


def _render_json_event(event: dict[str, object]) -> list[str]:
    rendered: list[str] = []
    if "messages" in event and isinstance(event["messages"], list):
        for message in event["messages"]:
            if isinstance(message, dict):
                rendered.extend(_message_texts(message))
        return rendered

    event_type = event.get("type")
    if event_type == "message":
        message = event.get("message")
        if isinstance(message, dict):
            rendered.extend(_message_texts(message))
        return rendered
    if event_type == "error" and isinstance(event.get("message"), str):
        rendered.append(str(event["message"]).strip())
        return rendered
    if event_type == "tool" and isinstance(event.get("tool"), str):
        rendered.append(f"tool:{event['tool']}".strip())
        return rendered
    if isinstance(event.get("summary"), str):
        rendered.append(str(event["summary"]).strip())
    if isinstance(event.get("status"), str):
        rendered.append(str(event["status"]).strip())
    if isinstance(event.get("kind"), str):
        rendered.append(str(event["kind"]).strip())
    return rendered


def _collect_event_summary(event: dict[str, object]) -> GooseEventSummary:
    rendered = _render_json_event(event)
    status_events: list[str] = []
    tool_events: list[str] = []
    error_events: list[str] = []
    files: list[str] = []

    event_type = event.get("type")
    if isinstance(event_type, str) and event_type.strip():
        status_events.append(event_type.strip())

    kind = event.get("kind")
    if isinstance(kind, str) and kind.strip():
        status_events.append(kind.strip())

    status = event.get("status")
    if isinstance(status, str) and status.strip():
        status_events.append(status.strip())

    tool = event.get("tool")
    if isinstance(tool, str) and tool.strip():
        tool_events.append(tool.strip())

    message = event.get("message")
    if event_type == "error" and isinstance(message, str) and message.strip():
        error_events.append(message.strip())

    if isinstance(event.get("files"), list):
        for item in event["files"]:
            if isinstance(item, str) and item.strip():
                files.append(item.strip())

    return GooseEventSummary(
        rendered_lines=rendered,
        status_events=status_events,
        tool_events=tool_events,
        error_events=error_events,
        files=files,
    )


def stream_events(process: subprocess.Popen[str], terminal: Terminal) -> tuple[list[str], list[str], bool, GooseEventSummary]:
    raw_lines: list[str] = []
    rendered_lines: list[str] = []
    structured_output = False
    summary = GooseEventSummary(rendered_lines=[], status_events=[], tool_events=[], error_events=[], files=[])

    assert process.stdout is not None
    for raw_line in process.stdout:
        line = raw_line.rstrip("\n")
        raw_lines.append(line)
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            rendered_lines.append(line)
            terminal.stream("goose", line)
            continue

        structured_output = True
        event_summary = _collect_event_summary(event)
        summary.rendered_lines.extend(event_summary.rendered_lines)
        summary.status_events.extend(event_summary.status_events)
        summary.tool_events.extend(event_summary.tool_events)
        summary.error_events.extend(event_summary.error_events)
        summary.files.extend(event_summary.files)
        for item in event_summary.rendered_lines:
            rendered_lines.append(item)
            terminal.stream("goose", item)

    summary.files = list(dict.fromkeys(summary.files))
    summary.status_events = list(dict.fromkeys(summary.status_events))
    summary.tool_events = list(dict.fromkeys(summary.tool_events))
    summary.error_events = list(dict.fromkeys(summary.error_events))
    return raw_lines, rendered_lines, structured_output, summary


def collect_result(
    workspace: Workspace,
    invocation: GooseInvocation,
    exit_code: int,
    raw_lines: list[str],
    rendered_lines: list[str],
    *,
    structured_output: bool,
    event_summary: GooseEventSummary,
) -> GooseRunResult:
    transcript = "\n".join([f"$ {' '.join(invocation.command)}", "", *raw_lines]).strip() + "\n"
    workspace.transcript_path.write_text(transcript, encoding="utf-8")

    patch_text = diff_text(workspace.repo_dir)
    workspace.diff_path.write_text(patch_text, encoding="utf-8")
    modified_files = source_changed_files(workspace.repo_dir)
    transcript_lines = [line for line in (rendered_lines or raw_lines) if line.strip()]
    normalized_lines = [line.strip() for line in transcript_lines]
    output_kind = _classify_transcript_output(normalized_lines, modified_files)
    likely_files = event_summary.files or _extract_likely_files(normalized_lines, workspace.repo_dir)
    excerpt = _transcript_excerpt(normalized_lines, likely_files)
    file_write_candidates = {} if structured_output else _extract_file_write_candidates(transcript_lines)
    patch_applied = False
    assistant_text = "\n".join(transcript_lines).strip()

    if output_kind in {"snippet-heavy", "brief", "general"} and _TOOL_PAYLOAD_HINT_RE.search(assistant_text):
        output_kind = "tool-payload"

    if not structured_output and not modified_files:
        recoverable_patch = _extract_recoverable_patch("\n".join(raw_lines))
        if recoverable_patch:
            patch_applied = _apply_recoverable_patch(workspace.repo_dir, recoverable_patch)
            if patch_applied:
                patch_text = diff_text(workspace.repo_dir)
                workspace.diff_path.write_text(patch_text, encoding="utf-8")
                modified_files = source_changed_files(workspace.repo_dir)
        if not modified_files and file_write_candidates:
            patch_applied = _write_candidate_files(workspace.repo_dir, file_write_candidates) or patch_applied
            if patch_applied:
                patch_text = diff_text(workspace.repo_dir)
                workspace.diff_path.write_text(patch_text, encoding="utf-8")
                modified_files = source_changed_files(workspace.repo_dir)

    if file_write_candidates and not modified_files and output_kind == "snippet-heavy":
        output_kind = "file-snippets"

    summary = "Goose run completed."
    if transcript_lines:
        summary = transcript_lines[-1]
    if structured_output:
        parsed_status = next((item for item in reversed(event_summary.status_events) if item and item != "analysis"), None)
        if parsed_status:
            output_kind = "structured-json"
            if parsed_status not in summary:
                summary = f"{parsed_status}: {summary}"
        elif event_summary.rendered_lines or event_summary.files:
            output_kind = "structured-json"
    if exit_code != 0:
        summary = f"Goose exited with status {exit_code}: {summary}"
    elif patch_applied:
        summary = "Goose transcript contained a recoverable patch and it was applied successfully."
    elif not modified_files and output_kind != "structured-json":
        if output_kind == "plan-only":
            summary = f"{summary} | Goose completed without source-file changes after plan-only output. Continue the session and apply the edits to disk."
        elif output_kind == "tool-payload":
            summary = f"{summary} | Goose printed tool payload text instead of executing a useful repository action. Continue the session and use tools directly."
        elif output_kind in {"snippet-heavy", "file-snippets"}:
            summary = f"{summary} | Goose completed without source-file changes after snippet-heavy output. Continue the session and apply the edits to disk."
        else:
            summary = f"{summary} | Goose completed without source-file changes. Continue the session and apply the edits to disk."

    return GooseRunResult(
        command=invocation.command,
        exit_code=exit_code,
        transcript_path=workspace.transcript_path,
        changed_files=modified_files,
        diff_text=patch_text,
        summary=summary,
        output_kind=output_kind,
        transcript_excerpt=excerpt,
        likely_files=likely_files,
        file_write_candidates=file_write_candidates,
        structured_output=structured_output,
        session_name=invocation.session_name,
        assistant_text=assistant_text,
        output_format=invocation.output_format,
        used_recipe=invocation.recipe,
        tool_events=event_summary.tool_events,
        error_events=event_summary.error_events,
        status_events=event_summary.status_events,
    )


def run_goose(
    config: SavedConfig,
    workspace: Workspace,
    prompt_text: str,
    terminal: Terminal,
    *,
    model: str | None = None,
    provider: str | None = None,
    resume_session: bool = False,
    system_prompt: str | None = None,
    recipe: str | None = None,
    recipe_purpose: str = "implement",
) -> GooseRunResult:
    workspace.prompt_path.write_text(prompt_text, encoding="utf-8")
    invocation = (
        resume_run(
            config,
            workspace,
            prompt_text,
            model=model,
            provider=provider,
            system_prompt=system_prompt,
            recipe=recipe,
            recipe_purpose=recipe_purpose,
        )
        if resume_session
        else start_run(
            config,
            workspace,
            prompt_text,
            model=model,
            provider=provider,
            system_prompt=system_prompt,
            recipe=recipe,
            recipe_purpose=recipe_purpose,
        )
    )

    display = " ".join(invocation.command[:2])
    if invocation.session_name:
        display += f" --name {invocation.session_name}"
        if resume_session:
            display += " --resume"
    else:
        display += " --no-session"
    display += f" --output-format {invocation.output_format}"
    terminal.command(display)

    process = subprocess.Popen(
        invocation.command,
        cwd=workspace.repo_dir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=invocation.env,
    )
    try:
        raw_lines, rendered_lines, structured_output, event_summary = stream_events(process, terminal)
        exit_code = process.wait()
    except OSError as exc:
        process.kill()
        raise GooseError(f"Failed to start Goose: {exc}") from exc
    finally:
        if process.stdout is not None:
            process.stdout.close()

    return collect_result(
        workspace,
        invocation,
        exit_code,
        raw_lines,
        rendered_lines,
        structured_output=structured_output,
        event_summary=event_summary,
    )
