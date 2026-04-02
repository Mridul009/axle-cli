from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from ..models import AutomationRun, SavedConfig

ROUTING_CONFIG_ENV_VAR = "AXLE_ROUTING_CONFIG"
DEFAULT_ROUTING_CONFIG_PATH = Path(__file__).with_name("routing.json")
DEFAULT_PRECEDENCE = ("defaults", "saved_config", "projects", "rules")


@dataclass
class RoutingProfile:
    repository: str | None = None
    base_branch: str | None = None
    test_command: str | None = None
    provider: str | None = None
    model: str | None = None
    inherits: str | None = None


@dataclass
class RoutingRule:
    match: dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    repository: str | None = None
    base_branch: str | None = None
    test_command: str | None = None
    provider: str | None = None
    model: str | None = None
    profile: str | None = None
    profile_config: RoutingProfile | None = None


@dataclass
class RoutingConfig:
    defaults: RoutingProfile = field(default_factory=RoutingProfile)
    profiles: dict[str, RoutingProfile] = field(default_factory=dict)
    projects: dict[str, RoutingProfile] = field(default_factory=dict)
    rules: list[RoutingRule] = field(default_factory=list)
    precedence: tuple[str, ...] = DEFAULT_PRECEDENCE


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _profile_mapping(profile: RoutingProfile) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for key in ("repository", "base_branch", "test_command", "provider", "model"):
        value = getattr(profile, key)
        if value is not None:
            mapping[key] = value
    return mapping


def _coerce_profile(payload: Any) -> RoutingProfile:
    if isinstance(payload, str):
        return RoutingProfile(inherits=_clean_text(payload))
    if not isinstance(payload, Mapping):
        return RoutingProfile()
    inherits = _clean_text(payload.get("inherits") or payload.get("profile"))
    return RoutingProfile(
        repository=_clean_text(payload.get("repository")),
        base_branch=_clean_text(payload.get("base_branch")),
        test_command=_clean_text(payload.get("test_command")),
        provider=_clean_text(payload.get("provider")),
        model=_clean_text(payload.get("model")),
        inherits=inherits,
    )


def _coerce_rule(payload: Mapping[str, Any]) -> RoutingRule:
    raw_profile = payload.get("profile")
    profile = _coerce_profile(raw_profile) if isinstance(raw_profile, Mapping) else None
    return RoutingRule(
        match=dict(payload.get("match") or {}),
        priority=int(payload.get("priority") or 0),
        repository=_clean_text(payload.get("repository")),
        base_branch=_clean_text(payload.get("base_branch")),
        test_command=_clean_text(payload.get("test_command")),
        provider=_clean_text(payload.get("provider")),
        model=_clean_text(payload.get("model")),
        profile=_clean_text(raw_profile) if isinstance(raw_profile, str) else None,
        profile_config=profile,
    )


def _normalize_profile_name(name: str | None) -> str | None:
    cleaned = _clean_text(name)
    return cleaned.lower() if cleaned else None


def _normalize_project_key(project_key: str | None) -> str | None:
    cleaned = _clean_text(project_key)
    return cleaned.upper() if cleaned else None


def _load_json_config(path: Path | None) -> dict[str, Any]:
    if path is None or not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError("Routing config must be a JSON object.")
    return payload


def _coerce_routing_config(payload: Mapping[str, Any]) -> RoutingConfig:
    defaults = _coerce_profile(payload.get("defaults"))
    profiles = {
        _normalize_profile_name(name): _coerce_profile(profile)
        for name, profile in dict(payload.get("profiles") or {}).items()
        if _normalize_profile_name(name)
    }
    projects = {
        _normalize_project_key(name): _coerce_profile(profile)
        for name, profile in dict(payload.get("projects") or {}).items()
        if _normalize_project_key(name)
    }
    rules = [
        _coerce_rule(rule)
        for rule in payload.get("rules", [])
        if isinstance(rule, Mapping)
    ]
    precedence = tuple(
        _clean_text(entry).lower()
        for entry in payload.get("precedence") or DEFAULT_PRECEDENCE
        if _clean_text(entry)
    )
    if not precedence:
        precedence = DEFAULT_PRECEDENCE
    return RoutingConfig(
        defaults=defaults,
        profiles=profiles,
        projects=projects,
        rules=rules,
        precedence=precedence,
    )


