from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import requests

from .config import settings
from .doctor import run_doctor
from .github_auth import save_github_settings, test_github_token
from .jira import fetch_jira_issue, format_jira_issue, jira_issue_task_text, save_jira_settings
from .models import RunRequest
from .repo import RepoError, diff_text, prepare_workspace, repo_tree
from .state import load_config, save_config
from .terminal import Terminal


LOCAL_LLM_PROVIDERS = {"ollama"}
JIRA_KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9]+-\d+$")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="axle", description="Axle terminal-first coding agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Save integration credentials.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    auth_github = auth_sub.add_parser("github", help="Save/test GitHub settings.")
    auth_github.add_argument("--token", help="GitHub token.")
    auth_github.add_argument("--repo", help="Default repository URL.")
    auth_github.add_argument("--branch", default=argparse.SUPPRESS, help="Default base branch.")

    auth_jira = auth_sub.add_parser("jira", help="Save/test Jira settings.")
    auth_jira.add_argument("--base-url", help="Jira Cloud base URL, for example https://your-domain.atlassian.net.")
    auth_jira.add_argument("--email", help="Jira account email.")
    auth_jira.add_argument("--api-token", help="Jira API token.")
    auth_jira.add_argument("--project", help="Default Jira project key.")

    auth_llm = auth_sub.add_parser("llm", help="Save Goose provider settings.")
    auth_llm.add_argument("--api-key", help="Provider API key passed through to Goose when the provider requires one.")
    auth_llm.add_argument("--provider", default=settings.default_goose_provider, help="Goose provider name.")
    auth_llm.add_argument("--model", default=settings.default_goose_model, help="Default Goose model.")
    auth_llm.add_argument("--fallback-model", help="Optional fallback Goose model when the primary model returns no source edits.")
    auth_llm.add_argument("--base-url", help="Optional provider base URL.")

    init = subparsers.add_parser("init", help="Save default repository, path, branch, and test command settings.")
    init.add_argument("--repo", help="Default repository URL.")
    init.add_argument("--path", help="Default local project path.")
    init.add_argument("--branch", help="Default base branch.")
    init.add_argument("--test", help="Default validation command.")

    config = subparsers.add_parser("config", help="Update local CLI configuration.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_goose = config_sub.add_parser("set-goose", help="Set Goose executable defaults.")
    config_goose.add_argument("--binary", default=settings.default_goose_binary, help="Goose CLI binary name.")
    config_goose.add_argument("--builtin", default="", help="Optional comma-separated Goose builtin extensions.")
    config_goose.add_argument("--recipe", help="Optional Goose recipe name or path.")

    status = subparsers.add_parser("status", help="Print current local configuration.")
    status.add_argument("--show-secrets", action="store_true", help="Print saved tokens and keys.")

    doctor = subparsers.add_parser("doctor", help="Check local Goose and CLI readiness.")
    doctor.add_argument("--show-secrets", action="store_true", help="Print saved tokens and keys in the report.")

    smoke = subparsers.add_parser("smoke", help="Run a safe Goose smoke check against the configured provider/model.")
    smoke.add_argument("--repo", help="Repository URL or local path.")
    smoke.add_argument("--path", help="Local project path. Takes priority over --repo.")
    smoke.add_argument("--branch", help="Base branch.")
    smoke.add_argument("--provider", help="Override the configured Goose provider.")
    smoke.add_argument("--model", help="Override the configured Goose model.")
    smoke.add_argument("--setup", choices=("auto", "skip"), default="auto", help="Prepare the workspace before the smoke run.")
    smoke.add_argument("--prompt", help="Optional smoke prompt override.")
    smoke.add_argument("--no-reuse-workspace", action="store_true", help="Use a fresh workspace instead of the cached workspace for this source.")

    jira = subparsers.add_parser("jira", help="Read Jira issues using saved credentials.")
    jira_sub = jira.add_subparsers(dest="jira_command", required=True)
    jira_get = jira_sub.add_parser("get", help="Fetch a Jira issue and print its details.")
    jira_get.add_argument("issue_key", help="Jira issue key, for example APP-123.")

    run = subparsers.add_parser("run", help="Run a coding task.")
    run.add_argument("--task", help="Task prompt. If omitted, stdin is used.")
    run.add_argument("--jira", help="Jira issue key to fetch and use as the task.")
    run.add_argument("--repo", help="Repository URL or local path.")
    run.add_argument("--path", help="Local project path. Takes priority over --repo.")
    run.add_argument("--branch", help="Base branch.")
    run.add_argument("--setup", choices=("auto", "skip"), default="auto", help="Prepare the workspace environment before Goose runs.")
    run.add_argument("--test", help="Validation command.")
    run.add_argument("--pr", action="store_true", help="Commit, push, and open a PR after validation.")
    run.add_argument("--model", help="Override the configured Goose model.")
    run.add_argument("--provider", help="Override the configured Goose provider.")
    run.add_argument("--no-reuse-workspace", action="store_true", help="Use a fresh workspace instead of the cached workspace for this source.")
    run.add_argument("--repair-attempts", type=int, default=settings.max_repair_attempts, help="Bounded repair retries.")
    run.add_argument("--chat", action="store_true", help="Open an interactive follow-up shell after the run completes.")
    run.add_argument("--quiet", action="store_true", help="Reduce streamed command output.")

    return parser


def _looks_like_jira_issue_key(value: str) -> bool:
    return bool(JIRA_KEY_PATTERN.match(value.strip().upper()))


def _normalize_argv(argv: list[str]) -> list[str]:
    if not argv:
        return argv

    normalized = list(argv)
    command = normalized[0]
    if command == "run" and len(normalized) > 1:
        shorthand = normalized[1]
        if shorthand and not shorthand.startswith("-") and "--jira" not in normalized[1:] and "--task" not in normalized[1:]:
            if _looks_like_jira_issue_key(shorthand):
                return [command, "--jira", shorthand, *normalized[2:]]
            return [command, "--task", shorthand, *normalized[2:]]
    if command == "jira" and len(normalized) > 1:
        shorthand = normalized[1]
        if shorthand and not shorthand.startswith("-") and shorthand != "get":
            return [command, "get", shorthand, *normalized[2:]]
    return normalized


def _require_task(task: str | None) -> str:
    if task and task.strip():
        return task.strip()
    payload = sys.stdin.read().strip()
    if payload:
        return payload
    raise SystemExit("A task is required. Pass --task or pipe prompt text into the command.")


def _resolve_task(args: argparse.Namespace, config, terminal: Terminal) -> str:
    if getattr(args, "jira", None):
        terminal.step("Jira", f"Fetching issue `{args.jira}`.")
        issue = fetch_jira_issue(config, args.jira)
        for line in format_jira_issue(issue).splitlines():
            terminal.info(line)
        return jira_issue_task_text(issue, args.task)
    return _require_task(args.task)


def _print_diff(terminal: Terminal, summary, max_lines: int = 200) -> None:
    if not summary.workspace:
        terminal.info("No workspace is available yet.")
        return
    repo_dir = Path(summary.workspace) / "repo"
    try:
        content = diff_text(repo_dir).rstrip()
    except RepoError as exc:
        terminal.error(str(exc))
        return
    if not content:
        terminal.info("No repository diff is available.")
        return
    lines = content.splitlines()
    terminal.info("Current repository diff:")
    for line in lines[:max_lines]:
        terminal.info(line)
    if len(lines) > max_lines:
        terminal.info(f"... truncated to {max_lines} lines")


def _chat_help(terminal: Terminal) -> None:
    terminal.info("Interactive commands:")
    terminal.info("- plain text: add a follow-up instruction and rerun")
    terminal.info("- /status: print the latest run summary")
    terminal.info("- /diff: print the current repository diff")
    terminal.info("- /retry: rerun the current task without extra instructions")
    terminal.info("- /create_pr: commit, push, and open a PR from the current workspace")
    terminal.info("- /exit: leave interactive mode")


def _interactive_run_loop(config, terminal: Terminal, runner, base_request: RunRequest, summary) -> int:
    terminal.info("Interactive mode is active. Type `/help` for commands.")
    current_request = base_request
    current_summary = summary

    while True:
        try:
            raw = input("axle> ").strip()
        except EOFError:
            terminal.info("Exiting interactive mode.")
            return 0 if current_summary.status == "completed" else 1
        if not raw:
            continue
        if raw == "/help":
            _chat_help(terminal)
            continue
        if raw == "/status":
            terminal.summary(current_summary)
            continue
        if raw == "/diff":
            _print_diff(terminal, current_summary)
            continue
        if raw == "/retry":
            terminal.step("Chat", "Continuing the current Goose session without extra instructions.")
            if hasattr(runner, "resume"):
                current_summary = runner.resume(
                    current_request,
                    current_summary,
                    "Continue the current task in this same repository session. Inspect the repository state and make the next necessary edits now.",
                )
            else:
                current_summary = runner.run(current_request)
            config.last_workspace = current_summary.workspace
            save_config(config)
            terminal.summary(current_summary)
            continue
        if raw == "/create_pr":
            try:
                current_summary = runner.create_pr(current_request, current_summary)
                terminal.summary(current_summary)
            except RepoError as exc:
                terminal.error(str(exc))
            continue
        if raw == "/exit":
            return 0 if current_summary.status == "completed" else 1
        if raw.startswith("/"):
            terminal.error(f"Unknown interactive command: {raw}")
            continue

        terminal.step("Chat", "Applying a follow-up instruction in the existing workspace.")
        if hasattr(runner, "resume"):
            current_summary = runner.resume(current_request, current_summary, raw)
        else:
            current_request = RunRequest(
                task=raw,
                task_context=current_request.task_context or current_request.task,
                repository=current_request.repository,
                base_branch=current_request.base_branch,
                repository_path=current_request.repository_path,
                repository_kind=current_request.repository_kind,
                setup_mode=current_request.setup_mode,
                test_command=current_request.test_command,
                create_pr=False,
                model=current_request.model,
                provider=current_request.provider,
                max_repair_attempts=current_request.max_repair_attempts,
                workspace_key=current_request.workspace_key,
                reuse_workspace=True,
                resume_goose_session=True,
            )
            current_summary = runner.run(current_request)
        config.last_workspace = current_summary.workspace
        save_config(config)
        terminal.summary(current_summary)


def handle_auth_github(args: argparse.Namespace, terminal: Terminal) -> int:
    token = getattr(args, "token", None)
    repo = getattr(args, "repo", None)
    branch = getattr(args, "branch", None)
    if not any((token, repo, branch)):
        raise SystemExit("Provide at least one of --token, --repo, or --branch.")
    config = save_github_settings(token, repo, branch)
    terminal.info(f"GitHub settings saved. Repository: {config.repository or 'not configured'}.")
    if token:
        payload = test_github_token(token.strip())
        terminal.info(f"GitHub token is valid for user `{payload.get('login')}`.")
    return 0


def handle_auth_llm(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    provider = args.provider.strip()
    model = args.model.strip()
    api_key = args.api_key.strip() if args.api_key else None
    fallback_model = getattr(args, "fallback_model", None)
    if provider.lower() in LOCAL_LLM_PROVIDERS:
        config.llm_api_key = api_key or None
    else:
        if not api_key:
            raise ValueError(f"`--api-key` is required for provider `{provider}`.")
        config.llm_api_key = api_key
    config.llm_provider = provider
    config.llm_model = model
    config.llm_fallback_model = fallback_model.strip() if fallback_model else None
    config.llm_base_url = args.base_url.strip().rstrip("/") if args.base_url else None
    save_config(config)
    terminal.info(f"Goose provider settings saved for `{config.llm_provider}` and model `{config.llm_model}`.")
    return 0


def handle_auth_jira(args: argparse.Namespace, terminal: Terminal) -> int:
    if not any((args.base_url, args.email, args.api_token, args.project)):
        raise SystemExit("Provide Jira settings with --base-url, --email, --api-token, or --project.")
    config = save_jira_settings(args.base_url, args.email, args.api_token, args.project)
    terminal.info(f"Jira settings saved. Base URL: {config.jira_base_url or 'not configured'}.")
    if args.base_url or args.email or args.api_token:
        terminal.info(f"Jira token is valid for account `{config.jira_email or 'unknown'}`.")
    return 0


def handle_init(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    repo = (args.repo or "").strip()
    path = (args.path or "").strip()
    branch = (args.branch or "").strip()
    test_command = (args.test or "").strip()

    if not any((repo, path, branch, test_command)):
        raise SystemExit("Provide at least one of --repo, --path, --branch, or --test.")

    if path:
        resolved_path = Path(path).expanduser().resolve()
        if not resolved_path.exists() or not resolved_path.is_dir():
            raise ValueError(f"Local project path does not exist: {resolved_path}")
        config.repository = str(resolved_path)
    elif repo:
        config.repository = repo

    if branch:
        config.base_branch = branch
    if test_command:
        config.default_test_command = test_command

    save_config(config)
    terminal.info("Axle defaults saved.")
    terminal.info(f"- repository: {config.repository or 'not configured'}")
    terminal.info(f"- base branch: {config.base_branch}")
    terminal.info(f"- default test command: {config.default_test_command or 'not configured'}")
    return 0


def handle_config_goose(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    config.goose_binary = args.binary.strip()
    config.goose_builtin = args.builtin.strip() or None
    config.goose_recipe = args.recipe.strip() if getattr(args, "recipe", None) else None
    save_config(config)
    terminal.info(f"Goose settings saved. Binary: `{config.goose_binary}`.")
    return 0


def handle_status(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    terminal.info("Current CLI configuration:")
    terminal.info(f"- repository: {config.repository or 'not configured'}")
    terminal.info(f"- base branch: {config.base_branch}")
    terminal.info(f"- GitHub user: {config.github_login or 'unknown'}")
    terminal.info(f"- Goose binary: {config.goose_binary}")
    terminal.info(f"- Goose provider/model: {config.llm_provider} / {config.llm_model}")
    terminal.info(f"- Goose fallback model: {config.llm_fallback_model or 'not configured'}")
    terminal.info(f"- Goose recipe: {config.goose_recipe or 'built-in axle recipe'}")
    terminal.info(f"- Goose builtins: {config.effective_goose_builtins() or 'none'}")
    terminal.info(f"- default test command: {config.default_test_command or 'not configured'}")
    terminal.info(f"- Jira base URL: {config.jira_base_url or 'not configured'}")
    terminal.info(f"- Jira project: {config.jira_project_key or 'not configured'}")
    if args.show_secrets:
        terminal.info(f"- GitHub token: {config.github_token or 'missing'}")
        terminal.info(f"- Jira email: {config.jira_email or 'missing'}")
        terminal.info(f"- Jira API token: {config.jira_api_token or 'missing'}")
        terminal.info(f"- LLM API key: {config.llm_api_key or 'missing'}")
    return 0


def handle_doctor(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    if args.show_secrets:
        terminal.info("Doctor is running with secret visibility enabled.")
    report = run_doctor(config)
    terminal.doctor(report)
    return 0 if report.ok else 1


def handle_smoke(args: argparse.Namespace, terminal: Terminal) -> int:
    from .bootstrap import bootstrap_python_workspace
    from .goose_runner import run_goose
    from .prompts import smoke_task_prompt

    config = load_config()
    path = getattr(args, "path", None)
    repository = (path or args.repo or config.repository or "").strip()
    if not repository:
        raise SystemExit("No repository is configured. Use `axle init --repo ...` or pass `--repo` or `--path`.")

    request = RunRequest(
        task=(args.prompt or "Inspect the repository, report one concrete implementation area, and do not modify files."),
        task_context=args.prompt or "Goose smoke run",
        repository=repository,
        base_branch=(args.branch or config.base_branch or settings.default_base_branch).strip(),
        repository_path=path,
        repository_kind="local" if path else "auto",
        setup_mode=getattr(args, "setup", "auto"),
        model=args.model or config.llm_model,
        provider=args.provider or config.llm_provider,
        max_repair_attempts=0,
        reuse_workspace=not getattr(args, "no_reuse_workspace", False),
    )

    workspace = prepare_workspace(request, config.github_token)
    config.last_workspace = str(workspace.root)
    save_config(config)

    if request.setup_mode != "skip":
        terminal.step("Setup", "Preparing the project workspace and environment.")
        bootstrap_execution = bootstrap_python_workspace(workspace.repo_dir)
        if bootstrap_execution.executed_commands:
            terminal.step("Setup", "Workspace bootstrap completed.")
        else:
            terminal.step("Setup", "Reusing the prepared workspace environment.")

    tree = repo_tree(workspace.repo_dir, settings.repo_tree_limit)
    prompt_text = smoke_task_prompt(request.task, tree)
    result = run_goose(
        config,
        workspace,
        prompt_text,
        terminal,
        model=request.model,
        provider=request.provider,
        recipe_purpose="smoke",
    )

    terminal.info("Smoke result:")
    terminal.info(f"- session: {result.session_name or 'none'}")
    terminal.info(f"- provider/model: {request.provider} / {request.model}")
    terminal.info(f"- recipe: {result.used_recipe or 'none'}")
    terminal.info(f"- output format: {result.output_format}")
    terminal.info(f"- structured output: {'yes' if result.structured_output else 'no'}")
    terminal.info(f"- status events: {', '.join(result.status_events) or 'none'}")
    terminal.info(f"- tool events: {', '.join(result.tool_events) or 'none'}")
    terminal.info(f"- error events: {', '.join(result.error_events) or 'none'}")
    terminal.info(f"- summary: {result.summary}")
    terminal.info(f"- changed files: {', '.join(result.changed_files) or 'none'}")
    return 0 if result.exit_code == 0 else 1


def handle_jira_get(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    issue = fetch_jira_issue(config, args.issue_key)
    for line in format_jira_issue(issue).splitlines():
        terminal.info(line)
    return 0


def handle_run(args: argparse.Namespace, terminal: Terminal) -> int:
    from .session import SessionRunner

    config = load_config()
    path = getattr(args, "path", None)
    repository = (path or args.repo or config.repository or "").strip()
    if not repository:
        raise SystemExit("No repository is configured. Use `axle auth github --repo ...` or pass `--repo` or `--path`.")
    task = _resolve_task(args, config, terminal)
    request = RunRequest(
        task=task,
        task_context=task,
        repository=repository,
        base_branch=(args.branch or config.base_branch or settings.default_base_branch).strip(),
        repository_path=path,
        repository_kind="local" if path else "auto",
        setup_mode=getattr(args, "setup", "auto"),
        test_command=(args.test or config.default_test_command),
        create_pr=bool(args.pr),
        model=args.model or config.llm_model,
        provider=args.provider or config.llm_provider,
        max_repair_attempts=max(0, int(args.repair_attempts)),
        reuse_workspace=not getattr(args, "no_reuse_workspace", False),
    )
    runner = SessionRunner(config=config, terminal=terminal)
    summary = runner.run(request)
    config.last_workspace = summary.workspace
    save_config(config)
    terminal.summary(summary)
    if getattr(args, "chat", False):
        return _interactive_run_loop(config, terminal, runner, request, summary)
    return 0 if summary.status == "completed" else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(_normalize_argv(list(argv) if argv is not None else sys.argv[1:]))
    terminal = Terminal(verbose=not getattr(args, "quiet", False))

    try:
        if args.command == "auth" and args.auth_command == "github":
            return handle_auth_github(args, terminal)
        if args.command == "auth" and args.auth_command == "jira":
            return handle_auth_jira(args, terminal)
        if args.command == "auth" and args.auth_command == "llm":
            return handle_auth_llm(args, terminal)
        if args.command == "init":
            return handle_init(args, terminal)
        if args.command == "config" and args.config_command == "set-goose":
            return handle_config_goose(args, terminal)
        if args.command == "status":
            return handle_status(args, terminal)
        if args.command == "doctor":
            return handle_doctor(args, terminal)
        if args.command == "smoke":
            return handle_smoke(args, terminal)
        if args.command == "jira" and args.jira_command == "get":
            return handle_jira_get(args, terminal)
        if args.command == "run":
            return handle_run(args, terminal)
    except requests.RequestException as exc:
        terminal.error(str(exc))
        return 1
    except ValueError as exc:
        terminal.error(str(exc))
        return 1

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
