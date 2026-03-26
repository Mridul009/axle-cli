from __future__ import annotations

import argparse
import sys

from .config import settings
from .doctor import run_doctor
from .github_auth import save_github_settings, test_github_token
from .jira import save_jira_settings
from .models import RunRequest
from .session import SessionRunner
from .state import load_config, save_config
from .terminal import Terminal


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="axle", description="Axle terminal-first coding agent.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    auth = subparsers.add_parser("auth", help="Save integration credentials.")
    auth_sub = auth.add_subparsers(dest="auth_command", required=True)

    auth_github = auth_sub.add_parser("github", help="Save/test GitHub settings.")
    auth_github.add_argument("--token", help="GitHub token.")
    auth_github.add_argument("--repo", help="Default repository URL.")
    auth_github.add_argument("--branch", default=settings.default_base_branch, help="Default base branch.")

    auth_jira = auth_sub.add_parser("jira", help="Save/test Jira settings.")
    auth_jira.add_argument("--base-url", help="Jira Cloud base URL, for example https://your-domain.atlassian.net.")
    auth_jira.add_argument("--email", help="Jira account email.")
    auth_jira.add_argument("--api-token", help="Jira API token.")
    auth_jira.add_argument("--project", help="Default Jira project key.")

    auth_llm = auth_sub.add_parser("llm", help="Save Goose provider settings.")
    auth_llm.add_argument("--api-key", required=True, help="Provider API key passed through to Goose.")
    auth_llm.add_argument("--provider", default=settings.default_goose_provider, help="Goose provider name.")
    auth_llm.add_argument("--model", default=settings.default_goose_model, help="Default Goose model.")
    auth_llm.add_argument("--base-url", help="Optional provider base URL.")

    config = subparsers.add_parser("config", help="Update local CLI configuration.")
    config_sub = config.add_subparsers(dest="config_command", required=True)
    config_goose = config_sub.add_parser("set-goose", help="Set Goose executable defaults.")
    config_goose.add_argument("--binary", default=settings.default_goose_binary, help="Goose CLI binary name.")
    config_goose.add_argument("--builtin", default="", help="Optional comma-separated Goose builtin extensions.")

    status = subparsers.add_parser("status", help="Print current local configuration.")
    status.add_argument("--show-secrets", action="store_true", help="Print saved tokens and keys.")

    doctor = subparsers.add_parser("doctor", help="Check local Goose and CLI readiness.")
    doctor.add_argument("--show-secrets", action="store_true", help="Print saved tokens and keys in the report.")

    run = subparsers.add_parser("run", help="Run a coding task.")
    run.add_argument("--task", help="Task prompt. If omitted, stdin is used.")
    run.add_argument("--repo", help="Repository URL or local path.")
    run.add_argument("--branch", help="Base branch.")
    run.add_argument("--test", help="Validation command.")
    run.add_argument("--pr", action="store_true", help="Commit, push, and open a PR after validation.")
    run.add_argument("--model", help="Override the configured Goose model.")
    run.add_argument("--provider", help="Override the configured Goose provider.")
    run.add_argument("--repair-attempts", type=int, default=settings.max_repair_attempts, help="Bounded repair retries.")
    run.add_argument("--quiet", action="store_true", help="Reduce streamed command output.")

    return parser


def _require_task(task: str | None) -> str:
    if task and task.strip():
        return task.strip()
    payload = sys.stdin.read().strip()
    if payload:
        return payload
    raise SystemExit("A task is required. Pass --task or pipe prompt text into the command.")


def handle_auth_github(args: argparse.Namespace, terminal: Terminal) -> int:
    if not any((args.token, args.repo, args.branch)):
        raise SystemExit("Provide at least one of --token, --repo, or --branch.")
    config = save_github_settings(args.token, args.repo, args.branch)
    terminal.info(f"GitHub settings saved. Repository: {config.repository or 'not configured'}.")
    if args.token:
        payload = test_github_token(args.token.strip())
        terminal.info(f"GitHub token is valid for user `{payload.get('login')}`.")
    return 0


def handle_auth_llm(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    config.llm_api_key = args.api_key.strip()
    config.llm_provider = args.provider.strip()
    config.llm_model = args.model.strip()
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


def handle_config_goose(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    config.goose_binary = args.binary.strip()
    config.goose_builtin = args.builtin.strip() or None
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
    terminal.info(f"- Goose builtins: {config.effective_goose_builtins() or 'none'}")
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


def handle_run(args: argparse.Namespace, terminal: Terminal) -> int:
    config = load_config()
    repository = (args.repo or config.repository or "").strip()
    if not repository:
        raise SystemExit("No repository is configured. Use `axle auth github --repo ...` or pass `--repo`.")
    task = _require_task(args.task)
    request = RunRequest(
        task=task,
        repository=repository,
        base_branch=(args.branch or config.base_branch or settings.default_base_branch).strip(),
        test_command=(args.test or config.default_test_command),
        create_pr=bool(args.pr),
        model=args.model or config.llm_model,
        provider=args.provider or config.llm_provider,
        max_repair_attempts=max(0, int(args.repair_attempts)),
    )
    runner = SessionRunner(config=config, terminal=terminal)
    summary = runner.run(request)
    summary.finished_at = summary.finished_at
    config.last_workspace = summary.workspace
    save_config(config)
    terminal.summary(summary)
    return 0 if summary.status == "completed" else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    terminal = Terminal(verbose=not getattr(args, "quiet", False))

    if args.command == "auth" and args.auth_command == "github":
        return handle_auth_github(args, terminal)
    if args.command == "auth" and args.auth_command == "jira":
        return handle_auth_jira(args, terminal)
    if args.command == "auth" and args.auth_command == "llm":
        return handle_auth_llm(args, terminal)
    if args.command == "config" and args.config_command == "set-goose":
        return handle_config_goose(args, terminal)
    if args.command == "status":
        return handle_status(args, terminal)
    if args.command == "doctor":
        return handle_doctor(args, terminal)
    if args.command == "run":
        return handle_run(args, terminal)

    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