def load_routing_config(routing_config_path: str | Path | None = None) -> RoutingConfig:
    if routing_config_path is not None:
        config_path = Path(routing_config_path)
    else:
        env_path = _clean_text(os.getenv(ROUTING_CONFIG_ENV_VAR))
        config_path = Path(env_path) if env_path else DEFAULT_ROUTING_CONFIG_PATH
    return _coerce_routing_config(_load_json_config(config_path))


def _issue_project_key(issue: Mapping[str, Any]) -> str | None:
    explicit = issue.get("project_key")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip().upper()
    project = issue.get("project")
    if isinstance(project, Mapping):
        nested = project.get("key") or project.get("project_key")
        if isinstance(nested, str) and nested.strip():
            return nested.strip().upper()
    issue_key = _clean_text(issue.get("key") or issue.get("issue_key"))
    if issue_key and "-" in issue_key:
        return issue_key.split("-", 1)[0].upper()
    return None


def _issue_labels(issue: Mapping[str, Any]) -> set[str]:
    labels = issue.get("labels") or []
    return {
        _clean_text(label).lower()
        for label in labels
        if _clean_text(label)
    }


def _issue_components(issue: Mapping[str, Any]) -> set[str]:
    components = issue.get("components") or []
    values: set[str] = set()
    for component in components:
        if isinstance(component, Mapping):
            name = component.get("name") or component.get("value")
        else:
            name = component
        cleaned = _clean_text(name)
        if cleaned:
            values.add(cleaned.lower())
    return values


def _match_rule(issue: Mapping[str, Any], rule: RoutingRule) -> bool:
    match = rule.match or {}
    project_key = _issue_project_key(issue)
    labels = _issue_labels(issue)
    components = _issue_components(issue)

    expected_project = _clean_text(match.get("project") or match.get("project_key"))
    if expected_project and project_key != expected_project.upper():
        return False

    expected_labels = [
        _clean_text(label).lower()
        for label in match.get("labels") or []
        if _clean_text(label)
    ]
    if expected_labels and not set(expected_labels).issubset(labels):
        return False

    expected_components = [
        _clean_text(component).lower()
        for component in match.get("components") or []
        if _clean_text(component)
    ]
    if expected_components and not set(expected_components).issubset(components):
        return False

    expected_issue_type = _clean_text(match.get("issue_type"))
    if expected_issue_type:
        issue_type = _clean_text(issue.get("issue_type"))
        if issue_type is None:
            issue_type = _clean_text(issue.get("type"))
        if issue_type is None:
            issue_type = _clean_text(issue.get("issuetype"))
        if issue_type is None:
            nested = issue.get("issue")
            if isinstance(nested, Mapping):
                issue_type = _clean_text(nested.get("issue_type") or nested.get("type"))
        if not issue_type or issue_type.lower() != expected_issue_type.lower():
            return False

    return True


def _select_rule(issue: Mapping[str, Any], config: RoutingConfig) -> RoutingRule | None:
    matches = [rule for rule in config.rules if _match_rule(issue, rule)]
    if not matches:
        return None
    return sorted(enumerate(matches), key=lambda item: (-item[1].priority, item[0]))[0][1]


def _resolve_profile_name(name: str | None, config: RoutingConfig, *, seen: set[str] | None = None) -> RoutingProfile:
    normalized = _normalize_profile_name(name)
    if not normalized:
        return RoutingProfile()
    if seen is None:
        seen = set()
    if normalized in seen:
        raise ValueError(f"Routing profile inheritance cycle detected for `{name}`.")
    seen = set(seen)
    seen.add(normalized)
    profile = config.profiles.get(normalized)
    if profile is None:
        return RoutingProfile()
    return _resolve_profile(profile, config, seen=seen)


def _resolve_profile(profile: RoutingProfile, config: RoutingConfig, *, seen: set[str] | None = None) -> RoutingProfile:
    resolved = RoutingProfile()
    if profile.inherits:
        resolved = _resolve_profile_name(profile.inherits, config, seen=seen)
    for key in ("repository", "base_branch", "test_command", "provider", "model"):
        value = getattr(profile, key)
        if value is not None:
            setattr(resolved, key, value)
    return resolved


def _resolve_project_profile(project_key: str | None, config: RoutingConfig) -> RoutingProfile:
    normalized = _normalize_project_key(project_key)
    if not normalized:
        return RoutingProfile()
    profile = config.projects.get(normalized)
    if profile is None:
        profile = config.projects.get("*") or config.projects.get("DEFAULT")
    if profile is None:
        return RoutingProfile()
    return _resolve_profile(profile, config)


