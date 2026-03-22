from __future__ import annotations

import json
import os
import re
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


def _normalize_fs_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return raw
    if os.name != "nt" and WINDOWS_PATH_PATTERN.match(raw):
        drive = raw[0].lower()
        remainder = raw[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{remainder}"
    return raw


def _resolve_project_root(
    project_root: str | None = None,
    config_toml_path: str | None = None,
) -> str:
    if project_root and project_root.strip():
        return _normalize_fs_path(project_root)

    env_project_root = os.getenv("DIGITAL_SOLUTIONS_PROJECT_ROOT", "").strip()
    if env_project_root:
        return _normalize_fs_path(env_project_root)

    settings_payload = runtime_settings(project_root=None, config_toml_path=config_toml_path)
    project_settings = settings_payload.get("settings", {}).get("project", {})
    config_project_root = str(project_settings.get("project_root", "")).strip()
    if config_project_root:
        return _normalize_fs_path(config_project_root)

    raise ValueError(
        "project_root was not provided. Set project_root argument, or set "
        "DIGITAL_SOLUTIONS_PROJECT_ROOT, or define [project].project_root in config.toml."
    )


@mcp.tool()
def detect_project(
    project_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Detect .NET solution/test projects and infer required variables for test automation."""
    return detect_project_profile(_resolve_project_root(project_root, config_toml_path))


@mcp.tool()
def bootstrap(
    project_root: str | None = None,
    overwrite_agents: bool = False,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Initialize .ai-test-mcp state in the target project and copy agent assets/templates."""
    resolved_root = _resolve_project_root(project_root, config_toml_path)
    return bootstrap_project(
        project_root=resolved_root,
        overwrite_agents=overwrite_agents,
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Inspect changed C# source files and map candidate classes/methods that need tests."""
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Generate baseline xUnit tests for changed public classes/methods."""
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    """Load effective TOML runtime settings used by the MCP server."""
    return runtime_settings(project_root=project_root, config_toml_path=config_toml_path)


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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    resolved_root = _resolve_project_root(project_root, config_toml_path)
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
    transport = os.getenv("DIGITAL_SOLUTIONS_MCP_TRANSPORT", "stdio").strip().lower()
    if transport in {"", "stdio"}:
        mcp.run()
        return

    host = os.getenv("DIGITAL_SOLUTIONS_MCP_HOST", "0.0.0.0").strip()
    port_raw = os.getenv("DIGITAL_SOLUTIONS_MCP_PORT", "8000").strip()
    path = os.getenv("DIGITAL_SOLUTIONS_MCP_PATH", "/mcp").strip() or "/mcp"
    stateless_http_raw = os.getenv("DIGITAL_SOLUTIONS_MCP_STATELESS_HTTP", "true").strip().lower()
    json_response_raw = os.getenv("DIGITAL_SOLUTIONS_MCP_JSON_RESPONSE", "true").strip().lower()

    try:
        port = int(port_raw)
    except ValueError:
        raise ValueError(f"Invalid DIGITAL_SOLUTIONS_MCP_PORT value: {port_raw}")

    stateless_http = stateless_http_raw in {"1", "true", "yes", "on"}
    json_response = json_response_raw in {"1", "true", "yes", "on"}

    # FastMCP runtime settings are read from mcp.settings for HTTP transports.
    mcp.settings.host = host
    mcp.settings.port = port
    mcp.settings.streamable_http_path = path
    mcp.settings.stateless_http = stateless_http
    mcp.settings.json_response = json_response

    mcp.run(transport=transport)


if __name__ == "__main__":
    main()
