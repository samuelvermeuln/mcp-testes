from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from .core import (
    auto_pipeline,
    bootstrap_project,
    detect_project_profile,
    discover_changes,
    enforce_changed_coverage,
    generate_tests_for_changes,
    list_context_states,
    memory_stats,
    query_memory,
    read_agent_file,
    resolve_context_state,
    run_validation,
    start_test_timer,
    stop_test_timer,
    summarize_metrics,
    upsert_memory,
    index_project_memory,
    runtime_settings,
)

mcp = FastMCP("digital-solutions-test-mcp", json_response=True)
WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
DEFAULT_ROUTER_CONTEXT_ROOT = "/data/contexts"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str, fallback: str = "default") -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-").lower()
    return normalized or fallback


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(_request: Request) -> JSONResponse:
    """Lightweight health endpoint for container orchestrators."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "digital-solutions-test-mcp",
        }
    )


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz_check(_request: Request) -> JSONResponse:
    """Alias endpoint for tools that prefer /healthz."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "digital-solutions-test-mcp",
        }
    )


@mcp.custom_route("/", methods=["GET"], include_in_schema=False)
async def root_info(_request: Request) -> JSONResponse:
    """Expose active transport/paths so reverse-proxy checks can discover the MCP endpoint."""
    server_settings = _server_runtime_settings()
    transport = server_settings["transport"]
    recommended_endpoint = (
        server_settings["sse_path"]
        if transport == "sse"
        else server_settings["streamable_http_path"]
        if transport == "streamable-http"
        else None
    )
    return JSONResponse(
        {
            "status": "ok",
            "service": "digital-solutions-test-mcp",
            "transport": transport,
            "recommended_endpoint": recommended_endpoint,
            "endpoints": {
                "health": "/health",
                "healthz": "/healthz",
                "sse": server_settings["sse_path"],
                "messages": server_settings["message_path"],
                "streamable_http": server_settings["streamable_http_path"],
            },
        }
    )


def _normalize_fs_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return raw
    if os.name != "nt" and WINDOWS_PATH_PATTERN.match(raw):
        drive = raw[0].lower()
        remainder = raw[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{remainder}"
    return raw


def _settings_block(config_toml_path: str | None = None) -> dict[str, Any]:
    payload = runtime_settings(project_root=None, config_toml_path=config_toml_path)
    settings = payload.get("settings", {})
    return settings if isinstance(settings, dict) else {}


def _server_runtime_settings(config_toml_path: str | None = None) -> dict[str, Any]:
    settings = _settings_block(config_toml_path=config_toml_path)
    server_settings = settings.get("server", {}) if isinstance(settings, dict) else {}

    transport = (
        os.getenv("DIGITAL_SOLUTIONS_MCP_TRANSPORT", "").strip().lower()
        or str(server_settings.get("transport", "")).strip().lower()
        or "sse"
    )
    host = (
        os.getenv("DIGITAL_SOLUTIONS_MCP_HOST", "").strip()
        or str(server_settings.get("host", "")).strip()
        or "0.0.0.0"
    )
    port_raw = (
        os.getenv("DIGITAL_SOLUTIONS_MCP_PORT", "").strip()
        or str(server_settings.get("port", "")).strip()
        or "8000"
    )
    streamable_http_path = (
        os.getenv("DIGITAL_SOLUTIONS_MCP_PATH", "").strip()
        or str(server_settings.get("streamable_http_path", "")).strip()
        or str(server_settings.get("path", "")).strip()
        or "/mcp"
    )
    sse_path = (
        os.getenv("DIGITAL_SOLUTIONS_MCP_SSE_PATH", "").strip()
        or str(server_settings.get("sse_path", "")).strip()
        or "/sse"
    )
    message_path = (
        os.getenv("DIGITAL_SOLUTIONS_MCP_MESSAGE_PATH", "").strip()
        or str(server_settings.get("message_path", "")).strip()
        or "/messages/"
    )
    stateless_http = _boolish(
        os.getenv("DIGITAL_SOLUTIONS_MCP_STATELESS_HTTP", "").strip() or server_settings.get("stateless_http"),
        default=True,
    )
    json_response = _boolish(
        os.getenv("DIGITAL_SOLUTIONS_MCP_JSON_RESPONSE", "").strip() or server_settings.get("json_response"),
        default=True,
    )

    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"Invalid MCP port value: {port_raw}")

    return {
        "transport": transport,
        "host": host,
        "port": port,
        "streamable_http_path": streamable_http_path or "/mcp",
        "sse_path": sse_path or "/sse",
        "message_path": message_path or "/messages/",
        "stateless_http": stateless_http,
        "json_response": json_response,
    }