def _resolve_rule_profile(rule: RoutingRule, config: RoutingConfig) -> RoutingProfile:
    base = RoutingProfile()
    if rule.profile_config is not None:
        base = _resolve_profile(rule.profile_config, config)
    elif rule.profile:
        base = _resolve_profile_name(rule.profile, config)
    for key in ("repository", "base_branch", "test_command", "provider", "model"):
        value = getattr(rule, key)
        if value is not None:
            setattr(base, key, value)
    return base


def _saved_config_profile(config: SavedConfig) -> RoutingProfile:
    return RoutingProfile(
        repository=_clean_text(config.repository),
        base_branch=_clean_text(config.base_branch),
        test_command=_clean_text(config.default_test_command),
        provider=_clean_text(config.llm_provider),
        model=_clean_text(config.llm_model),
    )


def _default_issue_url(issue: Mapping[str, Any], issue_key: str, config: SavedConfig) -> str:
    issue_url = _clean_text(issue.get("url") or issue.get("issue_url"))
    if issue_url:
        return issue_url
    if config.jira_base_url:
        return f"{config.jira_base_url.rstrip('/')}/browse/{issue_key}"
    return issue_key


def _issue_task(issue: Mapping[str, Any], issue_key: str) -> str:
    summary = _clean_text(issue.get("summary") or issue.get("title") or issue_key) or issue_key
    description = _clean_text(issue.get("description") or issue.get("body"))
    if description and description != summary:
        return f"{summary}\n\n{description}"
    return summary


def _merge_route_sources(config: RoutingConfig, sources: dict[str, RoutingProfile]) -> RoutingProfile:
    merged = RoutingProfile()
    for token in config.precedence:
        key = token.strip().lower()
        if key in {"saved_config", "saved", "config"}:
            source = sources.get("saved_config")
        elif key == "defaults":
            source = sources.get("defaults")
        elif key in {"projects", "project", "project_overrides"}:
            source = sources.get("projects")
        elif key == "rules":
            source = sources.get("rules")
        else:
            source = sources.get(key)
        if source is None:
            continue
        for field_name in ("repository", "base_branch", "test_command", "provider", "model"):
            value = getattr(source, field_name)
            if value is not None:
                setattr(merged, field_name, value)
    for field_name in ("repository", "base_branch", "test_command", "provider", "model"):
        if getattr(merged, field_name) is None:
            for source in (sources.get("rules"), sources.get("projects"), sources.get("defaults"), sources.get("saved_config")):
                if source is None:
                    continue
                value = getattr(source, field_name)
                if value is not None:
                    setattr(merged, field_name, value)
                    break
    return merged


def route_issue_to_run(
    config: SavedConfig,
    issue: Mapping[str, Any],
    *,
    routing_config_path: str | Path | None = None,
) -> AutomationRun:
    routing = load_routing_config(routing_config_path)
    issue_key = _clean_text(issue.get("key") or issue.get("issue_key")) or "UNKNOWN-0"
    issue_key = issue_key.upper()
    selected_rule = _select_rule(issue, routing)
    project_key = _issue_project_key(issue)

    sources = {
        "saved_config": _saved_config_profile(config),
        "defaults": routing.defaults,
        "projects": _resolve_project_profile(project_key, routing),
        "rules": _resolve_rule_profile(selected_rule, routing) if selected_rule is not None else RoutingProfile(),
    }
    merged = _merge_route_sources(routing, sources)

    create_pr_value = issue.get("create_pr")
    if create_pr_value is None:
        create_pr = True
    else:
        create_pr = bool(create_pr_value)

    return AutomationRun(
        run_id=_clean_text(issue.get("run_id") or issue.get("event_id")) or issue_key.lower(),
        issue_key=issue_key,
        issue_url=_default_issue_url(issue, issue_key, config),
        repository=merged.repository or config.repository or "",
        base_branch=merged.base_branch or config.base_branch or "main",
        task=_issue_task(issue, issue_key),
        callback_url=_clean_text(issue.get("callback_url")),
        test_command=merged.test_command,
        provider=merged.provider,
        model=merged.model,
        create_pr=create_pr,
        status=str(issue.get("status") or "queued"),
    )
