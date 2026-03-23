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
from mcp.server.fastmcp.server import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .core import (
    auto_pipeline,
    bootstrap_project,
    detect_project_profile,
    discover_changes,
    enforce_changed_coverage,
    generate_tests_for_changes,
    get_agents_assets_dir,
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

SERVER_INSTRUCTIONS = """
This MCP is optimized for remote multi-developer test orchestration.

Default behavior:
- Prefer context_only workflow when the repository is not visible on the MCP server.
- Do not ask the developer to mount the repository as the first response.
- Do not ask broad open-ended questions when the next tool call is already known.

When execution_mode is context_only:
1. Call route_project.
2. Call bootstrap_with_context or ingest_project_snapshot with project_manifest_json, source_snapshot_json, file_tree, and concise notes.
3. Call prepare_test_generation_context.
4. Use prompt_package so the external LLM writes the tests locally in the developer workspace.

Only use discover_test_targets, generate_tests, validate, coverage_gate, or pipeline when execution_mode is server_execution or when the user explicitly wants server-side execution.

If context is incomplete, ask only for the exact missing class, method, file tree, or source snapshot. Do not ask for Docker mounts unless the user wants server_execution.
""".strip()

mcp = FastMCP(
    "digital-solutions-test-mcp",
    instructions=SERVER_INSTRUCTIONS,
    json_response=True,
)
WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
DEFAULT_ROUTER_CONTEXT_ROOT = "/data/contexts"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _slug(value: str, fallback: str = "default") -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-").lower()
    return normalized or fallback


def _context_only_steps() -> list[dict[str, str]]:
    return [
        {
            "tool": "route_project",
            "why": "bind the active logical project for this developer/workspace",
        },
        {
            "tool": "bootstrap_with_context",
            "why": "initialize agents, state and memory while ingesting project manifest and source snapshots",
        },
        {
            "tool": "ingest_project_snapshot",
            "why": "refresh or append file/class/method snapshots after code changes",
        },
        {
            "tool": "prepare_test_generation_context",
            "why": "produce a compact prompt_package for the external LLM to write tests locally",
        },
    ]


def _server_execution_steps() -> list[dict[str, str]]:
    return [
        {
            "tool": "route_project",
            "why": "bind the active mounted project on the server",
        },
        {
            "tool": "discover_test_targets",
            "why": "inspect changed source files directly on the server filesystem",
        },
        {
            "tool": "generate_tests",
            "why": "generate baseline tests directly in the mounted repository",
        },
        {
            "tool": "validate",
            "why": "run build, tests and optional coverage on the server",
        },
    ]


def _workflow_guidance_payload(server_files_available: bool) -> dict[str, Any]:
    if server_files_available:
        return {
            "preferred_workflow": "server_execution",
            "summary": (
                "The repository is visible to the MCP server. Server-side execution tools may be used."
            ),
            "next_actions": _server_execution_steps(),
            "prompt_name": "server_execution_workflow",
            "resource_uri": "usage://workflow",
        }
    return {
        "preferred_workflow": "context_only",
        "summary": (
            "Preferred flow is remote context-only: ingest snapshots and let the external LLM write tests locally."
        ),
        "next_actions": _context_only_steps(),
        "prompt_name": "context_only_workflow",
        "resource_uri": "usage://workflow",
    }


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
            "instructions_summary": "For remote usage, prefer snapshot/context workflow instead of asking for repository mounts.",
            "transport": transport,
            "recommended_endpoint": recommended_endpoint,
            "security": {
                "dns_rebinding_protection": server_settings["transport_security"].enable_dns_rebinding_protection,
                "allowed_hosts": server_settings["transport_security"].allowed_hosts,
                "allowed_origins": server_settings["transport_security"].allowed_origins,
            },
            "endpoints": {
                "health": "/health",
                "healthz": "/healthz",
                "sse": server_settings["sse_path"],
                "messages": server_settings["message_path"],
                "streamable_http": server_settings["streamable_http_path"],
            },
            "workflow": {
                "resource_uri": "usage://workflow",
                "prompts": ["context_only_workflow", "server_execution_workflow"],
                "default_remote_sequence": [
                    "route_project",
                    "bootstrap_with_context",
                    "prepare_test_generation_context",
                ],
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
    security_settings = server_settings.get("security", {}) if isinstance(server_settings, dict) else {}

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
    enable_dns_rebinding_protection = _boolish(
        os.getenv("DIGITAL_SOLUTIONS_MCP_ENABLE_DNS_REBINDING_PROTECTION", "").strip()
        or security_settings.get("enable_dns_rebinding_protection"),
        default=False,
    )
    allowed_hosts = _coerce_string_list(
        os.getenv("DIGITAL_SOLUTIONS_MCP_ALLOWED_HOSTS", "").strip() or security_settings.get("allowed_hosts"),
        default=["*"],
    )
    allowed_origins = _coerce_string_list(
        os.getenv("DIGITAL_SOLUTIONS_MCP_ALLOWED_ORIGINS", "").strip() or security_settings.get("allowed_origins"),
        default=["*"],
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
        "transport_security": TransportSecuritySettings(
            enable_dns_rebinding_protection=enable_dns_rebinding_protection,
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
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


def _coerce_string_list(value: Any, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item).strip() for item in value if str(item).strip()]
    return _split_path_list(str(value))


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


def _binding_variables(binding: dict[str, Any] | None) -> dict[str, Any]:
    if isinstance(binding, dict) and isinstance(binding.get("variables"), dict):
        return dict(binding["variables"])
    return {}


def _is_virtual_project_root(project_root: str) -> bool:
    root = Path(_normalize_fs_path(project_root)).resolve()
    metadata_path = root / ".ai-test-mcp" / "project-reference.json"
    if not metadata_path.exists() or not metadata_path.is_file():
        return False
    try:
        payload = json.loads(metadata_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(payload.get("virtual_project"))


def _project_binding_details(identity: dict[str, str], project_root: str, requested_project_root: str | None = None) -> dict[str, Any]:
    binding = _get_active_binding(identity)
    variables = _binding_variables(binding)
    server_files_available = variables.get("server_files_available")
    if server_files_available is None:
        server_files_available = not _is_virtual_project_root(project_root)
    resolution = str(variables.get("resolution", "direct" if server_files_available else "virtual")).strip()
    requested = str(variables.get("requested_project_root", "")).strip() or requested_project_root or project_root
    return {
        "resolution": resolution,
        "server_files_available": bool(server_files_available),
        "requested_project_root": _normalize_fs_path(str(requested)),
        "execution_mode": "server_execution" if server_files_available else "context_only",
    }


def _project_hint_name(reference: str) -> str:
    normalized = _normalize_fs_path(reference).rstrip("/\\")
    if not normalized:
        return "project"
    name = Path(normalized).name.strip()
    if name:
        return name
    fallback = re.sub(r"^[A-Za-z]:", "", normalized).strip("/\\")
    return fallback or "project"


def _virtual_projects_root(identity: dict[str, str]) -> Path:
    return Path(identity["context_root"]).expanduser().resolve() / "_projects"


def _virtual_projects_fallback_root() -> Path:
    return Path.cwd().resolve() / ".ai-test-mcp" / "_projects"


def _ensure_virtual_project_root(
    project_hint: str,
    identity: dict[str, str],
    config_toml_path: str | None = None,
    original_reference: str | None = None,
) -> str:
    project_name = _project_hint_name(project_hint)
    root: Path | None = None
    storage_mode = "context_root"
    last_error: OSError | None = None

    for base_root, mode in (
        (_virtual_projects_root(identity), "context_root"),
        (_virtual_projects_fallback_root(), "cwd_fallback"),
    ):
        candidate_root = base_root / _slug(project_name, fallback="project")
        try:
            candidate_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            last_error = exc
            continue
        root = candidate_root
        storage_mode = mode
        break

    if root is None:
        if last_error is not None:
            raise last_error
        raise OSError("Unable to create a virtual project root.")

    metadata_path = root / ".ai-test-mcp" / "project-reference.json"
    metadata_path.parent.mkdir(parents=True, exist_ok=True)
    metadata = {
        "project_name": project_name,
        "project_root": str(root),
        "virtual_project": True,
        "storage_mode": storage_mode,
        "original_reference": original_reference or project_hint,
        "developer_id": identity.get("developer_id"),
        "workspace_id": identity.get("workspace_id"),
        "context_id": identity.get("context_id") or None,
        "config_toml_path": config_toml_path,
        "updated_at_utc": _utc_now_iso(),
    }
    metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    return str(root)


def _resolve_project_root_from_candidates(
    requested_root: str,
    config_toml_path: str | None = None,
) -> str | None:
    normalized = _normalize_fs_path(requested_root)
    if not normalized:
        return None

    normalized_path = Path(normalized)
    if normalized_path.exists():
        return str(normalized_path.resolve())

    candidates = _discover_project_candidates(config_toml_path=config_toml_path)
    if not candidates:
        return None

    normalized_parts = [part.lower() for part in normalized_path.parts if part]
    normalized_name = normalized_path.name.lower()

    exact_name_matches = [item for item in candidates if str(item["project_name"]).lower() == normalized_name]
    if len(exact_name_matches) == 1:
        return str(exact_name_matches[0]["project_root"])

    suffix_matches: list[dict[str, Any]] = []
    for item in candidates:
        candidate_parts = [part.lower() for part in Path(str(item["project_root"])).parts if part]
        if normalized_parts and len(candidate_parts) >= len(normalized_parts):
            if candidate_parts[-len(normalized_parts) :] == normalized_parts:
                suffix_matches.append(item)

    if len(suffix_matches) == 1:
        return str(suffix_matches[0]["project_root"])

    return None


def _missing_project_root_message(requested_root: str, config_toml_path: str | None = None) -> str:
    candidates = _discover_project_candidates(config_toml_path=config_toml_path)
    discovered_names = ", ".join(sorted(str(item["project_name"]) for item in candidates[:8]))
    discovered_hint = f" Visible server projects: {discovered_names}." if discovered_names else ""
    return (
        f"Project root not found on the MCP server: {_normalize_fs_path(requested_root)}. "
        "This remote server cannot read the developer local filesystem directly. "
        "Preferred remote flow: call route_project, then bootstrap_with_context or ingest_project_snapshot, "
        "then prepare_test_generation_context so the external LLM can write tests locally in the developer workspace. "
        "Only if you want server-side execution should you mount or sync the project into /workspace/projects on the server, "
        "or call route_project to select one of the projects already visible to the container."
        f"{discovered_hint}"
    )


def _resolve_project_reference(
    reference: str,
    identity: dict[str, str],
    config_toml_path: str | None = None,
    require_server_files: bool = False,
) -> dict[str, Any]:
    normalized = _normalize_fs_path(reference)
    candidate = Path(normalized)
    if candidate.exists():
        return {
            "project_root": str(candidate.resolve()),
            "resolution": "direct",
            "server_files_available": True,
            "requested_project_root": normalized,
        }

    remapped = _resolve_project_root_from_candidates(normalized, config_toml_path=config_toml_path)
    if remapped:
        return {
            "project_root": remapped,
            "resolution": "remapped",
            "server_files_available": True,
            "requested_project_root": normalized,
        }

    if require_server_files:
        raise FileNotFoundError(_missing_project_root_message(normalized, config_toml_path=config_toml_path))

    virtual_root = _ensure_virtual_project_root(
        project_hint=normalized,
        identity=identity,
        config_toml_path=config_toml_path,
        original_reference=normalized,
    )
    return {
        "project_root": virtual_root,
        "resolution": "virtual",
        "server_files_available": False,
        "requested_project_root": normalized,
    }


def _resolve_project_root(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
    require_server_files: bool = False,
) -> str:
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    if project_root and project_root.strip():
        resolved_payload = _resolve_project_reference(
            reference=project_root,
            identity=identity,
            config_toml_path=config_toml_path,
            require_server_files=require_server_files,
        )
        resolved = str(resolved_payload["project_root"])
        _set_active_binding(
            identity=identity,
            project_root=resolved,
            selected_by="manual_argument",
            selection_reason="project_root argument provided",
            variables={
                "resolution": resolved_payload["resolution"],
                "server_files_available": resolved_payload["server_files_available"],
                "requested_project_root": resolved_payload["requested_project_root"],
            },
        )
        return resolved

    env_project_root = os.getenv("DIGITAL_SOLUTIONS_PROJECT_ROOT", "").strip()
    if env_project_root:
        resolved_payload = _resolve_project_reference(
            reference=env_project_root,
            identity=identity,
            config_toml_path=config_toml_path,
            require_server_files=require_server_files,
        )
        resolved = str(resolved_payload["project_root"])
        _set_active_binding(
            identity=identity,
            project_root=resolved,
            selected_by="env",
            selection_reason="DIGITAL_SOLUTIONS_PROJECT_ROOT",
            variables={
                "resolution": resolved_payload["resolution"],
                "server_files_available": resolved_payload["server_files_available"],
                "requested_project_root": resolved_payload["requested_project_root"],
            },
        )
        return resolved

    active_binding = _get_active_binding(identity)
    if active_binding:
        active_root = _normalize_fs_path(str(active_binding.get("project_root", "")).strip())
        variables = _binding_variables(active_binding)
        server_files_available = variables.get("server_files_available")
        if server_files_available is None and active_root:
            server_files_available = not _is_virtual_project_root(active_root)
        requested_project_root = str(variables.get("requested_project_root", "")).strip() or active_root

        if require_server_files and server_files_available is False:
            resolved_payload = _resolve_project_reference(
                reference=requested_project_root,
                identity=identity,
                config_toml_path=config_toml_path,
                require_server_files=True,
            )
            return str(resolved_payload["project_root"])

        if active_root and Path(active_root).exists():
            return str(Path(active_root).resolve())

    settings_payload = runtime_settings(project_root=None, config_toml_path=config_toml_path)
    project_settings = settings_payload.get("settings", {}).get("project", {})
    config_project_root = str(project_settings.get("project_root", "")).strip()
    if config_project_root:
        resolved_payload = _resolve_project_reference(
            reference=config_project_root,
            identity=identity,
            config_toml_path=config_toml_path,
            require_server_files=require_server_files,
        )
        resolved = str(resolved_payload["project_root"])
        _set_active_binding(
            identity=identity,
            project_root=resolved,
            selected_by="config_toml",
            selection_reason="[project].project_root",
            variables={
                "resolution": resolved_payload["resolution"],
                "server_files_available": resolved_payload["server_files_available"],
                "requested_project_root": resolved_payload["requested_project_root"],
            },
        )
        return resolved

    auto_detected = _auto_detect_project_root(config_toml_path=config_toml_path)
    if auto_detected:
        _set_active_binding(
            identity=identity,
            project_root=auto_detected,
            selected_by="auto_detect",
            selection_reason="automatic project discovery",
            variables={
                "resolution": "auto_detect",
                "server_files_available": True,
                "requested_project_root": auto_detected,
            },
        )
        return auto_detected

    if require_server_files:
        raise ValueError(
            "project_root was not provided. Set project_root argument, or set "
            "DIGITAL_SOLUTIONS_PROJECT_ROOT, or define [project].project_root in config.toml. "
            "For remote context-only usage, call route_project, then bootstrap_with_context or "
            "ingest_project_snapshot, then prepare_test_generation_context so the external LLM can write tests "
            "locally in the developer workspace. If you specifically want server-side execution tools, place "
            "project(s) under /workspace/projects so auto-detection can resolve. When multiple projects exist, "
            "call route_project once with an intent so the MCP can cache selection."
        )

    logical_root = _ensure_virtual_project_root(
        project_hint=identity.get("context_id") or identity.get("workspace_id") or "remote-project",
        identity=identity,
        config_toml_path=config_toml_path,
        original_reference=identity.get("context_id") or identity.get("workspace_id") or "remote-project",
    )
    _set_active_binding(
        identity=identity,
        project_root=logical_root,
        selected_by="virtual_default",
        selection_reason="created logical project context because no server-side projects are visible",
        variables={
            "resolution": "virtual_default",
            "server_files_available": False,
            "requested_project_root": identity.get("context_id") or identity.get("workspace_id") or "remote-project",
        },
    )
    return logical_root


def _auto_detect_project_root(config_toml_path: str | None = None) -> str | None:
    candidates = _discover_project_candidates(config_toml_path=config_toml_path)
    if not candidates:
        return None

    return str(candidates[0]["project_root"])


def _read_json_file(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists() or not path.is_file():
        return dict(default or {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return dict(default or {})
    return payload if isinstance(payload, dict) else dict(default or {})


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _parse_json_payload(field_name: str, raw: str, empty_default: Any) -> Any:
    if not str(raw or "").strip():
        return empty_default
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid {field_name}: {exc.msg}")


def _coerce_int_mapping(value: Any, default: dict[str, int]) -> dict[str, int]:
    if not isinstance(value, dict):
        return dict(default)
    payload: dict[str, int] = {}
    for key, raw in value.items():
        try:
            payload[str(key)] = int(raw)
        except (TypeError, ValueError):
            continue
    return payload or dict(default)


def _context_state_files(
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
    return {
        "state": resolved,
        "state_dir": state_dir,
        "profile_path": state_dir / "project-profile.json",
        "variables_path": state_dir / "variables.json",
        "context_path": state_dir / "context.json",
    }


def _merge_project_manifest(
    current_profile: dict[str, Any],
    manifest: dict[str, Any],
    project_root: str,
) -> dict[str, Any]:
    profile = dict(current_profile)
    profile["project_root"] = project_root
    profile["project_name"] = (
        str(manifest.get("project_name", "")).strip()
        or str(profile.get("project_name", "")).strip()
        or Path(project_root).name
    )

    solution_path = str(manifest.get("solution_path", "")).strip() or profile.get("solution_path")
    all_solutions = _coerce_string_list(manifest.get("all_solutions"), default=_coerce_string_list(profile.get("all_solutions")))
    if solution_path and solution_path not in all_solutions:
        all_solutions.insert(0, solution_path)
    profile["solution_path"] = solution_path or None
    profile["all_solutions"] = all_solutions

    test_projects = _coerce_string_list(manifest.get("test_projects"), default=_coerce_string_list(profile.get("test_projects")))
    explicit_test_project = str(
        manifest.get("test_project_path", "") or manifest.get("default_test_project", "")
    ).strip()
    if explicit_test_project and explicit_test_project not in test_projects:
        test_projects.insert(0, explicit_test_project)
    profile["test_projects"] = test_projects
    profile["default_test_project"] = explicit_test_project or (test_projects[0] if test_projects else None)

    app_projects = _coerce_string_list(manifest.get("app_projects"), default=_coerce_string_list(profile.get("app_projects")))
    profile["app_projects"] = app_projects

    test_frameworks = _coerce_string_list(
        manifest.get("test_frameworks"),
        default=_coerce_string_list(profile.get("test_frameworks")),
    )
    explicit_framework = str(manifest.get("test_framework", "")).strip()
    if explicit_framework and explicit_framework not in test_frameworks:
        test_frameworks.insert(0, explicit_framework)
    profile["test_frameworks"] = test_frameworks

    target_frameworks = _coerce_string_list(
        manifest.get("target_frameworks"),
        default=_coerce_string_list(profile.get("target_frameworks")),
    )
    explicit_target = str(manifest.get("dotnet_version", "")).strip()
    if explicit_target and explicit_target not in target_frameworks:
        target_frameworks.insert(0, explicit_target)
    profile["target_frameworks"] = target_frameworks

    coverage_candidates = _coerce_string_list(
        manifest.get("coverage_settings_candidates"),
        default=_coerce_string_list(profile.get("coverage_settings_candidates")),
    )
    explicit_coverage = str(
        manifest.get("coverage_settings_path", "") or manifest.get("default_coverage_settings", "")
    ).strip()
    if explicit_coverage and explicit_coverage not in coverage_candidates:
        coverage_candidates.insert(0, explicit_coverage)
    profile["coverage_settings_candidates"] = coverage_candidates
    profile["default_coverage_settings"] = explicit_coverage or (
        coverage_candidates[0] if coverage_candidates else None
    )

    profile["coverage_targets"] = _coerce_int_mapping(
        manifest.get("coverage_targets"),
        default=_coerce_int_mapping(profile.get("coverage_targets"), {"line": 100, "branch": 100}),
    )
    profile["metrics_baseline_minutes"] = _coerce_int_mapping(
        manifest.get("metrics_baseline_minutes"),
        default=_coerce_int_mapping(profile.get("metrics_baseline_minutes"), {"S": 20, "M": 45, "L": 90}),
    )
    profile["server_project_files_found"] = bool(
        profile.get("all_solutions") or profile.get("test_projects") or profile.get("app_projects")
    )
    profile["virtual_project"] = bool(profile.get("virtual_project", False))
    profile["original_reference"] = profile.get("original_reference")
    profile["context_bootstrap_mode"] = "manual_context"
    profile["generated_at_utc"] = _utc_now_iso()
    return profile


def _merge_variables(
    current_variables: dict[str, Any],
    profile: dict[str, Any],
) -> dict[str, Any]:
    variables = dict(current_variables)
    variables["PROJECT_NAME"] = profile.get("project_name")
    variables["SOLUTION_PATH"] = profile.get("solution_path")
    variables["TEST_PROJECT_PATH"] = profile.get("default_test_project")
    variables["TEST_FRAMEWORK"] = (
        profile.get("test_frameworks", ["unknown"])[0] if profile.get("test_frameworks") else "unknown"
    )
    variables["DOTNET_VERSION"] = (
        profile.get("target_frameworks", ["unknown"])[0] if profile.get("target_frameworks") else "unknown"
    )
    variables["COVERAGE_SETTINGS_PATH"] = profile.get("default_coverage_settings")
    coverage_targets = profile.get("coverage_targets", {}) if isinstance(profile.get("coverage_targets"), dict) else {}
    variables["COVERAGE_LINE_TARGET"] = coverage_targets.get("line", 100)
    variables["COVERAGE_BRANCH_TARGET"] = coverage_targets.get("branch", 100)
    variables["CONTEXT_ONLY"] = "true" if profile.get("virtual_project") else "false"
    variables["CONTEXT_BOOTSTRAP_MODE"] = profile.get("context_bootstrap_mode", "detected")
    variables["MANUAL_CONTEXT_UPDATED_AT_UTC"] = _utc_now_iso()
    return variables


def _snapshot_sources(snapshot_payload: Any) -> list[dict[str, Any]]:
    if isinstance(snapshot_payload, dict):
        files = snapshot_payload.get("files", [])
    elif isinstance(snapshot_payload, list):
        files = snapshot_payload
    else:
        files = []

    entries: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(files):
        if not isinstance(raw_entry, dict):
            continue
        path = str(raw_entry.get("path", "") or raw_entry.get("name", "") or f"item-{index}").strip()
        normalized_path = path.replace("\\", "/")
        metadata = {key: value for key, value in raw_entry.items() if key != "content"}
        content = str(raw_entry.get("content", "")).rstrip()
        summary = str(raw_entry.get("summary", "")).strip()

        body_parts = [f"PATH: {normalized_path or f'item-{index}'}"]
        if metadata.get("kind"):
            body_parts.append(f"KIND: {metadata['kind']}")
        if "changed" in metadata:
            body_parts.append(f"CHANGED: {metadata['changed']}")
        if summary:
            body_parts.extend(["", "SUMMARY:", summary])
        if content:
            body_parts.extend(["", "CONTENT:", content])
        elif metadata:
            body_parts.extend(["", "METADATA:", json.dumps(metadata, indent=2, ensure_ascii=True)])

        entries.append(
            {
                "source": f"snapshot://{normalized_path or f'item-{index}'}",
                "content": "\n".join(body_parts).strip(),
                "metadata": {
                    "kind": metadata.get("kind", "snapshot"),
                    "path": normalized_path or None,
                    "language": metadata.get("language"),
                    "changed": metadata.get("changed"),
                    "symbols": metadata.get("symbols"),
                },
            }
        )
    return entries


def _apply_manual_context(
    project_root: str,
    project_manifest_json: str = "{}",
    source_snapshot_json: str = "{}",
    file_tree: str = "",
    notes: str = "",
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    manifest_payload = _parse_json_payload("project_manifest_json", project_manifest_json, {})
    snapshot_payload = _parse_json_payload("source_snapshot_json", source_snapshot_json, {})
    if manifest_payload and not isinstance(manifest_payload, dict):
        raise ValueError("project_manifest_json must be a JSON object.")
    if snapshot_payload and not isinstance(snapshot_payload, (dict, list)):
        raise ValueError("source_snapshot_json must be a JSON object or array.")

    state_files = _context_state_files(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    current_profile = _read_json_file(state_files["profile_path"], default={})
    current_variables = _read_json_file(state_files["variables_path"], default={})

    merged_profile = _merge_project_manifest(current_profile, manifest_payload if isinstance(manifest_payload, dict) else {}, project_root)
    merged_variables = _merge_variables(current_variables, merged_profile)
    _write_json_file(state_files["profile_path"], merged_profile)
    _write_json_file(state_files["variables_path"], merged_variables)

    upserted_sources: list[str] = []

    manifest_text = json.dumps(manifest_payload if isinstance(manifest_payload, dict) else {}, indent=2, ensure_ascii=True)
    if str(manifest_text).strip() not in {"{}", ""}:
        upsert_memory(
            project_root=project_root,
            source="context://project-manifest",
            content=manifest_text,
            metadata={"kind": "project_manifest"},
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        upserted_sources.append("context://project-manifest")

    if str(file_tree).strip():
        upsert_memory(
            project_root=project_root,
            source="context://file-tree",
            content=str(file_tree).strip(),
            metadata={"kind": "file_tree"},
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        upserted_sources.append("context://file-tree")

    if str(notes).strip():
        upsert_memory(
            project_root=project_root,
            source="context://manual-notes",
            content=str(notes).strip(),
            metadata={"kind": "manual_notes"},
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        upserted_sources.append("context://manual-notes")

    for entry in _snapshot_sources(snapshot_payload):
        upsert_memory(
            project_root=project_root,
            source=entry["source"],
            content=entry["content"],
            metadata=entry["metadata"],
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        upserted_sources.append(entry["source"])

    memory_index = index_project_memory(
        project_root=project_root,
        include_agents=True,
        include_metrics=True,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    return {
        "updated_profile": merged_profile,
        "updated_variables": merged_variables,
        "upserted_sources": upserted_sources,
        "memory_index": memory_index,
        "state_dir": str(state_files["state_dir"]),
    }


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
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    profile = detect_project_profile(resolved_root)
    details = _project_binding_details(identity, resolved_root, requested_project_root=project_root)
    workflow = _workflow_guidance_payload(details["server_files_available"])
    return {
        "status": "ok",
        **profile,
        **details,
        **workflow,
        "context_only_hint": (
            "This project is currently bound as logical context only. Preferred remote flow: bootstrap_with_context "
            "or ingest_project_snapshot, then prepare_test_generation_context so the external LLM can write tests "
            "locally in the developer workspace. Only the server-execution tools "
            "(discover_test_targets, generate_tests, validate, coverage_gate, pipeline) require the repository to be visible on the MCP server."
            if not details["server_files_available"]
            else None
        ),
    }


@mcp.tool()
def list_visible_projects(
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """List .NET projects currently visible to the MCP container/server."""
    candidates = _discover_project_candidates(config_toml_path=config_toml_path)
    return {
        "status": "ok",
        "projects_found": len(candidates),
        "projects": candidates,
        "search_roots": [str(path) for path in _router_search_roots(config_toml_path=config_toml_path)],
        "generated_at_utc": _utc_now_iso(),
    }


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
    binding_variables: dict[str, Any] = {
        "context_id": identity["context_id"] or None,
        "developer_id": identity["developer_id"],
        "workspace_id": identity["workspace_id"],
    }

    if project_root and project_root.strip():
        resolved_payload = _resolve_project_reference(
            reference=project_root,
            identity=identity,
            config_toml_path=config_toml_path,
            require_server_files=False,
        )
        resolved_root = _normalize_fs_path(str(resolved_payload["project_root"]))
        selected_by = "manual"
        selection_reason = "manual project_root provided"
        binding_variables.update(
            {
                "resolution": resolved_payload["resolution"],
                "server_files_available": resolved_payload["server_files_available"],
                "requested_project_root": resolved_payload["requested_project_root"],
            }
        )
    else:
        resolved_root = ""
        if not force_reselect and cached:
            cached_root = _normalize_fs_path(str(cached.get("project_root", "")).strip())
            cached_variables = _binding_variables(cached)
            if cached_root and Path(cached_root).exists():
                resolved_root = cached_root
                selection_reason = str(cached.get("selection_reason", selection_reason))
                selected_by = "cached"
                binding_variables.update(
                    {
                        "resolution": str(cached_variables.get("resolution", "cached")).strip() or "cached",
                        "server_files_available": cached_variables.get(
                            "server_files_available",
                            not _is_virtual_project_root(cached_root),
                        ),
                        "requested_project_root": str(
                            cached_variables.get("requested_project_root", cached_root)
                        ).strip()
                        or cached_root,
                    }
                )
            else:
                _clear_active_binding(identity)
                diagnostics["stale_binding_cleared"] = True

        if not resolved_root:
            candidates = _discover_project_candidates(config_toml_path=config_toml_path)
            if not candidates:
                reference = project_root or intent.strip() or identity.get("context_id") or identity.get("workspace_id") or "remote-project"
                resolved_payload = _resolve_project_reference(
                    reference=reference,
                    identity=identity,
                    config_toml_path=config_toml_path,
                    require_server_files=False,
                )
                resolved_root = _normalize_fs_path(str(resolved_payload["project_root"]))
                selected_by = "virtual"
                selection_reason = (
                    "created logical project context because no server-side .NET projects are visible"
                )
                diagnostics["candidate_count"] = 0
                diagnostics["context_only_mode"] = True
                diagnostics["message"] = (
                    "Remote MCP is running in context-only mode for this project. Use bootstrap_with_context or "
                    "ingest_project_snapshot to send project metadata/source snapshots, then call "
                    "prepare_test_generation_context so the external LLM can write tests locally. "
                    "Only the server-execution tools require the repository to be mounted or synced on the server."
                )
                binding_variables.update(
                    {
                        "resolution": resolved_payload["resolution"],
                        "server_files_available": resolved_payload["server_files_available"],
                        "requested_project_root": resolved_payload["requested_project_root"],
                    }
                )
            else:
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
                binding_variables.update(
                    {
                        "resolution": "router_candidate",
                        "server_files_available": True,
                        "requested_project_root": resolved_root,
                    }
                )

    binding = _set_active_binding(
        identity=identity,
        project_root=resolved_root,
        selected_by=selected_by,
        selection_reason=selection_reason,
        intent=intent,
        variables=binding_variables,
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
        "execution_mode": "server_execution" if binding_variables.get("server_files_available") else "context_only",
        "server_files_available": bool(binding_variables.get("server_files_available")),
        **_workflow_guidance_payload(bool(binding_variables.get("server_files_available"))),
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
            "preferred_workflow": "context_only",
            "next_actions": [
                {
                    "tool": "route_project",
                    "why": "bind a logical or mounted project before requesting test generation guidance",
                }
            ],
            "prompt_name": "context_only_workflow",
            "resource_uri": "usage://workflow",
        }

    root = _normalize_fs_path(str(binding.get("project_root", "")))
    exists = bool(root and Path(root).exists())
    details = _project_binding_details(identity, root)
    return {
        "status": "ok",
        "found": True,
        "identity": identity,
        "project_root": root,
        "project_exists": exists,
        **details,
        **_workflow_guidance_payload(details["server_files_available"]),
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
    project_manifest_json: str = "{}",
    source_snapshot_json: str = "{}",
    file_tree: str = "",
    notes: str = "",
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Initialize context and optionally ingest a project snapshot so the LLM can generate tests locally without server filesystem access."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    bootstrapped = bootstrap_project(
        project_root=resolved_root,
        overwrite_agents=overwrite_agents,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    manual_context = _apply_manual_context(
        project_root=resolved_root,
        project_manifest_json=project_manifest_json,
        source_snapshot_json=source_snapshot_json,
        file_tree=file_tree,
        notes=notes,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return {
        **bootstrapped,
        "manual_context_ingested": bool(
            manual_context["upserted_sources"]
            or str(project_manifest_json).strip() not in {"", "{}"}
            or str(file_tree).strip()
            or str(notes).strip()
        ),
        "manual_context": manual_context,
        "next_recommended_tool": "prepare_test_generation_context",
    }


@mcp.tool()
def ingest_project_snapshot(
    project_root: str | None = None,
    project_manifest_json: str = "{}",
    source_snapshot_json: str = "{}",
    file_tree: str = "",
    notes: str = "",
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Ingest remote project metadata/source snapshots into context + RAG so the client LLM can generate tests locally."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    _ensure_context_materialized(
        project_root=resolved_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    manual_context = _apply_manual_context(
        project_root=resolved_root,
        project_manifest_json=project_manifest_json,
        source_snapshot_json=source_snapshot_json,
        file_tree=file_tree,
        notes=notes,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return {
        "status": "ok",
        "project_root": resolved_root,
        "execution_mode": "context_only",
        "upserted_sources_count": len(manual_context["upserted_sources"]),
        "upserted_sources": manual_context["upserted_sources"],
        "memory_index": manual_context["memory_index"],
        "state_dir": manual_context["state_dir"],
        "next_recommended_tool": "prepare_test_generation_context",
    }


@mcp.tool()
def prepare_test_generation_context(
    objective: str,
    class_name: str = "",
    method_name: str = "",
    file_path: str = "",
    max_chunks: int | None = None,
    max_chars: int | None = None,
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Prepare a compact prompt package for an external LLM to write/update test files locally in the developer workspace."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    _ensure_context_materialized(
        project_root=resolved_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    details = _project_binding_details(identity, resolved_root, requested_project_root=project_root)
    state_files = _context_state_files(
        project_root=resolved_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    profile = _read_json_file(state_files["profile_path"], default={})
    variables = _read_json_file(state_files["variables_path"], default={})

    query_parts = [
        objective,
        class_name,
        method_name,
        file_path,
        str(profile.get("project_name", "")).strip(),
        "csharp dotnet tests xunit mocks coverage testing rules",
    ]
    query_text = " | ".join([part for part in query_parts if str(part).strip()])
    rag_payload = query_memory(
        project_root=resolved_root,
        query=query_text,
        max_chunks=max_chunks,
        max_chars=max_chars,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    target_test_project = str(
        variables.get("TEST_PROJECT_PATH", "") or profile.get("default_test_project", "")
    ).strip()
    prompt_lines = [
        "You are writing or updating C# tests in the developer local workspace.",
        f"Execution mode: {details['execution_mode']}.",
        "Do not ask the MCP server to mount or access the repository when execution mode is context_only.",
        "Use the project profile, retrieved context and packaged testing agents below.",
        "",
        f"Objective: {objective}",
    ]
    if class_name:
        prompt_lines.append(f"Focus class: {class_name}")
    if method_name:
        prompt_lines.append(f"Focus method: {method_name}")
    if file_path:
        prompt_lines.append(f"Focus file: {file_path}")
    if target_test_project:
        prompt_lines.append(f"Target test project hint: {target_test_project}")
    prompt_lines.extend(
        [
            "",
            "Project profile:",
            json.dumps(
                {
                    "project_name": profile.get("project_name"),
                    "solution_path": profile.get("solution_path"),
                    "default_test_project": profile.get("default_test_project"),
                    "test_frameworks": profile.get("test_frameworks"),
                    "target_frameworks": profile.get("target_frameworks"),
                    "coverage_targets": profile.get("coverage_targets"),
                },
                indent=2,
                ensure_ascii=True,
            ),
            "",
            "Retrieved context:",
            rag_payload.get("context_compact", ""),
            "",
            "Expected output:",
            "- produce complete test file content ready to be saved locally",
            "- respect xUnit naming and project test standards from the retrieved agent context",
            "- if context is incomplete, say exactly which class/method/file snapshot is missing instead of asking for server mounts",
        ]
    )

    return {
        "status": "ok",
        "project_root": resolved_root,
        **details,
        "objective": objective,
        "query_used": query_text,
        "target_test_project_hint": target_test_project or None,
        "prompt_package": "\n".join(prompt_lines).strip(),
        "rag": rag_payload,
        "next_actions": [
            {
                "step": "send_prompt_package",
                "why": "use the prepared context with the external LLM so it writes the test file locally",
            },
            {
                "step": "refresh_snapshot_after_changes",
                "why": "call ingest_project_snapshot again if the implementation or target test file changes",
            },
        ],
        "next_action": "Send prompt_package to the external LLM so it can write the test file locally.",
    }


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
    """Inspect changed C# source files and map candidate classes/methods that need tests. Requires server_execution mode."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
        require_server_files=True,
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
    """Generate baseline xUnit tests for changed public classes/methods. Requires server_execution mode."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
        require_server_files=True,
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
    """Run dotnet build/test and optional coverage collection for the project. Requires server_execution mode."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
        require_server_files=True,
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
    """Fail when changed files are below minimum line coverage. Requires server_execution mode."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
        require_server_files=True,
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
    """Run bootstrap + discovery + generation + coverage gate in a single call. Requires server_execution mode."""
    resolved_root = _resolve_project_root(
        project_root=project_root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
        require_server_files=True,
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
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    if project_root is not None:
        resolved_root = _normalize_fs_path(project_root)
    elif _get_active_binding(identity):
        resolved_root = _resolve_project_root(
            project_root=None,
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
def get_usage_guidance(
    project_root: str | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Return the preferred MCP workflow so external LLMs do not ask the developer for repository mounts unnecessarily."""
    identity = _resolve_identity(
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    server_files_available = False
    resolved_root = None

    try:
        resolved_root = _resolve_project_root(
            project_root=project_root,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        )
        details = _project_binding_details(identity, resolved_root, requested_project_root=project_root)
        server_files_available = details["server_files_available"]
    except Exception:
        details = {
            "execution_mode": "context_only",
            "server_files_available": False,
            "requested_project_root": _normalize_fs_path(project_root or ""),
            "resolution": "unresolved",
        }

    workflow = _workflow_guidance_payload(server_files_available)
    return {
        "status": "ok",
        "project_root": resolved_root,
        **details,
        **workflow,
        "server_instructions": SERVER_INSTRUCTIONS,
        "do_not_ask_for_mount_first": True,
    }


@mcp.tool()
def list_agent_files() -> dict[str, Any]:
    """List available agent markdown files packaged with this MCP server."""
    agents_dir = get_agents_assets_dir()
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


@mcp.prompt(
    name="context_only_workflow",
    title="Context-Only Workflow",
    description="Use this prompt when the repository is not visible to the MCP server and tests must be written locally by the external LLM.",
)
def context_only_workflow_prompt(objective: str = "") -> list[str]:
    prompt_lines = [
        "Use the context-only workflow for this MCP.",
        "Do not ask the developer to mount the repository as the first step.",
        "Do not ask broad open-ended questions.",
        "Call these tools in order:",
        "1. route_project",
        "2. bootstrap_with_context or ingest_project_snapshot",
        "3. prepare_test_generation_context",
        "Then use prompt_package so the external LLM writes the test file locally in the developer workspace.",
        "If context is incomplete, ask only for the exact missing class, method, file tree, or source snapshot.",
    ]
    if objective.strip():
        prompt_lines.extend(["", f"Current objective: {objective.strip()}"])
    return ["\n".join(prompt_lines)]


@mcp.prompt(
    name="server_execution_workflow",
    title="Server Execution Workflow",
    description="Use this prompt when the repository is mounted on the MCP server and server-side generation/validation tools may run directly.",
)
def server_execution_workflow_prompt(objective: str = "") -> list[str]:
    prompt_lines = [
        "Use the server-execution workflow for this MCP.",
        "The repository is visible to the MCP server, so direct generation and validation tools may be used.",
        "Recommended sequence:",
        "1. route_project",
        "2. discover_test_targets",
        "3. generate_tests",
        "4. validate",
        "5. coverage_gate or pipeline when appropriate",
    ]
    if objective.strip():
        prompt_lines.extend(["", f"Current objective: {objective.strip()}"])
    return ["\n".join(prompt_lines)]


@mcp.resource("usage://workflow")
def workflow_resource() -> str:
    """Expose the preferred MCP workflow instructions as a resource for clients."""
    return SERVER_INSTRUCTIONS


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
    mcp.settings.transport_security = server_settings["transport_security"]

    if transport == "sse":
        mcp.run(transport="sse")
        return

    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
