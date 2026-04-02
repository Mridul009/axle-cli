from __future__ import annotations

import importlib
import inspect
import json
import os
import socket
import threading
import sys
import tempfile
import unittest
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock


@unittest.skipUnless(
    importlib.util.find_spec("axle_cli.coordinator") and importlib.util.find_spec("axle_cli.worker"),
    "coordinator/worker modules are not available yet",
)
class CoordinatorWorkerTests(unittest.TestCase):
    def _temp_root(self) -> tempfile.TemporaryDirectory:
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        return tempdir

    def _import_any(self, *module_names: str):
        last_error: ModuleNotFoundError | None = None
        for module_name in module_names:
            try:
                return importlib.import_module(module_name)
            except ModuleNotFoundError as exc:
                last_error = exc
        raise AssertionError(f"Could not import any of: {module_names}") from last_error

    def _get_attr(self, module, *names: str):
        for name in names:
            if hasattr(module, name):
                return getattr(module, name)
        raise AssertionError(f"{module.__name__} does not expose any of: {names}")

    def _coerce_mapping(self, value):
        if value is None:
            return {}
        if isinstance(value, dict):
            return value
        if hasattr(value, "to_json"):
            return value.to_json()
        if is_dataclass(value):
            return asdict(value)
        keys = [
            "run_id",
            "status",
            "task",
            "task_context",
            "repository",
            "base_branch",
            "callback_url",
            "payload",
            "result",
        ]
        return {key: getattr(value, key) for key in keys if hasattr(value, key)}

    def _build_store(self, store_cls, root: Path):
        try:
            return store_cls(root)
        except TypeError:
            try:
                return store_cls(root=root)
            except TypeError:
                return store_cls()

    def _call_compat(self, func, *candidate_args, **candidate_kwargs):
        last_error: Exception | None = None
        for args, kwargs in candidate_args:
            merged_kwargs = dict(candidate_kwargs)
            merged_kwargs.update(kwargs)
            try:
                return func(*args, **merged_kwargs)
            except TypeError as exc:
                last_error = exc
        if candidate_kwargs:
            try:
                return func(**candidate_kwargs)
            except TypeError as exc:
                last_error = exc
        raise AssertionError(f"Could not call {func!r} with compatible arguments") from last_error

    def _call_store_method(self, store, method_names: tuple[str, ...], payload: dict[str, object]):
        method = self._get_attr(store, *method_names)
        sig = inspect.signature(method)
        accepted = {
            name: value
            for name, value in payload.items()
            if name in sig.parameters
        }
        candidate_args: list[tuple[tuple[object, ...], dict[str, object]]] = []
        if accepted:
            candidate_args.append(((), accepted))
        candidate_args.extend(
            [
                ((payload,), {}),
                ((), {"run": payload}),
                ((), {"record": payload}),
                ((), {"data": payload}),
                ((), {"payload": payload}),
            ]
        )
        return self._call_compat(method, *candidate_args)

    def _load_store_record(self, store, method_names: tuple[str, ...], run_id: str):
        method = self._get_attr(store, *method_names)
        return self._call_compat(method, ((run_id,), {}), ((), {"run_id": run_id}), ((), {"id": run_id}))

    def _find_json_with_run_id(self, root: Path, run_id: str) -> dict[str, object]:
        for path in root.rglob("*.json"):
            if "idempotency" in path.parts:
                continue
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("run_id") == run_id and (payload.get("repository") or payload.get("issue_key")):
                return payload
        raise AssertionError(f"Did not find a persisted JSON record for {run_id} under {root}")

    def test_file_backed_run_store_persists_and_round_trips_run_records(self):
        tempdir = self._temp_root()
        coordinator = self._import_any("axle_cli.coordinator.run_store", "axle_cli.coordinator")
        store_cls = self._get_attr(coordinator, "FileBackedRunStore", "RunStore")
        with mock.patch.dict(os.environ, {"AXLE_RUN_STORE": "", "AXLE_SQLITE_PATH": ""}, clear=False):
            store = self._build_store(store_cls, Path(tempdir.name))

        payload = {
            "run_id": "run-123",
            "task": "Update docs",
            "repository": "https://github.com/acme/widgets.git",
            "base_branch": "main",
            "callback_url": "https://example.invalid/callback",
            "status": "queued",
        }

        created = self._call_store_method(store, ("create_run", "create", "save_run", "save", "put", "write_run"), payload)
        loaded = self._load_store_record(store, ("get_run", "get", "load_run", "load", "read_run"), "run-123")

        self.assertEqual(self._coerce_mapping(created).get("run_id", "run-123"), "run-123")
        self.assertEqual(self._coerce_mapping(loaded).get("run_id"), "run-123")
        self.assertEqual(self._find_json_with_run_id(Path(tempdir.name), "run-123")["task"], "Update docs")

    def test_file_backed_run_store_supports_worker_registration_and_claiming_by_default(self):
        tempdir = self._temp_root()
        coordinator = self._import_any("axle_cli.coordinator.run_store", "axle_cli.coordinator")
        store_cls = self._get_attr(coordinator, "FileBackedRunStore", "RunStore")

        with mock.patch.dict(os.environ, {"AXLE_RUN_STORE": "", "AXLE_SQLITE_PATH": ""}, clear=False):
            store = self._build_store(store_cls, Path(tempdir.name))

        self._call_store_method(
            store,
            ("create_run", "create", "save_run", "save", "put", "write_run"),
            {
                "run_id": "queued-file-1",
                "task": "File-backed claim",
                "repository": "https://github.com/acme/widgets.git",
                "base_branch": "main",
                "status": "queued",
            },
        )

        registered = store.register_worker("worker-file", hostname="host-file", capabilities={"executor": "axle"}, version="demo")
        self.assertEqual(registered["worker_id"], "worker-file")
        self.assertEqual(getattr(store, "backend_kind", "file"), "file")

        claimed = store.claim_next_queued_run("worker-file")
        self.assertIsNotNone(claimed)
        claimed_mapping = self._coerce_mapping(claimed)
        self.assertEqual(claimed_mapping.get("status"), "running")
        self.assertEqual(claimed_mapping.get("worker_instance_id"), "worker-file")

    def test_webhook_creates_run_and_persists_callback_metadata(self):
        tempdir = self._temp_root()
        coordinator = self._import_any("axle_cli.coordinator.webhooks", "axle_cli.coordinator")
        store_cls = self._get_attr(coordinator, "FileBackedRunStore", "RunStore")
        store = self._build_store(store_cls, Path(tempdir.name))
        webhook_handler = self._get_attr(
            coordinator,
            "create_run_from_webhook",
            "handle_webhook",
            "webhook_to_run",
            "register_run_from_webhook",
        )

        webhook_payload = {
            "event_id": "evt-456",
            "task": "Patch failing worker run",
            "repository": "https://github.com/acme/widgets.git",
            "base_branch": "main",
            "callback_url": "https://example.invalid/callback",
            "source": "webhook",
        }

        created = self._call_compat(
            webhook_handler,
            ((webhook_payload, store), {}),
            ((store, webhook_payload), {}),
            ((webhook_payload,), {"store": store}),
            ((webhook_payload,), {"run_store": store}),
            ((), webhook_payload | {"store": store}),
        )

        created_mapping = self._coerce_mapping(created)
        run_id = created_mapping.get("run_id") or webhook_payload["event_id"]
        persisted = self._find_json_with_run_id(Path(tempdir.name), run_id)

        self.assertEqual(persisted.get("repository"), webhook_payload["repository"])
        self.assertEqual(persisted.get("callback_url"), webhook_payload["callback_url"])
        self.assertEqual(persisted.get("task"), webhook_payload["task"])

    def test_webhook_idempotency_reuses_the_same_run_id_for_duplicate_payloads(self):
        tempdir = self._temp_root()
        webhook = self._import_any("axle_cli.coordinator.webhook_service")
        store_cls = self._get_attr(self._import_any("axle_cli.coordinator.run_store"), "FileBackedRunStore")
        store = self._build_store(store_cls, Path(tempdir.name))

        payload = {
            "event_id": "evt-789",
            "task": "Deduplicate webhook payloads",
            "repository": "https://github.com/acme/widgets.git",
            "base_branch": "main",
            "status": "queued",
        }

        first = self._call_compat(
            self._get_attr(webhook, "create_run_from_webhook"),
            ((payload, store), {}),
            ((store, payload), {}),
            ((payload,), {"store": store}),
        )
        second = self._call_compat(
            self._get_attr(webhook, "create_run_from_webhook"),
            ((payload, store), {}),
            ((store, payload), {}),
            ((payload,), {"store": store}),
        )

        self.assertEqual(self._coerce_mapping(first).get("run_id"), self._coerce_mapping(second).get("run_id"))
        self.assertEqual(len(store.list_runs()), 1)

    def test_jira_webhook_falls_back_to_inline_issue_payload_when_jira_lookup_fails(self):
        tempdir = self._temp_root()
        webhook = self._import_any("axle_cli.coordinator.webhook_service")
        run_store = self._import_any("axle_cli.coordinator.run_store")
        models = self._import_any("axle_cli.models")
        store = self._build_store(self._get_attr(run_store, "FileBackedRunStore"), Path(tempdir.name))

        payload = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "APP-123",
                "fields": {
                    "summary": "Demo webhook fallback",
                    "description": "Use inline issue payload",
                    "project": {"key": "APP", "name": "Demo"},
                    "labels": ["axle-run"],
                },
            },
        }
        config = models.SavedConfig(
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            jira_base_url="https://example.atlassian.net",
            jira_email="demo@example.com",
            jira_api_token="token",
        )

        with mock.patch("axle_cli.coordinator.api.fetch_jira_issue", side_effect=RuntimeError("jira unavailable")):
            created = webhook.handle_jira_webhook(payload, config, store=store)

        mapping = self._coerce_mapping(created)
        self.assertEqual(mapping.get("issue_key"), "APP-123")
        self.assertEqual(mapping.get("status"), "queued")
        self.assertIn("Demo webhook fallback", mapping.get("task", ""))

    def test_routing_config_applies_project_label_and_component_rules(self):
        tempdir = self._temp_root()
        router = self._import_any("axle_cli.coordinator.router")
        route_issue_to_run = self._get_attr(router, "route_issue_to_run")

        routing_path = Path(tempdir.name) / "routing.json"
        routing_path.write_text(
            json.dumps(
                {
                    "precedence": ["saved_config", "defaults", "rules"],
                    "defaults": {
                        "repository": "git@github.com:acme/default.git",
                        "base_branch": "main",
                        "test_command": "pytest -q",
                        "provider": "openai",
                        "model": "gpt-5-mini",
                    },
                    "rules": [
                        {
                            "match": {
                                "project": "APP",
                                "labels": ["frontend"],
                                "components": ["dashboard"],
                            },
                            "repository": "git@github.com:acme/frontend.git",
                            "base_branch": "develop",
                            "test_command": "npm test",
                            "provider": "openrouter",
                            "model": "qwen/qwen3-coder:free",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        config = self._import_any("axle_cli.models").SavedConfig(
            repository="git@github.com:acme/ignored.git",
            base_branch="release",
            default_test_command="python -m pytest",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )
        issue = {
            "key": "APP-123",
            "url": "https://acme.atlassian.net/browse/APP-123",
            "summary": "Fix dashboard filters",
            "description": "Broken in production",
            "labels": ["frontend", "urgent"],
            "components": [{"name": "dashboard"}],
        }

        run = route_issue_to_run(config, issue, routing_config_path=routing_path)

        self.assertEqual(run.repository, "git@github.com:acme/frontend.git")
        self.assertEqual(run.base_branch, "develop")
        self.assertEqual(run.test_command, "npm test")
        self.assertEqual(run.provider, "openrouter")
        self.assertEqual(run.model, "qwen/qwen3-coder:free")

    def test_routing_config_falls_back_to_defaults_when_no_rule_matches(self):
        tempdir = self._temp_root()
        router = self._import_any("axle_cli.coordinator.router")
        route_issue_to_run = self._get_attr(router, "route_issue_to_run")

        routing_path = Path(tempdir.name) / "routing.json"
        routing_path.write_text(
            json.dumps(
                {
                    "precedence": ["saved_config", "defaults"],
                    "defaults": {
                        "repository": "git@github.com:acme/default.git",
                        "base_branch": "main",
                        "test_command": "pytest -q",
                        "provider": "openai",
                        "model": "gpt-5-mini",
                    },
                    "rules": [
                        {
                            "match": {"project": "OPS", "labels": ["infra"]},
                            "repository": "git@github.com:acme/platform.git",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

        config = self._import_any("axle_cli.models").SavedConfig(
            repository="git@github.com:acme/ignored.git",
            base_branch="release",
            default_test_command="python -m pytest",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )
        issue = {
            "key": "APP-999",
            "url": "https://acme.atlassian.net/browse/APP-999",
            "summary": "Fix dashboard filters",
            "description": "Broken in production",
        }

        run = route_issue_to_run(config, issue, routing_config_path=routing_path)

        self.assertEqual(run.repository, "git@github.com:acme/default.git")
        self.assertEqual(run.base_branch, "main")
        self.assertEqual(run.test_command, "pytest -q")
        self.assertEqual(run.provider, "openai")
        self.assertEqual(run.model, "gpt-5-mini")

    def test_routing_config_supports_precedence_project_profiles_and_profile_inheritance(self):
        tempdir = self._temp_root()
        router = self._import_any("axle_cli.coordinator.router")
        route_issue_to_run = self._get_attr(router, "route_issue_to_run")

        routing_path = Path(tempdir.name) / "routing.json"
        routing_path.write_text(
            json.dumps(
                {
                    "precedence": ["saved_config", "defaults", "projects", "rules"],
                    "defaults": {
                        "repository": "git@github.com:acme/default.git",
                        "base_branch": "main",
                        "test_command": "pytest -q",
                        "provider": "openai",
                        "model": "gpt-5-mini",
                    },
                    "profiles": {
                        "frontend-base": {
                            "repository": "git@github.com:acme/frontend.git",
                            "provider": "openrouter",
                        },
                        "frontend": {
                            "inherits": "frontend-base",
                            "base_branch": "develop",
                            "model": "qwen/qwen3-coder:free",
                        },
                    },
                    "projects": {
                        "APP": {
                            "profile": "frontend",
                            "test_command": "npm test",
                        }
                    },
                    "rules": [
                        {
                            "priority": 10,
                            "match": {"project": "APP", "labels": ["frontend"]},
                            "test_command": "npm test -- --ci",
                        },
                        {
                            "priority": 20,
                            "match": {"project": "APP", "labels": ["frontend"]},
                            "test_command": "npm test -- --runInBand",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

        config = self._import_any("axle_cli.models").SavedConfig(
            repository="git@github.com:acme/saved.git",
            base_branch="release",
            default_test_command="python -m pytest",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )
        issue = {
            "key": "APP-123",
            "url": "https://acme.atlassian.net/browse/APP-123",
            "summary": "Fix dashboard filters",
            "description": "Broken in production",
            "labels": ["frontend", "urgent"],
            "components": [{"name": "dashboard"}],
        }

        run = route_issue_to_run(config, issue, routing_config_path=routing_path)

        self.assertEqual(run.repository, "git@github.com:acme/frontend.git")
        self.assertEqual(run.base_branch, "develop")
        self.assertEqual(run.test_command, "npm test -- --runInBand")
        self.assertEqual(run.provider, "openrouter")
        self.assertEqual(run.model, "qwen/qwen3-coder:free")

    def test_routing_precedence_can_favor_saved_config_over_defaults(self):
        tempdir = self._temp_root()
        router = self._import_any("axle_cli.coordinator.router")
        route_issue_to_run = self._get_attr(router, "route_issue_to_run")

        routing_path = Path(tempdir.name) / "routing.json"
        routing_path.write_text(
            json.dumps(
                {
                    "precedence": ["defaults", "saved_config"],
                    "defaults": {
                        "repository": "git@github.com:acme/default.git",
                        "base_branch": "main",
                        "test_command": "pytest -q",
                        "provider": "openai",
                        "model": "gpt-5-mini",
                    },
                }
            ),
            encoding="utf-8",
        )

        config = self._import_any("axle_cli.models").SavedConfig(
            repository="git@github.com:acme/saved.git",
            base_branch="release",
            default_test_command="python -m pytest",
            llm_provider="ollama",
            llm_model="qwen2.5-coder:14b",
        )
        issue = {
            "key": "OPS-42",
            "url": "https://acme.atlassian.net/browse/OPS-42",
            "summary": "Prefer saved config when precedence flips",
            "description": "Defaults should not override saved values here.",
        }

        run = route_issue_to_run(config, issue, routing_config_path=routing_path)

        self.assertEqual(run.repository, "git@github.com:acme/saved.git")
        self.assertEqual(run.base_branch, "release")
        self.assertEqual(run.test_command, "python -m pytest")
        self.assertEqual(run.provider, "ollama")
        self.assertEqual(run.model, "qwen2.5-coder:14b")

    def test_worker_payload_retrieval_and_callbacks_update_run_state(self):
        tempdir = self._temp_root()
        coordinator = self._import_any("axle_cli.coordinator.run_store", "axle_cli.coordinator")
        worker = self._import_any("axle_cli.worker")
        store_cls = self._get_attr(coordinator, "FileBackedRunStore", "RunStore")
        store = self._build_store(store_cls, Path(tempdir.name))

        payload = {
            "run_id": "run-worker-1",
            "task": "Implement worker callback",
            "repository": "https://github.com/acme/widgets.git",
            "base_branch": "main",
            "callback_url": "https://example.invalid/callback",
            "status": "queued",
        }
        self._call_store_method(store, ("create_run", "create", "save_run", "save", "put", "write_run"), payload)

        payload_loader = self._get_attr(
            worker,
            "load_worker_payload",
            "get_run_payload",
            "fetch_run_payload",
            "load_payload",
        )
        callback_writer = self._get_attr(
            worker,
            "post_callback",
            "send_callback",
            "record_callback",
            "update_callback",
            "update_run",
        )

        loaded_payload = self._call_compat(
            payload_loader,
            ((store, "run-worker-1"), {}),
            (("run-worker-1", store), {}),
            (("run-worker-1",), {"store": store}),
            ((store,), {"run_id": "run-worker-1"}),
        )
        self.assertEqual(self._coerce_mapping(loaded_payload).get("task"), payload["task"])
        self.assertEqual(self._coerce_mapping(loaded_payload).get("run_id"), "run-worker-1")

        callback_result = self._call_compat(
            callback_writer,
            ((store, "run-worker-1", {"status": "completed", "result": "ok"}), {}),
            ((store, "run-worker-1", "completed"), {}),
            (("run-worker-1", {"status": "completed", "result": "ok"}), {"store": store}),
            (("run-worker-1",), {"store": store, "status": "completed", "result": "ok"}),
        )
        callback_mapping = self._coerce_mapping(callback_result)
        self.assertEqual(callback_mapping.get("status", "completed"), "completed")

        refreshed = self._load_store_record(store, ("get_run", "get", "load_run", "load", "read_run"), "run-worker-1")
        refreshed_mapping = self._coerce_mapping(refreshed)
        self.assertEqual(refreshed_mapping.get("status"), "completed")
        self.assertEqual(refreshed_mapping.get("result"), "ok")

    def test_worker_entrypoint_reuses_the_shared_execution_core(self):
        worker = self._import_any("axle_cli.worker")
        entrypoint = self._get_attr(worker, "main", "worker_main", "cli")
        execution_core = self._get_attr(
            worker,
            "execute_run",
            "run_execution_core",
            "run_worker_core",
            "process_run",
        )
        payload_loader = self._get_attr(
            worker,
            "load_worker_payload",
            "get_run_payload",
            "fetch_run_payload",
            "load_payload",
        )
        callback_writer = self._get_attr(
            worker,
            "post_callback",
            "send_callback",
            "record_callback",
            "update_callback",
            "update_run",
        )

        payload = {
            "run_id": "run-entrypoint-1",
            "task": "Reuse execution core",
            "repository": "https://github.com/acme/widgets.git",
            "base_branch": "main",
            "callback_url": "https://example.invalid/callback",
        }

        with mock.patch.object(worker, payload_loader.__name__, return_value=payload) as load_mock:
            with mock.patch.object(worker, callback_writer.__name__, return_value={"status": "completed"}) as callback_mock:
                with mock.patch.object(worker, execution_core.__name__, return_value={"exit_code": 0, "status": "completed"}) as core_mock:
                    if len(inspect.signature(entrypoint).parameters) == 0:
                        with mock.patch.object(sys, "argv", ["axle-worker", "--run-id", "run-entrypoint-1"]):
                            result = entrypoint()
                    else:
                        try:
                            result = entrypoint(["--run-id", "run-entrypoint-1"])
                        except TypeError:
                            result = entrypoint()

        self.assertTrue(core_mock.called, "worker entrypoint did not call the shared execution core")
        self.assertTrue(load_mock.called, "worker entrypoint did not fetch the run payload")
        self.assertTrue(callback_mock.called, "worker entrypoint did not post a callback update")
        self.assertIsNotNone(result)

    def test_jira_webhook_secret_validation_and_issue_normalization(self):
        webhook = self._import_any("axle_cli.coordinator.webhook_service")
        validate_webhook_secret = self._get_attr(webhook, "validate_webhook_secret")
        normalize_webhook_issue_payload = self._get_attr(webhook, "normalize_webhook_issue_payload")
        ensure_supported_webhook_event = self._get_attr(webhook, "ensure_supported_webhook_event")
        sign_webhook_payload = self._get_attr(webhook, "sign_webhook_payload")
        validate_webhook_auth = self._get_attr(webhook, "validate_webhook_auth")

        with self.assertRaises(ValueError):
            validate_webhook_secret("wrong-secret", "expected-secret")
        with self.assertRaises(ValueError):
            ensure_supported_webhook_event({"webhookEvent": "jira:comment_created"})
        self.assertEqual(ensure_supported_webhook_event({"webhookEvent": "jira:issue_created"}), "jira:issue_created")

        payload = {
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "APP-321",
                "fields": {
                    "summary": "Add routing metadata",
                    "description": {
                        "type": "doc",
                        "content": [{"type": "paragraph", "content": [{"type": "text", "text": "Route by label"}]}],
                    },
                    "project": {"key": "APP", "name": "Analytics Platform"},
                    "issuetype": {"name": "Story"},
                    "labels": ["frontend", "urgent"],
                    "components": [{"name": "dashboard"}],
                    "priority": {"name": "High"},
                    "status": {"name": "In Progress"},
                },
            },
        }
        signature = sign_webhook_payload(payload, "expected-secret")
        validate_webhook_auth(payload, provided_signature=signature, webhook_secret="expected-secret")
        with self.assertRaises(ValueError):
            validate_webhook_auth(payload, provided_signature="sha256=deadbeef", webhook_secret="expected-secret")

        normalized = normalize_webhook_issue_payload(
            payload
        )

        self.assertEqual(normalized["key"], "APP-321")
        self.assertEqual(normalized["project_key"], "APP")
        self.assertEqual(normalized["issue_type"], "Story")
        self.assertEqual(normalized["labels"], ["frontend", "urgent"])
        self.assertEqual(normalized["components"], ["dashboard"])
        self.assertEqual(normalized["priority"], "High")
        self.assertEqual(normalized["status"], "In Progress")
        self.assertIn("Route by label", normalized["description"])

    def test_webhook_replay_detects_conflicting_payloads_for_same_event_key(self):
        tempdir = self._temp_root()
        webhook = self._import_any("axle_cli.coordinator.idempotency")
        ledger_cls = self._get_attr(webhook, "FileWebhookIdempotencyLedger")
        ledger = ledger_cls(Path(tempdir.name))

        payload = {
            "event_id": "evt-replay-1",
            "webhookEvent": "jira:issue_updated",
            "issue": {
                "key": "APP-909",
                "fields": {
                    "updated": "2026-03-26T10:00:00.000+0000",
                },
            },
        }
        event_key = self._get_attr(webhook, "normalize_webhook_event_key")(payload)
        reservation = ledger.reserve(
            event_key,
            "run-replay-1",
            payload=payload,
            event_type=self._get_attr(webhook, "normalize_webhook_event_type")(payload),
        )
        duplicate = ledger.reserve(
            event_key,
            "run-replay-2",
            payload=payload,
            event_type=self._get_attr(webhook, "normalize_webhook_event_type")(payload),
        )

        self.assertFalse(reservation.duplicate)
        self.assertTrue(duplicate.duplicate)
        with self.assertRaises(ValueError):
            ledger.reserve(
                event_key,
                "run-replay-3",
                payload={**payload, "extra": "mutated"},
                event_type=self._get_attr(webhook, "normalize_webhook_event_type")(payload),
            )

    def test_jira_notifications_cover_worker_launch_pr_and_failure_details(self):
        notifier = self._import_any("axle_cli.coordinator.jira_notifier")
        models = self._import_any("axle_cli.models")
        add_comment = self._get_attr(notifier, "add_jira_comment")

        run = models.AutomationRun(
            run_id="run-notify-1",
            issue_key="APP-555",
            issue_url="https://example.invalid/browse/APP-555",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Verify notifications",
            pr_url="https://github.com/acme/widgets/pull/55",
            branch_name="axle/run-notify-1",
            changed_files=["app.py", "README.md"],
            failure="pytest failed: 1 test failed",
            worker_launch_spec=models.WorkerLaunchSpec(
                run_id="run-notify-1",
                callback_token="callback-token",
                instance_name="axle-worker-run-notify-1",
                coordinator_url="https://coordinator.example",
                instance_id="i-123456",
                status="launched",
            ),
        )

        with mock.patch.object(notifier, "add_jira_comment") as comment_mock:
            notifier.notify_worker_launched(models.SavedConfig(), run)
            notifier.notify_pr_created(models.SavedConfig(), run)
            notifier.notify_run_completed(models.SavedConfig(), run)

        bodies = [call.args[2] for call in comment_mock.call_args_list]
        self.assertTrue(any("Axle worker launched." in body for body in bodies))
        self.assertTrue(any("Worker instance name: axle-worker-run-notify-1" in body for body in bodies))
        self.assertTrue(any("Axle pull request created." in body for body in bodies))
        self.assertTrue(any("PR: https://github.com/acme/widgets/pull/55" in body for body in bodies))
        self.assertTrue(any("Failure details:" in body and "pytest failed: 1 test failed" in body for body in bodies))

    def test_sqlite_run_store_supports_worker_registration_and_claiming(self):
        tempdir = self._temp_root()
        coordinator = self._import_any("axle_cli.coordinator.run_store")
        store_cls = self._get_attr(coordinator, "SqliteRunStore")
        with mock.patch.dict(
            os.environ,
            {
                "AXLE_RUN_STORE": "sqlite",
                "AXLE_SQLITE_PATH": str(Path(tempdir.name) / "coordinator.sqlite3"),
            },
            clear=False,
        ):
            store = self._build_store(store_cls, Path(tempdir.name))

        created = self._call_store_method(
            store,
            ("create_run", "create", "save_run"),
            {
                "run_id": "queued-1",
                "task": "Implement queue claim",
                "repository": "https://github.com/acme/widgets.git",
                "base_branch": "main",
                "status": "queued",
            },
        )
        self.assertEqual(self._coerce_mapping(created).get("run_id"), "queued-1")

        registered = store.register_worker("worker-a", hostname="host-a", capabilities={"executor": "axle"}, version="test")
        self.assertEqual(registered["worker_id"], "worker-a")
        self.assertEqual(getattr(store, "backend_kind", "sqlite"), "sqlite")

        claimed = store.claim_next_queued_run("worker-a")
        self.assertIsNotNone(claimed)
        claimed_mapping = self._coerce_mapping(claimed)
        self.assertEqual(claimed_mapping.get("run_id"), "queued-1")
        self.assertEqual(claimed_mapping.get("status"), "running")

    def test_recovery_helpers_requeue_stale_and_orphaned_runs_in_file_mode(self):
        tempdir = self._temp_root()
        recovery = self._import_any("axle_cli.coordinator.recovery")
        models = self._import_any("axle_cli.models")
        store_cls = self._get_attr(self._import_any("axle_cli.coordinator.run_store"), "FileBackedRunStore")
        store = self._build_store(store_cls, Path(tempdir.name))

        stale_now = datetime.now(timezone.utc)
        stale_run = models.AutomationRun(
            run_id="run-stale",
            issue_key="APP-200",
            issue_url="https://example.invalid/browse/APP-200",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Recover stale run",
            status="running",
            retry_count=0,
            max_retries=2,
            updated_at=stale_now - timedelta(hours=2),
            created_at=stale_now - timedelta(hours=3),
        )
        orphan_run = models.AutomationRun(
            run_id="run-orphan",
            issue_key="APP-201",
            issue_url="https://example.invalid/browse/APP-201",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Recover orphan run",
            status="running",
            worker_instance_id="worker-missing",
            retry_count=1,
            max_retries=3,
            updated_at=stale_now - timedelta(hours=1),
            created_at=stale_now - timedelta(hours=2),
        )
        store.create_run(stale_run)
        store.create_run(orphan_run)
        stale_path = Path(store.root) / "runs" / "run-stale.json"
        stale_payload = json.loads(stale_path.read_text(encoding="utf-8"))
        stale_payload["created_at"] = (stale_now - timedelta(hours=3)).isoformat()
        stale_payload["updated_at"] = (stale_now - timedelta(hours=2)).isoformat()
        stale_path.write_text(json.dumps(stale_payload, indent=2) + "\n", encoding="utf-8")

        recovered = recovery.recover_stale_runs(store, stale_after_seconds=1, max_retries=2)
        cleaned = recovery.cleanup_orphan_runs(store, stale_after_seconds=1, max_retries=3)

        recovered_mapping = {item.run_id: self._coerce_mapping(item) for item in recovered}
        cleaned_mapping = {item.run_id: self._coerce_mapping(item) for item in cleaned}

        self.assertIn("run-stale", recovered_mapping)
        self.assertEqual(recovered_mapping["run-stale"].get("status"), "queued")
        self.assertEqual(recovered_mapping["run-stale"].get("retry_count"), 1)
        self.assertIn("run-orphan", cleaned_mapping)
        self.assertEqual(cleaned_mapping["run-orphan"].get("status"), "queued")
        self.assertEqual(cleaned_mapping["run-orphan"].get("retry_count"), 2)

    def test_cleanup_orphan_runs_skips_live_local_worker_processes(self):
        tempdir = self._temp_root()
        recovery = self._import_any("axle_cli.coordinator.recovery")
        models = self._import_any("axle_cli.models")
        store_cls = self._get_attr(self._import_any("axle_cli.coordinator.run_store"), "FileBackedRunStore")
        store = self._build_store(store_cls, Path(tempdir.name))

        run = models.AutomationRun(
            run_id="run-local-live",
            issue_key="APP-202",
            issue_url="https://example.invalid/browse/APP-202",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Keep local worker active",
            status="running",
            worker_instance_id="local-43210",
            retry_count=0,
            max_retries=1,
            updated_at=datetime.now(timezone.utc),
            created_at=datetime.now(timezone.utc),
        )
        store.create_run(run)

        with mock.patch.object(recovery, "_local_worker_is_alive", return_value=True):
            cleaned = recovery.cleanup_orphan_runs(store, stale_after_seconds=1, max_retries=1)

        self.assertEqual(cleaned, [])

    def test_recovery_paths_emit_jira_notifications_for_requeue_and_orphan_cleanup(self):
        tempdir = self._temp_root()
        api = self._import_any("axle_cli.coordinator.api")
        models = self._import_any("axle_cli.models")
        store_cls = self._get_attr(self._import_any("axle_cli.coordinator.run_store"), "FileBackedRunStore")
        store = self._build_store(store_cls, Path(tempdir.name))

        stale_now = datetime.now(timezone.utc)
        stale_run = models.AutomationRun(
            run_id="run-stale-notify",
            issue_key="APP-300",
            issue_url="https://example.invalid/browse/APP-300",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Recover stale run",
            status="running",
            retry_count=0,
            max_retries=2,
            updated_at=stale_now - timedelta(hours=2),
            created_at=stale_now - timedelta(hours=3),
        )
        orphan_run = models.AutomationRun(
            run_id="run-orphan-notify",
            issue_key="APP-301",
            issue_url="https://example.invalid/browse/APP-301",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Recover orphan run",
            status="running",
            worker_instance_id="worker-missing",
            retry_count=1,
            max_retries=3,
            updated_at=stale_now - timedelta(hours=1),
            created_at=stale_now - timedelta(hours=2),
        )
        store.create_run(stale_run)
        store.create_run(orphan_run)
        stale_path = Path(store.root) / "runs" / "run-stale-notify.json"
        orphan_path = Path(store.root) / "runs" / "run-orphan-notify.json"
        stale_payload = json.loads(stale_path.read_text(encoding="utf-8"))
        stale_payload["created_at"] = (stale_now - timedelta(hours=3)).isoformat()
        stale_payload["updated_at"] = (stale_now - timedelta(hours=2)).isoformat()
        stale_path.write_text(json.dumps(stale_payload, indent=2) + "\n", encoding="utf-8")
        orphan_payload = json.loads(orphan_path.read_text(encoding="utf-8"))
        orphan_payload["created_at"] = (stale_now + timedelta(minutes=10)).isoformat()
        orphan_payload["updated_at"] = (stale_now + timedelta(minutes=10)).isoformat()
        orphan_path.write_text(json.dumps(orphan_payload, indent=2) + "\n", encoding="utf-8")

        api_module = self._import_any("axle_cli.coordinator.api")
        with mock.patch.object(api_module, "notify_run_recovered") as recovered_mock:
            with mock.patch.object(api_module, "notify_run_requeued") as requeued_mock:
                with mock.patch.object(api_module, "notify_orphan_cleanup") as orphan_mock:
                    service = api.CoordinatorService(models.SavedConfig(), store=store)
                    recovered = service.recover_stale_runs(stale_after_seconds=1, max_retries=2)
                    cleaned = service.cleanup_orphan_runs(stale_after_seconds=1, max_retries=3)

        self.assertTrue(recovered)
        self.assertTrue(cleaned)
        self.assertTrue(recovered_mock.called)
        self.assertTrue(requeued_mock.called)
        self.assertTrue(orphan_mock.called)
        self.assertEqual(recovered_mock.call_args.args[1].run_id, "run-stale-notify")
        self.assertEqual(orphan_mock.call_args.args[1].run_id, "run-orphan-notify")

    def test_recovery_helpers_reconcile_and_terminate_ec2_runs_before_requeueing(self):
        tempdir = self._temp_root()
        recovery = self._import_any("axle_cli.coordinator.recovery")
        models = self._import_any("axle_cli.models")
        store_cls = self._get_attr(self._import_any("axle_cli.coordinator.run_store"), "FileBackedRunStore")
        store = self._build_store(store_cls, Path(tempdir.name))

        stale_now = datetime.now(timezone.utc)
        stale_run = models.AutomationRun(
            run_id="run-stale-ec2",
            issue_key="APP-400",
            issue_url="https://example.invalid/browse/APP-400",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Recover EC2-backed stale run",
            status="running",
            retry_count=0,
            max_retries=2,
            worker_instance_id="i-ec2-stale",
            updated_at=stale_now - timedelta(hours=2),
            created_at=stale_now - timedelta(hours=3),
            worker_launch_spec=models.WorkerLaunchSpec(
                run_id="run-stale-ec2",
                callback_token="callback-token",
                instance_name="axle-worker-run-stale-ec2",
                coordinator_url="https://coordinator.example",
                instance_id="i-ec2-stale",
                terminate_on_finish=True,
                status="launched",
            ),
        )
        store.create_run(stale_run)

        launcher = mock.MagicMock()
        launcher.terminate_run_instance.return_value = {
            "run_id": "run-stale-ec2",
            "instance_id": "i-ec2-stale",
            "instance_state": "running",
            "instance_present": True,
            "instance_active": True,
            "terminated": True,
            "terminated_instance_ids": ["i-ec2-stale"],
        }

        recovered = recovery.recover_stale_runs(store, stale_after_seconds=1, max_retries=2, launcher=launcher)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0].run_id, "run-stale-ec2")
        self.assertTrue(launcher.terminate_run_instance.called)
        self.assertIn("terminated", recovered[0].recovery_reason.lower())

    def test_store_factory_defaults_to_file_mode_and_can_switch_to_sqlite(self):
        tempdir = self._temp_root()
        factory = self._import_any("axle_cli.coordinator.store_factory")

        with mock.patch.dict(os.environ, {"AXLE_RUN_STORE": "file", "AXLE_SQLITE_PATH": ""}, clear=False):
            file_store = factory.open_run_store(root=Path(tempdir.name))
        self.assertEqual(file_store.__class__.__name__, "FileBackedRunStore")

        sqlite_path = Path(tempdir.name) / "coordinator.sqlite3"
        with mock.patch.dict(os.environ, {"AXLE_RUN_STORE": "sqlite", "AXLE_SQLITE_PATH": str(sqlite_path)}, clear=False):
            sqlite_store = factory.open_run_store(root=Path(tempdir.name))
        self.assertEqual(sqlite_store.__class__.__name__, "SqliteRunStore")

    def test_file_artifact_store_persists_workspace_artifacts_and_summary(self):
        tempdir = self._temp_root()
        artifact_module = self._import_any("axle_cli.coordinator.artifact_store")
        store_cls = self._get_attr(artifact_module, "FileArtifactStore")

        workspace_root = Path(tempdir.name) / "workspace"
        workspace_root.mkdir(parents=True, exist_ok=True)
        (workspace_root / "goose-transcript.log").write_text("transcript body", encoding="utf-8")
        (workspace_root / "changes.patch").write_text("diff body", encoding="utf-8")
        (workspace_root / "test.log").write_text("test log body", encoding="utf-8")

        store = store_cls(root=Path(tempdir.name) / "artifacts")
        manifest = store.store_run_artifacts(
            "run-artifacts-1",
            source_dir=workspace_root,
            summary={
                "run_id": "run-artifacts-1",
                "status": "completed",
                "test_result": {"command": "pytest -q", "exit_code": 0, "output": "ok", "timed_out": False},
            },
        )

        run_root = Path(manifest["manifest"]).parent
        self.assertEqual((run_root / "goose-transcript.log").read_text(encoding="utf-8"), "transcript body")
        self.assertEqual((run_root / "changes.patch").read_text(encoding="utf-8"), "diff body")
        self.assertEqual((run_root / "test.log").read_text(encoding="utf-8"), "test log body")
        summary_payload = json.loads((run_root / "summary.json").read_text(encoding="utf-8"))
        self.assertEqual(summary_payload["run_id"], "run-artifacts-1")
        self.assertEqual(summary_payload["status"], "completed")

    def test_http_coordinator_registers_workers_claims_runs_and_accepts_completion(self):
        requests = importlib.import_module("requests")
        tempdir = self._temp_root()
        coordinator_server_module = self._import_any("axle_cli.coordinator.server")
        server_cls = self._get_attr(coordinator_server_module, "CoordinatorHttpServer")

        try:
            server = server_cls(("127.0.0.1", 0), store_root=tempdir.name, admin_token="admin-secret", worker_token="worker-secret")
        except PermissionError as exc:
            self.skipTest(f"local socket bind is not permitted in this environment: {exc}")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 1)

        base_url = f"http://127.0.0.1:{server.server_port}"
        admin_headers = {"Authorization": "Bearer admin-secret"}
        worker_headers = {"Authorization": "Bearer worker-secret"}

        created = requests.post(
            f"{base_url}/api/webhooks/runs",
            headers=admin_headers,
            json={
                "run_id": "net-1",
                "task": "Handle networked worker",
                "repository": "https://github.com/acme/widgets.git",
                "base_branch": "main",
                "status": "queued",
            },
            timeout=5,
        )
        self.assertEqual(created.status_code, 201)
        created_payload = created.json()
        callback_token = created_payload["callback_token"]

        registered = requests.post(
            f"{base_url}/api/workers/register",
            headers=worker_headers,
            json={"worker_id": "worker-1", "hostname": "test-host", "capabilities": {"executor": "axle"}},
            timeout=5,
        )
        self.assertEqual(registered.status_code, 200)

        claimed = requests.post(
            f"{base_url}/api/workers/claim",
            headers=worker_headers,
            json={"worker_id": "worker-1"},
            timeout=5,
        )
        self.assertEqual(claimed.status_code, 200)
        run_payload = claimed.json()["run"]
        self.assertEqual(run_payload["run_id"], "net-1")
        self.assertEqual(run_payload["status"], "running")

        completed = requests.post(
            f"{base_url}/api/runs/net-1/complete",
            json={"callback_token": callback_token, "status": "completed", "result": "ok"},
            timeout=5,
        )
        self.assertEqual(completed.status_code, 200)
        self.assertEqual(completed.json()["status"], "completed")

        artifacts = requests.post(
            f"{base_url}/api/runs/net-1/artifacts",
            json={
                "callback_token": callback_token,
                "transcript": "remote transcript",
                "diff": "remote diff",
                "test_log": "remote test log",
                "summary": {"run_id": "net-1", "status": "completed"},
            },
            timeout=5,
        )
        self.assertEqual(artifacts.status_code, 200)
        artifact_root = server.artifact_store.root / "net-1"
        self.assertEqual((artifact_root / "goose-transcript.log").read_text(encoding="utf-8"), "remote transcript")
        self.assertEqual((artifact_root / "changes.patch").read_text(encoding="utf-8"), "remote diff")
        self.assertEqual((artifact_root / "test.log").read_text(encoding="utf-8"), "remote test log")
        self.assertEqual(json.loads((artifact_root / "summary.json").read_text(encoding="utf-8"))["status"], "completed")

    def test_ec2_worker_launcher_renders_bootstrap_manifest_and_shutdown_hook(self):
        coordinator = self._import_any("axle_cli.coordinator.worker_launcher")
        models = self._import_any("axle_cli.models")
        launcher_cls = self._get_attr(coordinator, "Ec2WorkerLauncher")

        run = models.AutomationRun(
            run_id="run-ec2-1",
            issue_key="APP-101",
            issue_url="https://jira.example/browse/APP-101",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Launch worker safely",
        )
        launcher = launcher_cls(terminate_on_finish=True, tags={"Owner": "Axle"})

        spec = launcher.build_launch_spec(run)
        self.assertTrue(spec.terminate_on_finish)
        self.assertEqual(spec.bootstrap_manifest["run"]["run_id"], "run-ec2-1")
        self.assertEqual(spec.bootstrap_manifest["auth"]["callback_token"], run.callback_token)
        self.assertEqual(spec.launch_config["tags"]["Owner"], "Axle")
        self.assertIn("AXLE_BOOTSTRAP_FILE", spec.user_data)
        self.assertIn("cleanup()", spec.user_data)
        self.assertIn("shutdown -h now", spec.user_data)
        self.assertIn("python3 -m axle_cli.worker.entrypoint run --run-id run-ec2-1 --callback-token", spec.user_data)

    def test_ec2_worker_launcher_maps_launch_config_to_ec2_params(self):
        coordinator = self._import_any("axle_cli.coordinator.worker_launcher")
        models = self._import_any("axle_cli.models")
        launcher_cls = self._get_attr(coordinator, "Ec2WorkerLauncher")

        run = models.AutomationRun(
            run_id="run-ec2-2",
            issue_key="APP-102",
            issue_url="https://jira.example/browse/APP-102",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Launch with EC2 config",
        )

        ec2_client = mock.MagicMock()
        ec2_client.run_instances.return_value = {"Instances": [{"InstanceId": "i-123456"}]}
        fake_boto3 = mock.MagicMock()
        fake_boto3.client.return_value = ec2_client

        launcher = launcher_cls(
            ami_id="ami-123456",
            instance_type="m6i.large",
            subnet_id="subnet-123",
            security_group_id="sg-primary",
            security_group_ids=["sg-extra"],
            instance_profile_name="axle-profile",
            key_name="axle-key",
            associate_public_ip_address=False,
            terminate_on_finish=False,
            tags={"Project": "Axle"},
        )

        with mock.patch.dict(sys.modules, {"boto3": fake_boto3}):
            spec = launcher.launch(run, coordinator_url="https://coordinator.example")

        self.assertEqual(spec.instance_id, "i-123456")
        self.assertEqual(spec.status, "launched")
        self.assertEqual(ec2_client.run_instances.call_count, 1)
        kwargs = ec2_client.run_instances.call_args.kwargs
        self.assertEqual(kwargs["ImageId"], "ami-123456")
        self.assertEqual(kwargs["InstanceType"], "m6i.large")
        self.assertEqual(kwargs["KeyName"], "axle-key")
        self.assertEqual(kwargs["IamInstanceProfile"], {"Name": "axle-profile"})
        self.assertEqual(kwargs["InstanceInitiatedShutdownBehavior"], "stop")
        self.assertNotIn("SubnetId", kwargs)
        self.assertNotIn("SecurityGroupIds", kwargs)
        self.assertEqual(kwargs["NetworkInterfaces"][0]["SubnetId"], "subnet-123")
        self.assertFalse(kwargs["NetworkInterfaces"][0]["AssociatePublicIpAddress"])
        self.assertEqual(kwargs["NetworkInterfaces"][0]["Groups"], ["sg-extra", "sg-primary"])
        tags = kwargs["TagSpecifications"][0]["Tags"]
        self.assertIn({"Key": "Name", "Value": "axle-worker-run-ec2-"}, tags)
        self.assertIn({"Key": "AxleRunId", "Value": "run-ec2-2"}, tags)
        self.assertIn({"Key": "AxleCoordinator", "Value": "https://coordinator.example"}, tags)
        self.assertIn({"Key": "Project", "Value": "Axle"}, tags)

    def test_ec2_worker_launcher_reconciles_instances_and_terminates_active_workers(self):
        coordinator = self._import_any("axle_cli.coordinator.worker_launcher")
        models = self._import_any("axle_cli.models")
        launcher_cls = self._get_attr(coordinator, "Ec2WorkerLauncher")

        ec2_client = mock.MagicMock()
        ec2_client.describe_instances.return_value = {
            "Reservations": [
                {
                    "Instances": [
                        {
                            "InstanceId": "i-ec2-123",
                            "State": {"Name": "running"},
                            "Tags": [
                                {"Key": "AxleRunId", "Value": "run-ec2-3"},
                                {"Key": "Name", "Value": "axle-worker-run-ec2-3"},
                            ],
                        }
                    ]
                }
            ]
        }
        ec2_client.terminate_instances.return_value = {
            "TerminatingInstances": [{"InstanceId": "i-ec2-123"}]
        }

        launcher = launcher_cls(client_factory=lambda: ec2_client)
        run = models.AutomationRun(
            run_id="run-ec2-3",
            issue_key="APP-103",
            issue_url="https://jira.example/browse/APP-103",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Reconcile EC2 worker",
            status="running",
            worker_instance_id="i-ec2-123",
            worker_launch_spec=models.WorkerLaunchSpec(
                run_id="run-ec2-3",
                callback_token="callback-token",
                instance_name="axle-worker-run-ec2-3",
                coordinator_url="https://coordinator.example",
                instance_id="i-ec2-123",
                terminate_on_finish=True,
                status="launched",
            ),
        )

        reconciliation = launcher.reconcile_run_instance(run, coordinator_url="https://coordinator.example")
        terminated = launcher.terminate_run_instance(run, coordinator_url="https://coordinator.example")

        self.assertTrue(reconciliation["instance_present"])
        self.assertTrue(reconciliation["instance_active"])
        self.assertEqual(reconciliation["instance_id"], "i-ec2-123")
        self.assertEqual(reconciliation["instance_state"], "running")
        self.assertTrue(terminated["terminated"])
        self.assertEqual(terminated["terminated_instance_ids"], ["i-ec2-123"])
        ec2_client.describe_instances.assert_called()
        ec2_client.terminate_instances.assert_called_once_with(InstanceIds=["i-ec2-123"])

    def test_ec2_worker_launcher_is_safe_without_boto3(self):
        coordinator = self._import_any("axle_cli.coordinator.worker_launcher")
        models = self._import_any("axle_cli.models")
        launcher_cls = self._get_attr(coordinator, "Ec2WorkerLauncher")

        launcher = launcher_cls(client_factory=lambda: None)
        run = models.AutomationRun(
            run_id="run-ec2-4",
            issue_key="APP-104",
            issue_url="https://jira.example/browse/APP-104",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="No boto3 path",
        )

        self.assertEqual(launcher.describe_instances(), [])
        self.assertEqual(launcher.terminate_instances(["i-unknown"]), [])
        reconciliation = launcher.reconcile_run_instance(run)
        terminated = launcher.terminate_run_instance(run)
        self.assertFalse(reconciliation["instance_present"])
        self.assertEqual(reconciliation["instance_state"], "missing")
        self.assertFalse(terminated["terminated"])
        self.assertEqual(terminated["termination_skipped"], "no_instance")

    def test_build_worker_launcher_reads_env_backed_ec2_config(self):
        coordinator = self._import_any("axle_cli.coordinator.worker_launcher")
        build_worker_launcher = self._get_attr(coordinator, "build_worker_launcher")

        with mock.patch.dict(
            os.environ,
            {
                "AXLE_EC2_AMI_ID": "ami-demo-123",
                "AXLE_EC2_INSTANCE_TYPE": "m7i.large",
                "AXLE_EC2_SUBNET_ID": "subnet-demo",
                "AXLE_EC2_SECURITY_GROUP_ID": "sg-primary",
                "AXLE_EC2_SECURITY_GROUP_IDS": "sg-extra, sg-other",
                "AXLE_EC2_INSTANCE_PROFILE_NAME": "axle-ec2-profile",
                "AXLE_EC2_KEY_NAME": "axle-demo-key",
                "AXLE_EC2_ASSOCIATE_PUBLIC_IP_ADDRESS": "true",
                "AXLE_EC2_TERMINATE_ON_FINISH": "false",
            },
            clear=False,
        ):
            launcher = build_worker_launcher(mode="ec2")

        self.assertEqual(launcher.ami_id, "ami-demo-123")
        self.assertEqual(launcher.instance_type, "m7i.large")
        self.assertEqual(launcher.subnet_id, "subnet-demo")
        self.assertEqual(launcher.security_group_id, "sg-primary")
        self.assertEqual(launcher.security_group_ids, ["sg-extra", "sg-other"])
        self.assertEqual(launcher.instance_profile_name, "axle-ec2-profile")
        self.assertEqual(launcher.key_name, "axle-demo-key")
        self.assertTrue(launcher.associate_public_ip_address)
        self.assertFalse(launcher.terminate_on_finish)

    def test_local_worker_launcher_spawns_local_demo_process(self):
        coordinator = self._import_any("axle_cli.coordinator.worker_launcher")
        models = self._import_any("axle_cli.models")
        launcher_cls = self._get_attr(coordinator, "LocalWorkerLauncher")

        launcher = launcher_cls(store_root="/tmp/axle-demo-store", working_directory="/tmp")
        run = models.AutomationRun(
            run_id="run-local-1",
            issue_key="APP-105",
            issue_url="https://jira.example/browse/APP-105",
            repository="https://github.com/acme/widgets.git",
            base_branch="main",
            task="Run locally",
        )

        with mock.patch("axle_cli.coordinator.worker_launcher.subprocess.Popen") as popen:
            popen.return_value.pid = 43210
            spec = launcher.launch(run)

        self.assertEqual(spec.status, "launched")
        self.assertEqual(spec.instance_id, "local-43210")
        self.assertTrue(str(spec.launch_config.get("log_path") or "").endswith("run-local-1.log"))
        command = popen.call_args.args[0]
        self.assertIn("-m", command)
        self.assertIn("axle_cli.worker.entrypoint", command)
        self.assertIn("--run-id", command)
        self.assertIn("run-local-1", command)
        self.assertIn("--store-root", command)
        self.assertIn("/tmp/axle-demo-store", command)

    def test_worker_runtime_adds_progress_percent_to_emitted_events(self):
        runtime = self._import_any("axle_cli.worker.runtime")
        progress_payload = self._get_attr(runtime, "_progress_payload")

        payload = progress_payload({"kind": "step", "stage": "Workspace", "message": "Preparing workspace"})
        self.assertEqual(payload.get("progress_percent"), "10")

        complete = progress_payload({"kind": "complete", "stage": "Worker", "message": "done"})
        self.assertEqual(complete.get("progress_percent"), "100")
