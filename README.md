# Axle CLI

Terminal-first CLI for the Axle repo. This app is separate from the dashboard stack and uses Goose as the only execution engine. Axle handles repo setup, terminal output, tests, and PR creation around that Goose session.

1. connect GitHub
2. save Jira credentials
3. save a default repo or local path
4. save a default validation command
5. inspect the repo
6. stream short live updates
7. let Goose edit files directly
8. run tests
9. retry once on failure
10. summarize changes and optionally open a PR

Axle now runs Goose with a stable workspace session, a bundled recipe, and structured output. That means `axle run KAN-2 --chat` continues the same Goose session instead of rebuilding the whole task string from scratch.

## Install

From the repository root:

```bash
cd cli_agent
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

After installation, use the `axle` command directly:

```bash
axle status
axle doctor
```

## Container Build

Build the CLI image with Goose installed at image-build time:

```bash
docker compose --profile cli build cli_agent
```

Run the CLI in the container:

```bash
docker compose --profile cli run --rm cli_agent axle status
```

The container includes:

- Python runtime for `axle`
- `git`
- Goose CLI installed during the Docker build

Override the Goose version if needed:

```bash
docker compose --profile cli build --build-arg GOOSE_VERSION=v1.19.1 cli_agent
```

## Configure

Save GitHub access and a default repo:

```bash
axle auth github --token <github-token> --repo https://github.com/your-org/your-repo.git --branch main
```

Save Goose / model settings:

```bash
axle auth llm --provider openrouter --model qwen/qwen3-coder:free --api-key <llm-key> --base-url https://openrouter.ai/api/v1
```

Save Jira access and an optional default project:

```bash
axle auth jira --base-url https://your-domain.atlassian.net --email you@company.com --api-token <jira-token> --project APP
```

Optional Goose configuration:

```bash
axle config set-goose --binary goose --builtin developer
```

To override the built-in recipe, point Axle at a local Goose recipe file:

```bash
axle config set-goose --recipe /path/to/custom-recipe.yaml
```

## Run

Save a default test command for the repo:

```bash
axle init --repo https://github.com/your-org/your-repo.git --branch main --test "python manage.py test"
```

Fetch a Jira ticket:

```bash
axle jira KAN-2
```

Run the coding flow with the shorthand command:

```bash
axle run KAN-2
```

Run with an explicit operator instruction and PR creation:

```bash
axle run KAN-2 --task "Keep the patch narrow and add focused tests only." --pr
```

If Goose is installed, the CLI invokes it using the official `goose run` flow in the checked-out repo workspace. Goose is the only coding engine; Axle does not run any separate planner/editor/reviewer stack.
Axle uses `goose recipe validate` as the recipe contract and `goose run --output-format stream-json` for structured agent output when available.

## Session And Recipe Model

Axle now treats Goose as a sessioned agent runtime instead of a stateless text generator.

- Axle owns task intake, workspace prep, bootstrap, validation, and PR orchestration.
- Goose owns the coding session, provider/model runtime, recipe-backed behavior, and structured agent output.
- Each reusable Axle workspace gets a stable Goose session name.
- Axle defaults to the built-in Goose recipe at `axle_cli/recipes/axle_run.yaml`.
- Repair passes and `--chat` follow-ups resume the same Goose session for that workspace.

For repeatable provider checks before a full coding run:

```bash
axle doctor
axle smoke
```

The smoke run uses the current provider/model/recipe, starts a real Goose session in the prepared workspace, and reports structured status, tool, and error events without requiring a full implementation task.

## Smoke Tests

Real-provider smoke runs are opt-in and only run when you provide the inputs explicitly.

```bash
AXLE_SMOKE_REPO=/absolute/path/to/project \
AXLE_SMOKE_TASK="Add a focused test for the latest change." \
AXLE_SMOKE_TEST_COMMAND="python manage.py test" \
AXLE_SMOKE_PROVIDER=ollama \
AXLE_SMOKE_MODEL=qwen2.5-coder:14b \
python -m unittest tests.test_setup_features.SetupFeatureTests.test_real_provider_smoke_run_hook
```

Use the same hook with a hosted provider by pointing `AXLE_SMOKE_PROVIDER` and `AXLE_SMOKE_MODEL` at the configured model runtime. The test skips unless the required env vars are present.

## Short Demo Flow

```bash
axle init --repo https://github.com/Mridul009/lister.git --branch main --test "python manage.py test"
axle status
axle doctor
axle smoke
axle jira KAN-2
axle run KAN-2
```

For an interactive follow-up loop after the first run:

```bash
axle run KAN-2 --chat
```

Inside interactive mode:
- plain text resumes the same Goose session with a follow-up instruction
- `/status` prints the latest run summary
- `/diff` prints the current repository diff
- `/retry` resumes the current Goose session without extra instructions
- `/create_pr` commits, pushes, and opens a GitHub PR from the current workspace
- `/exit` leaves interactive mode