def _boolish(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if not normalized:
        return default
    return normalized in {"1", "true", "yes", "on"}


def _split_path_list(raw: str) -> list[str]:
    if not raw:
        return []
    parts = re.split(r"[;\n,]+", raw)
    return [item.strip() for item in parts if item.strip()]


def _resolve_identity(
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, str]:
    settings = _settings_block(config_toml_path=config_toml_path)
    context_settings = settings.get("context", {}) if isinstance(settings, dict) else {}

    resolved_context_id = (
        context_id
        or os.getenv("DIGITAL_SOLUTIONS_CONTEXT_ID", "").strip()
        or str(context_settings.get("context_id", "")).strip()
    )
    resolved_developer_id = (
        developer_id
        or os.getenv("DIGITAL_SOLUTIONS_DEVELOPER_ID", "").strip()
        or str(context_settings.get("developer_id", "")).strip()
        or os.getenv("USERNAME", "").strip()
        or os.getenv("USER", "").strip()
        or "dev"
    )
    resolved_workspace_id = (
        workspace_id
        or os.getenv("DIGITAL_SOLUTIONS_WORKSPACE_ID", "").strip()
        or str(context_settings.get("workspace_id", "")).strip()
        or "default-workspace"
    )
    resolved_context_root = (
        context_root
        or os.getenv("DIGITAL_SOLUTIONS_CONTEXT_ROOT", "").strip()
        or str(context_settings.get("store_root", "")).strip()
        or DEFAULT_ROUTER_CONTEXT_ROOT
    )
    normalized_context_root = _normalize_fs_path(resolved_context_root)

    return {
        "context_id": resolved_context_id,
        "developer_id": resolved_developer_id,
        "workspace_id": resolved_workspace_id,
        "context_root": normalized_context_root,
    }


def _router_state_path(identity: dict[str, str]) -> Path:
    base_root = Path(identity["context_root"]).expanduser().resolve()
    return base_root / "_router" / "active-projects.json"


def _router_fallback_state_path() -> Path:
    return Path.cwd() / ".ai-test-mcp" / "_router" / "active-projects.json"


def _load_router_state(path: Path) -> dict[str, Any]:
    default_state = {
        "schema_version": 1,
        "updated_at_utc": _utc_now_iso(),
        "bindings": {},
    }
    payload: dict[str, Any] = {}
    for candidate in (path, _router_fallback_state_path()):
        if not candidate.exists() or not candidate.is_file():
            continue
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(loaded, dict):
            payload = loaded
            break

    if not payload:
        return default_state

    bindings = payload.get("bindings", {})
    return {
        "schema_version": 1,
        "updated_at_utc": payload.get("updated_at_utc"),
        "bindings": bindings if isinstance(bindings, dict) else {},
    }


def _write_router_state(path: Path, payload: dict[str, Any]) -> None:
    payload["updated_at_utc"] = _utc_now_iso()
    content = json.dumps(payload, indent=2, ensure_ascii=True) + "\n"

    last_error: OSError | None = None
    for candidate in (path, _router_fallback_state_path()):
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            candidate.write_text(content, encoding="utf-8")
            return
        except OSError as exc:
            last_error = exc
            continue

    if last_error is not None:
        raise last_error


def _binding_key(identity: dict[str, str]) -> str:
    if identity.get("context_id"):
        return f"context::{_slug(identity['context_id'], fallback='context')}"
    dev = _slug(identity.get("developer_id", ""), fallback="dev")
    workspace = _slug(identity.get("workspace_id", ""), fallback="workspace")
    return f"dev::{dev}__workspace::{workspace}"


def _get_active_binding(identity: dict[str, str]) -> dict[str, Any] | None:
    state_path = _router_state_path(identity)
    state = _load_router_state(state_path)
    binding = state["bindings"].get(_binding_key(identity))
    if not isinstance(binding, dict):
        return None
    project_root = _normalize_fs_path(str(binding.get("project_root", "")).strip())
    if not project_root:
        return None
    binding["project_root"] = project_root
    return binding


def _set_active_binding(
    identity: dict[str, str],
    project_root: str,
    selected_by: str,
    selection_reason: str,
    intent: str = "",
    variables: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_path = _router_state_path(identity)
    state = _load_router_state(state_path)
    key = _binding_key(identity)
    state["bindings"][key] = {
        "project_root": _normalize_fs_path(project_root),
        "selected_by": selected_by,
        "selection_reason": selection_reason,
        "intent": intent,
        "variables": variables or {},
        "updated_at_utc": _utc_now_iso(),
        "developer_id": identity.get("developer_id"),
        "workspace_id": identity.get("workspace_id"),
        "context_id": identity.get("context_id") or None,
    }
    try:
        _write_router_state(state_path, state)
    except OSError:
        # Keep operation functional even when persistent router cache cannot be written.
        pass
    return state["bindings"][key]


def _clear_active_binding(identity: dict[str, str]) -> bool:
    state_path = _router_state_path(identity)
    state = _load_router_state(state_path)
    key = _binding_key(identity)
    if key not in state["bindings"]:
        return False
    del state["bindings"][key]
    try:
        _write_router_state(state_path, state)
    except OSError:
        pass
    return True


def _router_search_roots(config_toml_path: str | None = None) -> list[Path]:
    settings = _settings_block(config_toml_path=config_toml_path)
    router_settings = settings.get("router", {}) if isinstance(settings, dict) else {}

    raw_roots = (
        os.getenv("DIGITAL_SOLUTIONS_PROJECTS_ROOT", "").strip()
        or str(router_settings.get("projects_root", "")).strip()
    )
    roots: list[Path] = []

    for token in _split_path_list(raw_roots):
        roots.append(Path(_normalize_fs_path(token)).expanduser())

    roots.append(Path("/workspace/projects"))
    roots.append(Path.cwd())

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        try:
            key = str(root.resolve())
        except OSError:
            key = str(root)
        if key in seen:
            continue
        seen.add(key)
        if root.exists() and root.is_dir():
            deduped.append(root.resolve())
    return deduped


def _discover_project_candidates(config_toml_path: str | None = None) -> list[dict[str, Any]]:
    settings = _settings_block(config_toml_path=config_toml_path)
    router_settings = settings.get("router", {}) if isinstance(settings, dict) else {}
    max_candidates = int(router_settings.get("max_candidates", 64) or 64)
    if max_candidates < 1:
        max_candidates = 64

    roots = _router_search_roots(config_toml_path=config_toml_path)
    if not roots:
        return []

    bucket: dict[str, dict[str, Any]] = {}

    def ensure_candidate(path: Path) -> dict[str, Any]:
        normalized = str(path.resolve())
        if normalized not in bucket:
            bucket[normalized] = {
                "project_root": normalized,
                "project_name": path.name,
                "solutions": [],
                "solution_count": 0,
                "csproj_count": 0,
                "score": 0,
            }
        return bucket[normalized]

    for base in roots:
        for sln in base.rglob("*.sln"):
            if not sln.is_file():
                continue
            candidate = ensure_candidate(sln.parent)
            stem = sln.stem
            if stem not in candidate["solutions"]:
                candidate["solutions"].append(stem)
            candidate["solution_count"] += 1

        for csproj in base.rglob("*.csproj"):
            if not csproj.is_file():
                continue
            candidate = ensure_candidate(csproj.parent)
            candidate["csproj_count"] += 1

    if not bucket:
        return []

    for candidate in bucket.values():
        candidate["score"] = candidate["solution_count"] * 100 + candidate["csproj_count"] * 10
        candidate["solutions"] = sorted(candidate["solutions"])

    ranked = sorted(
        bucket.values(),
        key=lambda item: (
            int(item["score"]),
            int(item["solution_count"]),
            int(item["csproj_count"]),
            str(item["project_root"]),
        ),
        reverse=True,
    )
    return ranked[:max_candidates]


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return payload
    except json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            payload = json.loads(text[start : end + 1])
            if isinstance(payload, dict):
                return payload
        except json.JSONDecodeError:
            return None
    return None


def _router_prompt(intent: str, candidates: list[dict[str, Any]]) -> str:
    payload = {
        "intent": intent,
        "projects": [
            {
                "id": idx,
                "name": item["project_name"],
                "path": item["project_root"],
                "solutions": item["solutions"][:3],
                "solution_count": item["solution_count"],
                "csproj_count": item["csproj_count"],
            }
            for idx, item in enumerate(candidates)
        ],
        "response_format": {
            "project_id": "integer",
            "reason": "string",
            "confidence": "float_0_1",
        },
    }
    return json.dumps(payload, ensure_ascii=True)


def _resolve_by_command_router(
    intent: str,
    candidates: list[dict[str, Any]],
    command: str,
) -> dict[str, Any] | None:
    args = shlex.split(command)
    if not args:
        return None
    try:
        proc = subprocess.run(
            args,
            input=_router_prompt(intent, candidates),
            text=True,
            capture_output=True,
            timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return _extract_json_object(proc.stdout)


def _resolve_by_openai_router(
    intent: str,
    candidates: list[dict[str, Any]],
    model: str,
) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    base_url = os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1").strip().rstrip("/")
    endpoint = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 220,
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a deterministic project router. Return only JSON with keys "
                    "project_id, reason, confidence. Choose the best project id from provided list."
                ),
            },
            {
                "role": "user",
                "content": _router_prompt(intent, candidates),
            },
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

    choices = payload.get("choices", [])
    if not isinstance(choices, list) or not choices:
        return None
    message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
    content = message.get("content", "") if isinstance(message, dict) else ""
    if not isinstance(content, str):
        return None
    return _extract_json_object(content)


def _resolve_by_anthropic_router(
    intent: str,
    candidates: list[dict[str, Any]],
    model: str,
) -> dict[str, Any] | None:
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return None

    endpoint = "https://api.anthropic.com/v1/messages"
    body = {
        "model": model,
        "max_tokens": 220,
        "temperature": 0,
        "system": (
            "You are a deterministic project router. Return only JSON with keys "
            "project_id, reason, confidence. Choose the best project id from provided list."
        ),
        "messages": [
            {
                "role": "user",
                "content": _router_prompt(intent, candidates),
            }
        ],
    }
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
        return None

    parts = payload.get("content", [])
    if not isinstance(parts, list):
        return None
    text_blocks = [
        item.get("text", "")
        for item in parts
        if isinstance(item, dict) and str(item.get("type")) == "text"
    ]
    content = "\n".join(text_blocks).strip()
    return _extract_json_object(content)


def _heuristic_project_selection(
    intent: str,
    candidates: list[dict[str, Any]],
) -> tuple[dict[str, Any], str]:
    if len(candidates) == 1:
        return candidates[0], "single_candidate"

    tokens = sorted(set(re.findall(r"[A-Za-z0-9_]{2,}", (intent or "").lower())))
    if not tokens:
        return candidates[0], "default_top_ranked"

    best = candidates[0]
    best_score = -1
    for item in candidates:
        hay = f"{item['project_name']} {item['project_root']} {' '.join(item['solutions'])}".lower()
        score = 0
        for token in tokens:
            if token in hay:
                score += 5 if token in item["project_name"].lower() else 2
        if score > best_score:
            best = item
            best_score = score
    return best, "heuristic_tokens"


def _select_project_with_llm_or_heuristic(
    intent: str,
    candidates: list[dict[str, Any]],
    config_toml_path: str | None = None,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    settings = _settings_block(config_toml_path=config_toml_path)
    router_settings = settings.get("router", {}) if isinstance(settings, dict) else {}

    command = (
        os.getenv("DIGITAL_SOLUTIONS_ROUTER_COMMAND", "").strip()
        or str(router_settings.get("resolver_command", "")).strip()
    )
    provider = (
        os.getenv("DIGITAL_SOLUTIONS_ROUTER_PROVIDER", "").strip().lower()
        or str(router_settings.get("provider", "")).strip().lower()
    )
    model = (
        os.getenv("DIGITAL_SOLUTIONS_ROUTER_MODEL", "").strip()
        or str(router_settings.get("model", "")).strip()
    )
    prefer_llm = _boolish(
        os.getenv("DIGITAL_SOLUTIONS_ROUTER_PREFER_LLM", "").strip() or router_settings.get("prefer_llm"),
        default=True,
    )
    diagnostics: dict[str, Any] = {
        "prefer_llm": prefer_llm,
        "provider": provider or None,
        "model": model or None,
        "command_configured": bool(command),
    }

    llm_payload: dict[str, Any] | None = None
    if prefer_llm:
        if command:
            llm_payload = _resolve_by_command_router(intent=intent, candidates=candidates, command=command)
            diagnostics["used_resolver"] = "command" if llm_payload else "command_failed"
        elif provider == "openai" and model:
            llm_payload = _resolve_by_openai_router(intent=intent, candidates=candidates, model=model)
            diagnostics["used_resolver"] = "openai" if llm_payload else "openai_failed"
        elif provider == "anthropic" and model:
            llm_payload = _resolve_by_anthropic_router(intent=intent, candidates=candidates, model=model)
            diagnostics["used_resolver"] = "anthropic" if llm_payload else "anthropic_failed"
        else:
            diagnostics["used_resolver"] = "none_configured"

    if isinstance(llm_payload, dict):
        project_idx = llm_payload.get("project_id")
        try:
            index = int(project_idx)
        except (TypeError, ValueError):
            index = -1
        if 0 <= index < len(candidates):
            selected = candidates[index]
            reason = str(llm_payload.get("reason", "")).strip() or "llm_selected"
            diagnostics["llm_confidence"] = llm_payload.get("confidence")
            diagnostics["llm_reason"] = reason
            return selected, "llm", diagnostics

        project_path = _normalize_fs_path(str(llm_payload.get("project_path", "")).strip())
        if project_path:
            for candidate in candidates:
                if _normalize_fs_path(candidate["project_root"]) == project_path:
                    diagnostics["llm_confidence"] = llm_payload.get("confidence")
                    diagnostics["llm_reason"] = llm_payload.get("reason")
                    return candidate, "llm_path", diagnostics

    selected, method = _heuristic_project_selection(intent=intent, candidates=candidates)
    diagnostics["fallback_method"] = method
    return selected, method, diagnostics


def _ensure_context_materialized(
    project_root: str,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    resolved = resolve_context_state(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    state_dir = Path(resolved["state_dir_absolute"])

    required_files = [
        state_dir / "context.json",
        state_dir / "variables.json",
        state_dir / "project-profile.json",
    ]
    missing = [str(path) for path in required_files if not path.exists()]

    bootstrap_result: dict[str, Any] | None = None
    if missing:
        bootstrap_result = bootstrap_project(
            project_root=project_root,
            overwrite_agents=False,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )

    stats = memory_stats(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    reindexed = None
    if int(stats.get("chunks", 0)) <= 0:
        reindexed = index_project_memory(
            project_root=project_root,
            include_agents=True,
            include_metrics=True,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        stats = memory_stats(
            project_root=project_root,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )

    return {
        "context_state": resolved,
        "missing_state_files": missing,
        "bootstrapped": bootstrap_result is not None,
        "bootstrap": bootstrap_result,
        "rag_reindexed": reindexed is not None,
        "rag_index_result": reindexed,
        "rag_stats": stats,
    }


def _resolve_project_root(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> str:
    if project_root and project_root.strip():
        resolved = _normalize_fs_path(project_root)
        identity = _resolve_identity(
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        _set_active_binding(
            identity=identity,
            project_root=resolved,
            selected_by="manual_argument",
            selection_reason="project_root argument provided",
            variables={},
        )
        return resolved

    env_project_root = os.getenv("DIGITAL_SOLUTIONS_PROJECT_ROOT", "").strip()
    if env_project_root:
        resolved = _normalize_fs_path(env_project_root)
        identity = _resolve_identity(
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        _set_active_binding(
            identity=identity,
            project_root=resolved,
            selected_by="env",
            selection_reason="DIGITAL_SOLUTIONS_PROJECT_ROOT",
            variables={},
        )
        return resolved

    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    active_binding = _get_active_binding(identity)
    if active_binding:
        active_root = _normalize_fs_path(str(active_binding.get("project_root", "")).strip())
        if active_root and Path(active_root).exists():
            return active_root

    settings_payload = runtime_settings(project_root=None, config_toml_path=config_toml_path)
    project_settings = settings_payload.get("settings", {}).get("project", {})
    config_project_root = str(project_settings.get("project_root", "")).strip()
    if config_project_root:
        resolved = _normalize_fs_path(config_project_root)
        _set_active_binding(
            identity=identity,
            project_root=resolved,
            selected_by="config_toml",
            selection_reason="[project].project_root",
            variables={},
        )
        return resolved

    auto_detected = _auto_detect_project_root(config_toml_path=config_toml_path)
    if auto_detected:
        _set_active_binding(
            identity=identity,
            project_root=auto_detected,
            selected_by="auto_detect",
            selection_reason="automatic project discovery",
            variables={},
        )
        return auto_detected

    raise ValueError(
        "project_root was not provided. Set project_root argument, or set "
        "DIGITAL_SOLUTIONS_PROJECT_ROOT, or define [project].project_root in config.toml. "
        "If running on server, place project(s) under /workspace/projects so auto-detection can resolve. "
        "When multiple projects exist, call route_project once with an intent so the MCP can cache selection."
    )


def _auto_detect_project_root(config_toml_path: str | None = None) -> str | None:
    candidates = _discover_project_candidates(config_toml_path=config_toml_path)
    if not candidates:
        return None

    return str(candidates[0]["project_root"])


@mcp.tool()
def detect_project(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Detect .NET solution/test projects and infer required variables for test automation."""
    return detect_project_profile(
        _resolve_project_root(
            project_root=project_root,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
    )


@mcp.tool()
def route_project(
    intent: str = "",
    project_root: str | None = None,
    force_reselect: bool = False,
    ensure_context: bool = True,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """
    Resolve and cache active project per context/developer/workspace.
    If multiple projects are available, can use LLM router (or heuristic fallback).
    """
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    cached = _get_active_binding(identity)
    switched = False
    selected_by = "cached"
    selection_reason = "using cached active project"
    diagnostics: dict[str, Any] = {}

    if project_root and project_root.strip():
        resolved_root = _normalize_fs_path(project_root)
        selected_by = "manual"
        selection_reason = "manual project_root provided"
    else:
        resolved_root = ""
        if not force_reselect and cached:
            cached_root = _normalize_fs_path(str(cached.get("project_root", "")).strip())
            if cached_root and Path(cached_root).exists():
                resolved_root = cached_root
                selection_reason = str(cached.get("selection_reason", selection_reason))
                selected_by = "cached"
            else:
                _clear_active_binding(identity)
                diagnostics["stale_binding_cleared"] = True

        if not resolved_root:
            candidates = _discover_project_candidates(config_toml_path=config_toml_path)
            if not candidates:
                raise ValueError(
                    "No .NET projects found. Mount projects under /workspace/projects or set "
                    "DIGITAL_SOLUTIONS_PROJECTS_ROOT."
                )

            selected, method, resolver_diag = _select_project_with_llm_or_heuristic(
                intent=intent,
                candidates=candidates,
                config_toml_path=config_toml_path,
            )
            resolved_root = _normalize_fs_path(str(selected["project_root"]))
            selected_by = method
            selection_reason = f"selected by {method}"
            diagnostics.update(resolver_diag)
            diagnostics["candidate_count"] = len(candidates)
            diagnostics["selected_project_name"] = selected["project_name"]

    binding = _set_active_binding(
        identity=identity,
        project_root=resolved_root,
        selected_by=selected_by,
        selection_reason=selection_reason,
        intent=intent,
        variables={
            "context_id": identity["context_id"] or None,
            "developer_id": identity["developer_id"],
            "workspace_id": identity["workspace_id"],
        },
    )

    if cached and _normalize_fs_path(str(cached.get("project_root", ""))) != resolved_root:
        switched = True
    if (not cached) and selected_by != "cached":
        switched = True

    context_payload = None
    if ensure_context:
        context_payload = _ensure_context_materialized(
            project_root=resolved_root,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )

    return {
        "status": "ok",
        "project_root": resolved_root,
        "selected_by": selected_by,
        "selection_reason": selection_reason,
        "switched": switched,
        "binding": binding,
        "ensure_context": ensure_context,
        "context_materialized": context_payload,
        "diagnostics": diagnostics,
        "generated_at_utc": _utc_now_iso(),
    }


@mcp.tool()
def get_active_project(
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Get active project bound to context/developer/workspace identity."""
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    binding = _get_active_binding(identity)
    if not binding:
        return {
            "status": "ok",
            "found": False,
            "identity": identity,
            "message": "No active project binding found for this identity.",
        }

    root = _normalize_fs_path(str(binding.get("project_root", "")))
    exists = bool(root and Path(root).exists())
    return {
        "status": "ok",
        "found": True,
        "identity": identity,
        "project_root": root,
        "project_exists": exists,
        "binding": binding,
    }


@mcp.tool()
def clear_active_project(
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Clear active project binding for one identity."""
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    removed = _clear_active_binding(identity)
    return {
        "status": "ok",
        "removed": removed,
        "identity": identity,
        "generated_at_utc": _utc_now_iso(),
    }


@mcp.tool()
def bootstrap(
    project_root: str | None = None,
    overwrite_agents: bool = False,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Initialize .ai-test-mcp state in the target project and copy agent assets/templates."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return bootstrap_project(
        project_root=resolved_root,
        overwrite_agents=overwrite_agents,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def bootstrap_with_context(
    project_root: str | None = None,
    overwrite_agents: bool = False,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Initialize project context using TOML/env context settings for multi-window and multi-developer isolation."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return bootstrap_project(
        project_root=resolved_root,
        overwrite_agents=overwrite_agents,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def discover_test_targets(
    project_root: str | None = None,
    base_ref: str = "HEAD~1",
    include_untracked: bool = True,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Inspect changed C# source files and map candidate classes/methods that need tests."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return discover_changes(
        project_root=resolved_root,
        base_ref=base_ref,
        include_untracked=include_untracked,
    )


@mcp.tool()
def generate_tests(
    project_root: str | None = None,
    base_ref: str = "HEAD~1",
    dry_run: bool = False,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Generate baseline xUnit tests for changed public classes/methods."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return generate_tests_for_changes(
        project_root=resolved_root,
        base_ref=base_ref,
        dry_run=dry_run,
    )


@mcp.tool()
def validate(
    project_root: str | None = None,
    run_coverage: bool = True,
    configuration: str = "Debug",
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Run dotnet build/test and optional coverage collection for the project."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return run_validation(
        project_root=resolved_root,
        run_coverage=run_coverage,
        configuration=configuration,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def coverage_gate(
    project_root: str | None = None,
    base_ref: str = "HEAD~1",
    min_line_rate: float = 1.0,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Fail when changed files are below minimum line coverage."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return enforce_changed_coverage(
        project_root=resolved_root,
        base_ref=base_ref,
        min_line_rate=min_line_rate,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def pipeline(
    project_root: str | None = None,
    base_ref: str = "HEAD~1",
    min_line_rate: float = 1.0,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Run bootstrap + discovery + generation + coverage gate in a single call."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return auto_pipeline(
        project_root=resolved_root,
        base_ref=base_ref,
        min_line_rate=min_line_rate,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def start_timer(
    test_case_id: str,
    feature: str,
    test_name: str,
    complexity: str,
    test_type: str,
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Start per-test timer for productivity metrics."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return start_test_timer(
        project_root=resolved_root,
        test_case_id=test_case_id,
        feature=feature,
        test_name=test_name,
        complexity=complexity,
        test_type=test_type,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def stop_timer(
    test_case_id: str,
    project_root: str | None = None,
    status: str = "PASS",
    notes: str = "",
    baseline_manual_minutes: int | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Stop per-test timer, compute savings, and append row to metrics log."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return stop_test_timer(
        project_root=resolved_root,
        test_case_id=test_case_id,
        status=status,
        notes=notes,
        baseline_manual_minutes=baseline_manual_minutes,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def metrics_summary(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Summarize accumulated productivity and savings metrics."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return summarize_metrics(
        project_root=resolved_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def resolve_context(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Resolve and create context directory for project/developer/workspace isolation."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return resolve_context_state(
        project_root=resolved_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def get_runtime_settings(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Load effective TOML runtime settings used by the MCP server."""
    resolved_root = None
    if project_root is not None or _get_active_binding(
        _resolve_identity(
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
    ):
        resolved_root = _resolve_project_root(
            project_root=project_root,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
    return runtime_settings(project_root=resolved_root, config_toml_path=config_toml_path)


@mcp.tool()
def list_contexts(
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """List isolated contexts currently available under context_root."""
    return list_context_states(context_root=context_root, config_toml_path=config_toml_path)


@mcp.tool()
def rag_index_context(
    project_root: str | None = None,
    include_agents: bool = True,
    include_metrics: bool = True,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Index project/context state into local RAG memory for low-token long-context retrieval."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return index_project_memory(
        project_root=resolved_root,
        include_agents=include_agents,
        include_metrics=include_metrics,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def rag_upsert_note(
    source: str,
    content: str,
    metadata_json: str = "{}",
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Store or replace a memory source in local SQLite RAG store (isolated by project/context)."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    try:
        metadata = json.loads(metadata_json or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid metadata_json: {exc.msg}")
    return upsert_memory(
        project_root=resolved_root,
        source=source,
        content=content,
        metadata=metadata if isinstance(metadata, dict) else {"value": metadata},
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def rag_query(
    query: str,
    max_chunks: int | None = None,
    max_chars: int | None = None,
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Retrieve compact context via local RAG to minimize token usage in external LLMs."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return query_memory(
        project_root=resolved_root,
        query=query,
        max_chunks=max_chunks,
        max_chars=max_chars,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def rag_stats(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Get local RAG memory footprint and token estimate for the active project/context."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return memory_stats(
        project_root=resolved_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )


@mcp.tool()
def list_agent_files() -> dict[str, Any]:
    """List available agent markdown files packaged with this MCP server."""
    agents_dir = Path(__file__).resolve().parents[2] / "assets" / "Agents.Testing"
    files = sorted([p.name for p in agents_dir.glob("*.md") if p.is_file()])
    return {
        "status": "ok",
        "count": len(files),
        "files": files,
    }


@mcp.tool()
def get_agent_file(file_name: str) -> dict[str, Any]:
    """Read one packaged agent markdown file by name."""
    return read_agent_file(file_name)


@mcp.resource("agent://{file_name}")
def agent_resource(file_name: str) -> str:
    """Expose agent markdown files as MCP resources."""
    return read_agent_file(file_name)["content"]


def main() -> None:
    server_settings = _server_runtime_settings()
    transport = server_settings["transport"]
    if transport in {"", "stdio"}:
        mcp.run()
        return

    # FastMCP runtime settings are read from mcp.settings for HTTP transports.
    mcp.settings.host = server_settings["host"]
    mcp.settings.port = server_settings["port"]
    mcp.settings.streamable_http_path = server_settings["streamable_http_path"]
    mcp.settings.sse_path = server_settings["sse_path"]
    mcp.settings.message_path = server_settings["message_path"]
    mcp.settings.stateless_http = server_settings["stateless_http"]
    mcp.settings.json_response = server_settings["json_response"]

    if transport == "sse":
        mcp.run(transport="sse")
        return

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
