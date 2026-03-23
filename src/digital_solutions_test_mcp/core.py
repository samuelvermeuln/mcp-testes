from __future__ import annotations

import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import tomllib
import xml.etree.ElementTree as ET
from hashlib import sha1
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

EXCLUDED_DIR_NAMES = {
    ".git",
    ".idea",
    ".vs",
    ".vscode",
    "bin",
    "obj",
    "node_modules",
    "packages",
    "TestResults",
}

CLASS_PATTERN = re.compile(
    r"^\s*(public|internal|protected|private)?\s*(?:abstract\s+|sealed\s+|partial\s+)*"
    r"(class|record)\s+([A-Za-z_][A-Za-z0-9_]*)",
    re.MULTILINE,
)

METHOD_PATTERN = re.compile(
    r"^\s*(public|internal|protected)\s+"
    r"(?:virtual\s+|override\s+|sealed\s+|static\s+|async\s+|partial\s+)*"
    r"([A-Za-z_][A-Za-z0-9_<>,\.\[\]\?]*)\s+"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*\(([^\)]*)\)",
    re.MULTILINE,
)

NAMESPACE_PATTERN = re.compile(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_\.]*)", re.MULTILINE)
WINDOWS_PATH_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")

TABLE_HEADER = (
    "| TEST_CASE_ID | FEATURE | TEST_NAME | TYPE | COMPLEXITY | START_TIME_UTC | END_TIME_UTC | "
    "ACTUAL_MINUTES | BASELINE_MANUAL_MINUTES | SAVINGS_MINUTES | SAVINGS_PERCENT | "
    "PRODUCTIVITY_RATIO | STATUS | NOTES |"
)

DEFAULT_CONFIG_CANDIDATES = ("config.toml", ".digital-solutions-test-mcp.toml")
DEFAULT_RAG_CHUNK_CHARS = 1200
DEFAULT_RAG_OVERLAP_CHARS = 180
DEFAULT_RAG_MAX_CHUNKS = 8
DEFAULT_RAG_MAX_CHARS = 7000


def _normalize_fs_path(value: str) -> str:
    raw = (value or "").strip()
    if not raw:
        return raw

    # Convert Windows paths when running under Linux/WSL.
    if os.name != "nt" and WINDOWS_PATH_PATTERN.match(raw):
        drive = raw[0].lower()
        remainder = raw[2:].replace("\\", "/").lstrip("/")
        return f"/mnt/{drive}/{remainder}"

    return raw


@dataclass
class CommandResult:
    command: list[str]
    returncode: int
    stdout: str
    stderr: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "command": " ".join(self.command),
            "returncode": self.returncode,
            "stdout": self.stdout,
            "stderr": self.stderr,
        }



def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _is_excluded(path: Path) -> bool:
    return any(part in EXCLUDED_DIR_NAMES for part in path.parts)


def _iter_files(root: Path, suffix: str) -> list[Path]:
    files: list[Path] = []
    for file_path in root.rglob(f"*{suffix}"):
        if file_path.is_file() and not _is_excluded(file_path):
            files.append(file_path)
    return sorted(files)


def _run_command(command: list[str], cwd: Path, timeout: int = 1800) -> CommandResult:
    try:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
        return CommandResult(
            command=command,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        timeout_message = f"Command timed out after {timeout}s: {' '.join(command)}"
        stderr = f"{stderr}\n{timeout_message}".strip()
        return CommandResult(
            command=command,
            returncode=124,
            stdout=stdout,
            stderr=stderr,
        )


def _parse_tag(content: str, tag: str) -> str | None:
    match = re.search(rf"<{tag}>(.*?)</{tag}>", content, flags=re.IGNORECASE | re.DOTALL)
    if not match:
        return None
    return match.group(1).strip()


def _is_test_project(csproj_path: Path) -> bool:
    lowered = csproj_path.name.lower()
    if "test" in lowered:
        return True
    content = _safe_read(csproj_path).lower()
    return any(pkg in content for pkg in ["xunit", "mstest", "nunit", "microsoft.net.test.sdk"])


def _infer_test_framework(csproj_content: str) -> str:
    lowered = csproj_content.lower()
    if "xunit" in lowered:
        return "xunit"
    if "mstest" in lowered:
        return "mstest"
    if "nunit" in lowered:
        return "nunit"
    return "unknown"


def _infer_dotnet_target(csproj_content: str) -> str | None:
    target_framework = _parse_tag(csproj_content, "TargetFramework")
    if target_framework:
        return target_framework

    target_frameworks = _parse_tag(csproj_content, "TargetFrameworks")
    if target_frameworks:
        return target_frameworks.split(";")[0].strip()

    return None


def _find_coverage_settings(root: Path) -> list[Path]:
    result: list[Path] = []
    for file_path in root.rglob("*"):
        if not file_path.is_file() or _is_excluded(file_path):
            continue
        lower = file_path.name.lower()
        if lower.endswith(".runsettings") or lower == "coverlet.settings.xml":
            result.append(file_path)
    return sorted(result)


def _short(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def _select_primary_solution(solutions: list[Path], root: Path) -> Path | None:
    if not solutions:
        return None

    # Prefer a solution at root, then shortest relative path.
    ranked = sorted(
        solutions,
        key=lambda s: (
            0 if s.parent == root else 1,
            len(_short(s, root)),
            _short(s, root),
        ),
    )
    return ranked[0]


def detect_project_profile(project_root: str) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    if not root.exists():
        raise FileNotFoundError(f"Project root not found: {root}")

    solutions = _iter_files(root, ".sln")
    csprojs = _iter_files(root, ".csproj")
    reference_metadata = _project_reference_metadata(root)

    test_projects: list[Path] = []
    app_projects: list[Path] = []
    for csproj in csprojs:
        if _is_test_project(csproj):
            test_projects.append(csproj)
        else:
            app_projects.append(csproj)

    primary_solution = _select_primary_solution(solutions, root)

    frameworks: set[str] = set()
    target_frameworks: set[str] = set()
    for test_proj in test_projects:
        content = _safe_read(test_proj)
        frameworks.add(_infer_test_framework(content))
        target = _infer_dotnet_target(content)
        if target:
            target_frameworks.add(target)

    coverage_settings = _find_coverage_settings(root)

    profile = {
        "project_name": root.name,
        "project_root": str(root),
        "solution_path": _short(primary_solution, root) if primary_solution else None,
        "all_solutions": [_short(s, root) for s in solutions],
        "test_projects": [_short(t, root) for t in test_projects],
        "app_projects": [_short(a, root) for a in app_projects],
        "test_frameworks": sorted(frameworks),
        "target_frameworks": sorted(target_frameworks),
        "coverage_settings_candidates": [_short(c, root) for c in coverage_settings],
        "default_test_project": _short(test_projects[0], root) if test_projects else None,
        "default_coverage_settings": _short(coverage_settings[0], root) if coverage_settings else None,
        "coverage_targets": {
            "line": 100,
            "branch": 100,
        },
        "server_project_files_found": bool(solutions or csprojs),
        "virtual_project": bool(reference_metadata.get("virtual_project", False)),
        "original_reference": reference_metadata.get("original_reference"),
        "metrics_baseline_minutes": {
            "S": 20,
            "M": 45,
            "L": 90,
        },
        "generated_at_utc": utc_now_iso(),
    }

    return profile


def _load_toml_settings(
    project_root: Path | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    candidate_paths: list[Path] = []

    if config_toml_path:
        candidate_paths.append(Path(_normalize_fs_path(config_toml_path)).expanduser().resolve())

    env_config = os.getenv("DIGITAL_SOLUTIONS_MCP_CONFIG_TOML", "").strip()
    if env_config:
        candidate_paths.append(Path(_normalize_fs_path(env_config)).expanduser().resolve())

    project_root_path = project_root.resolve() if project_root else None
    if project_root_path:
        for name in DEFAULT_CONFIG_CANDIDATES:
            candidate_paths.append((project_root_path / name).resolve())

    server_root = Path(__file__).resolve().parents[2]
    for name in DEFAULT_CONFIG_CANDIDATES:
        candidate_paths.append((server_root / name).resolve())

    seen: set[Path] = set()
    for candidate in candidate_paths:
        if candidate in seen:
            continue
        seen.add(candidate)
        if not candidate.exists() or not candidate.is_file():
            continue
        with candidate.open("rb") as handle:
            payload = tomllib.load(handle)
            payload["_meta"] = {"config_path": str(candidate)}
            return payload

    return {}


def _slug(value: str, fallback: str = "default") -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "-", (value or "").strip()).strip("-").lower()
    return normalized or fallback


def _resolve_context(
    root: Path,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    settings = _load_toml_settings(project_root=root, config_toml_path=config_toml_path)
    context_settings = settings.get("context", {}) if isinstance(settings, dict) else {}

    resolved_context_id = (
        context_id
        or os.getenv("DIGITAL_SOLUTIONS_CONTEXT_ID", "").strip()
        or context_settings.get("context_id")
        or ""
    )
    resolved_developer_id = (
        developer_id
        or os.getenv("DIGITAL_SOLUTIONS_DEVELOPER_ID", "").strip()
        or context_settings.get("developer_id")
        or os.getenv("USERNAME", "").strip()
        or os.getenv("USER", "").strip()
        or "dev"
    )
    resolved_workspace_id = (
        workspace_id
        or os.getenv("DIGITAL_SOLUTIONS_WORKSPACE_ID", "").strip()
        or context_settings.get("workspace_id")
        or "default-workspace"
    )

    resolved_context_root = (
        context_root
        or os.getenv("DIGITAL_SOLUTIONS_CONTEXT_ROOT", "").strip()
        or context_settings.get("store_root")
        or ""
    )

    mode = str(context_settings.get("mode", "project_local")).strip().lower()
    wants_isolated_context = bool(
        resolved_context_id
        or developer_id
        or workspace_id
        or context_root
        or resolved_context_root
        or mode != "project_local"
    )

    if not wants_isolated_context:
        state_dir = root / ".ai-test-mcp"
        context_key = "project_local_default"
    else:
        if resolved_context_id:
            context_key = _slug(resolved_context_id, fallback="context")
        else:
            seed = f"{root}|{resolved_developer_id}|{resolved_workspace_id}"
            digest = sha1(seed.encode("utf-8")).hexdigest()[:10]
            context_key = (
                f"{_slug(root.name)}__{_slug(resolved_developer_id)}__"
                f"{_slug(resolved_workspace_id)}__{digest}"
            )

        if resolved_context_root:
            base_root = Path(_normalize_fs_path(str(resolved_context_root))).expanduser().resolve()
            state_dir = base_root / context_key
        else:
            state_dir = root / ".ai-test-mcp" / "contexts" / context_key

    return {
        "state_dir": state_dir,
        "context_key": context_key,
        "context_id": resolved_context_id or None,
        "developer_id": resolved_developer_id,
        "workspace_id": resolved_workspace_id,
        "mode": mode,
        "config_path": settings.get("_meta", {}).get("config_path") if isinstance(settings, dict) else None,
    }


def _ensure_state_dir_writable(state_dir: Path, root: Path, context_key: str) -> Path:
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        return state_dir
    except PermissionError:
        fallback = root / ".ai-test-mcp" / "contexts" / context_key
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _mcp_state_dir(
    root: Path,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> Path:
    resolved = _resolve_context(
        root=root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    return resolved["state_dir"]


def _assets_agents_dir() -> Path:
    candidates: list[Path] = []

    configured_assets_dir = os.getenv("DIGITAL_SOLUTIONS_ASSETS_DIR", "").strip()
    if configured_assets_dir:
        candidates.append(Path(_normalize_fs_path(configured_assets_dir)).expanduser().resolve())

    module_file = Path(__file__).resolve()
    candidates.extend(
        [
            module_file.parent / "assets" / "Agents.Testing",
            module_file.parents[2] / "assets" / "Agents.Testing",
            Path.cwd().resolve() / "assets" / "Agents.Testing",
        ]
    )

    configured_toml = os.getenv("DIGITAL_SOLUTIONS_MCP_CONFIG_TOML", "").strip()
    if configured_toml:
        config_path = Path(_normalize_fs_path(configured_toml)).expanduser().resolve()
        candidates.append(config_path.parent / "assets" / "Agents.Testing")

    candidates.append(Path("/app/assets/Agents.Testing"))

    seen: set[Path] = set()
    normalized_candidates: list[Path] = []
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        normalized_candidates.append(candidate)

    for candidate in normalized_candidates:
        if candidate.exists() and candidate.is_dir():
            return candidate

    searched = ", ".join(str(path) for path in normalized_candidates)
    raise FileNotFoundError(f"Agents assets not found. Searched: {searched}")


def get_agents_assets_dir() -> Path:
    return _assets_agents_dir()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


def _read_json(path: Path, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return {} if default is None else default
    text = _safe_read(path)
    if not text.strip():
        return {} if default is None else default
    return json.loads(text)


def _project_reference_metadata(root: Path) -> dict[str, Any]:
    metadata_path = root / ".ai-test-mcp" / "project-reference.json"
    if not metadata_path.exists() or not metadata_path.is_file():
        return {}
    payload = _read_json(metadata_path, default={})
    return payload if isinstance(payload, dict) else {}


def _estimate_tokens(text: str) -> int:
    # Lightweight heuristic used only for budgeting context payloads.
    return max(1, math.ceil(len(text) / 4))


def _normalize_whitespace(text: str) -> str:
    compact = re.sub(r"\r\n?", "\n", text or "")
    compact = re.sub(r"[ \t]+", " ", compact)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def _chunk_text(
    text: str,
    max_chars: int = DEFAULT_RAG_CHUNK_CHARS,
    overlap_chars: int = DEFAULT_RAG_OVERLAP_CHARS,
) -> list[str]:
    cleaned = _normalize_whitespace(text)
    if not cleaned:
        return []

    if len(cleaned) <= max_chars:
        return [cleaned]

    chunks: list[str] = []
    start = 0
    step = max(64, max_chars - max(0, overlap_chars))
    while start < len(cleaned):
        end = min(len(cleaned), start + max_chars)
        snippet = cleaned[start:end].strip()
        if snippet:
            chunks.append(snippet)
        if end >= len(cleaned):
            break
        start += step
    return chunks


def _project_key(root: Path) -> str:
    return sha1(str(root).encode("utf-8")).hexdigest()


def _memory_db_path(
    root: Path,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> Path:
    resolved_context = _resolve_context(
        root=root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    state_dir = _ensure_state_dir_writable(
        state_dir=resolved_context["state_dir"],
        root=root,
        context_key=resolved_context["context_key"],
    )
    memory_dir = state_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    return memory_dir / "rag-memory.sqlite3"


def _connect_memory_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA temp_store=MEMORY;")
    return conn


def _ensure_memory_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS memory_chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_key TEXT NOT NULL,
            context_key TEXT NOT NULL,
            source TEXT NOT NULL,
            chunk_index INTEGER NOT NULL,
            content TEXT NOT NULL,
            token_estimate INTEGER NOT NULL,
            metadata_json TEXT NOT NULL DEFAULT '{}',
            created_at_utc TEXT NOT NULL,
            updated_at_utc TEXT NOT NULL
        );

        CREATE UNIQUE INDEX IF NOT EXISTS ux_memory_chunks_scope
        ON memory_chunks(project_key, context_key, source, chunk_index);

        CREATE INDEX IF NOT EXISTS ix_memory_chunks_lookup
        ON memory_chunks(project_key, context_key, updated_at_utc DESC);
        """
    )


def _upsert_source_chunks(
    conn: sqlite3.Connection,
    project_key: str,
    context_key: str,
    source: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    chunk_chars: int = DEFAULT_RAG_CHUNK_CHARS,
    chunk_overlap_chars: int = DEFAULT_RAG_OVERLAP_CHARS,
) -> dict[str, Any]:
    normalized_source = source.strip()
    if not normalized_source:
        raise ValueError("source must not be empty")

    chunks = _chunk_text(content, max_chars=chunk_chars, overlap_chars=chunk_overlap_chars)
    now = utc_now_iso()
    meta_json = json.dumps(metadata or {}, ensure_ascii=True, separators=(",", ":"))

    with conn:
        conn.execute(
            """
            DELETE FROM memory_chunks
            WHERE project_key = ? AND context_key = ? AND source = ?
            """,
            (project_key, context_key, normalized_source),
        )

        for idx, chunk in enumerate(chunks):
            conn.execute(
                """
                INSERT INTO memory_chunks(
                    project_key, context_key, source, chunk_index, content,
                    token_estimate, metadata_json, created_at_utc, updated_at_utc
                )
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_key,
                    context_key,
                    normalized_source,
                    idx,
                    chunk,
                    _estimate_tokens(chunk),
                    meta_json,
                    now,
                    now,
                ),
            )

    return {
        "source": normalized_source,
        "chunks": len(chunks),
        "tokens_estimate": sum(_estimate_tokens(chunk) for chunk in chunks),
    }


def _tokenize_query(text: str) -> list[str]:
    raw = re.findall(r"[A-Za-z0-9_]{2,}", text.lower())
    return sorted(set(raw))


def _score_chunk(content: str, terms: list[str]) -> int:
    if not terms:
        return 1
    lowered = content.lower()
    return sum(lowered.count(term) for term in terms)


def _memory_scope(
    root: Path,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    resolved_context = _resolve_context(
        root=root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    state_dir = _ensure_state_dir_writable(
        state_dir=resolved_context["state_dir"],
        root=root,
        context_key=resolved_context["context_key"],
    )
    return {
        "project_key": _project_key(root),
        "context_key": resolved_context["context_key"],
        "state_dir": state_dir,
        "db_path": _memory_db_path(
            root,
            context_id=context_id,
            developer_id=developer_id,
            workspace_id=workspace_id,
            context_root=context_root,
            config_toml_path=config_toml_path,
        ),
    }


def _coerce_positive_int(value: Any, default: int, minimum: int = 1) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _memory_runtime_settings(
    root: Path,
    config_toml_path: str | None = None,
) -> dict[str, int]:
    settings = _load_toml_settings(project_root=root, config_toml_path=config_toml_path)
    memory = settings.get("memory", {}) if isinstance(settings, dict) else {}
    return {
        "chunk_chars": _coerce_positive_int(memory.get("chunk_chars"), DEFAULT_RAG_CHUNK_CHARS, minimum=256),
        "chunk_overlap_chars": _coerce_positive_int(
            memory.get("chunk_overlap_chars"), DEFAULT_RAG_OVERLAP_CHARS, minimum=0
        ),
        "default_max_chunks": _coerce_positive_int(
            memory.get("default_max_chunks"), DEFAULT_RAG_MAX_CHUNKS, minimum=1
        ),
        "default_max_chars": _coerce_positive_int(memory.get("default_max_chars"), DEFAULT_RAG_MAX_CHARS, minimum=256),
    }


def upsert_memory(
    project_root: str,
    source: str,
    content: str,
    metadata: dict[str, Any] | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    memory_settings = _memory_runtime_settings(root, config_toml_path=config_toml_path)
    scope = _memory_scope(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    with _connect_memory_db(scope["db_path"]) as conn:
        _ensure_memory_schema(conn)
        result = _upsert_source_chunks(
            conn=conn,
            project_key=scope["project_key"],
            context_key=scope["context_key"],
            source=source,
            content=content,
            metadata=metadata,
            chunk_chars=memory_settings["chunk_chars"],
            chunk_overlap_chars=memory_settings["chunk_overlap_chars"],
        )

    return {
        "status": "ok",
        "project_root": str(root),
        "context_key": scope["context_key"],
        "db_path": str(scope["db_path"]),
        "upserted": result,
        "generated_at_utc": utc_now_iso(),
    }


def index_project_memory(
    project_root: str,
    include_agents: bool = True,
    include_metrics: bool = True,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    memory_settings = _memory_runtime_settings(root, config_toml_path=config_toml_path)
    scope = _memory_scope(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    state_dir: Path = scope["state_dir"]

    sources_to_index: list[tuple[str, Path, dict[str, Any]]] = []

    core_state_files = [
        ("state://project-profile", state_dir / "project-profile.json", {"kind": "state"}),
        ("state://variables", state_dir / "variables.json", {"kind": "state"}),
        ("state://context", state_dir / "context.json", {"kind": "state"}),
    ]
    sources_to_index.extend(core_state_files)

    if include_metrics:
        sources_to_index.extend(
            [
                ("metrics://test-metrics-log", state_dir / "metrics" / "test-metrics-log.md", {"kind": "metrics"}),
                ("metrics://ai-savings-report", state_dir / "metrics" / "ai-savings-report.md", {"kind": "metrics"}),
            ]
        )

    if include_agents:
        agents_dir = state_dir / "agents"
        if agents_dir.exists():
            for agent_file in sorted(agents_dir.glob("*.md")):
                sources_to_index.append(
                    (
                        f"agent://{agent_file.name}",
                        agent_file,
                        {"kind": "agent", "file_name": agent_file.name},
                    )
                )

    indexed: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    with _connect_memory_db(scope["db_path"]) as conn:
        _ensure_memory_schema(conn)
        for source_name, file_path, meta in sources_to_index:
            if not file_path.exists() or not file_path.is_file():
                skipped.append({"source": source_name, "reason": "file_not_found"})
                continue
            text = _safe_read(file_path)
            if not text.strip():
                skipped.append({"source": source_name, "reason": "empty"})
                continue
            upsert_result = _upsert_source_chunks(
                conn=conn,
                project_key=scope["project_key"],
                context_key=scope["context_key"],
                source=source_name,
                content=text,
                metadata=meta | {"path": str(file_path)},
                chunk_chars=memory_settings["chunk_chars"],
                chunk_overlap_chars=memory_settings["chunk_overlap_chars"],
            )
            indexed.append(upsert_result)

    return {
        "status": "ok",
        "project_root": str(root),
        "context_key": scope["context_key"],
        "db_path": str(scope["db_path"]),
        "indexed_sources": len(indexed),
        "indexed": indexed,
        "skipped": skipped,
        "generated_at_utc": utc_now_iso(),
    }


def query_memory(
    project_root: str,
    query: str,
    max_chunks: int | None = None,
    max_chars: int | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    memory_settings = _memory_runtime_settings(root, config_toml_path=config_toml_path)
    max_chunks_resolved = _coerce_positive_int(
        max_chunks,
        memory_settings["default_max_chunks"],
        minimum=1,
    )
    max_chars_resolved = _coerce_positive_int(
        max_chars,
        memory_settings["default_max_chars"],
        minimum=256,
    )
    scope = _memory_scope(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    terms = _tokenize_query(query)

    with _connect_memory_db(scope["db_path"]) as conn:
        _ensure_memory_schema(conn)
        rows = conn.execute(
            """
            SELECT source, chunk_index, content, token_estimate, metadata_json, updated_at_utc
            FROM memory_chunks
            WHERE project_key = ? AND context_key = ?
            ORDER BY updated_at_utc DESC, source ASC, chunk_index ASC
            """,
            (scope["project_key"], scope["context_key"]),
        ).fetchall()

    scored: list[dict[str, Any]] = []
    for row in rows:
        score = _score_chunk(str(row["content"]), terms)
        if terms and score <= 0:
            continue
        scored.append(
            {
                "source": str(row["source"]),
                "chunk_index": int(row["chunk_index"]),
                "content": str(row["content"]),
                "score": score,
                "token_estimate": int(row["token_estimate"]),
                "updated_at_utc": str(row["updated_at_utc"]),
                "metadata": json.loads(str(row["metadata_json"]) or "{}"),
            }
        )

    scored.sort(key=lambda item: (item["score"], item["updated_at_utc"]), reverse=True)

    selected: list[dict[str, Any]] = []
    used_chars = 0
    for row in scored:
        if len(selected) >= max_chunks_resolved:
            break
        content = row["content"]
        new_chars = used_chars + len(content)
        if selected and new_chars > max_chars_resolved:
            continue
        used_chars = new_chars
        selected.append(row)

    compact_blocks: list[str] = []
    for row in selected:
        compact_blocks.append(
            f"[{row['source']}#{row['chunk_index']}] {row['content']}"
        )
    compact_context = "\n\n".join(compact_blocks)

    return {
        "status": "ok",
        "query": query,
        "terms": terms,
        "project_root": str(root),
        "context_key": scope["context_key"],
        "db_path": str(scope["db_path"]),
        "matched_chunks": len(scored),
        "selected_chunks": len(selected),
        "selected_token_estimate": sum(item["token_estimate"] for item in selected),
        "selected_char_count": len(compact_context),
        "max_chunks": max_chunks_resolved,
        "max_chars": max_chars_resolved,
        "context_compact": compact_context,
        "results": selected,
        "generated_at_utc": utc_now_iso(),
    }


def memory_stats(
    project_root: str,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    scope = _memory_scope(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    with _connect_memory_db(scope["db_path"]) as conn:
        _ensure_memory_schema(conn)
        aggregate = conn.execute(
            """
            SELECT COUNT(*) AS chunks, COALESCE(SUM(token_estimate), 0) AS tokens
            FROM memory_chunks
            WHERE project_key = ? AND context_key = ?
            """,
            (scope["project_key"], scope["context_key"]),
        ).fetchone()
        by_source = conn.execute(
            """
            SELECT source, COUNT(*) AS chunks, COALESCE(SUM(token_estimate), 0) AS tokens
            FROM memory_chunks
            WHERE project_key = ? AND context_key = ?
            GROUP BY source
            ORDER BY chunks DESC, source ASC
            """,
            (scope["project_key"], scope["context_key"]),
        ).fetchall()

    return {
        "status": "ok",
        "project_root": str(root),
        "context_key": scope["context_key"],
        "db_path": str(scope["db_path"]),
        "chunks": int(aggregate["chunks"]) if aggregate else 0,
        "token_estimate": int(aggregate["tokens"]) if aggregate else 0,
        "sources": [
            {
                "source": str(row["source"]),
                "chunks": int(row["chunks"]),
                "token_estimate": int(row["tokens"]),
            }
            for row in by_source
        ],
        "generated_at_utc": utc_now_iso(),
    }


def _copy_tree(src: Path, dst: Path, overwrite: bool) -> None:
    if dst.exists() and overwrite:
        shutil.rmtree(dst)
    if not dst.exists():
        shutil.copytree(src, dst)


def bootstrap_project(
    project_root: str,
    overwrite_agents: bool = False,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    profile = detect_project_profile(str(root))

    resolved_context = _resolve_context(
        root=root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    state_dir = _ensure_state_dir_writable(
        state_dir=resolved_context["state_dir"],
        root=root,
        context_key=resolved_context["context_key"],
    )

    profile_path = state_dir / "project-profile.json"
    _write_json(profile_path, profile)

    agents_src = _assets_agents_dir()
    agents_dst = state_dir / "agents"
    _copy_tree(agents_src, agents_dst, overwrite=overwrite_agents)

    metrics_dir = state_dir / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)

    metrics_log_path = metrics_dir / "test-metrics-log.md"
    report_path = metrics_dir / "ai-savings-report.md"
    timers_path = metrics_dir / "timers.json"

    if not metrics_log_path.exists():
        template = agents_dst / "TEST-METRICS-LOG-TEMPLATE.md"
        metrics_log_path.write_text(_safe_read(template), encoding="utf-8")

    if not report_path.exists():
        template = agents_dst / "AI-SAVINGS-REPORT-TEMPLATE.md"
        report_path.write_text(_safe_read(template), encoding="utf-8")

    if not timers_path.exists():
        _write_json(timers_path, {"timers": {}, "records": []})

    vars_payload = {
        "PROJECT_NAME": profile["project_name"],
        "SOLUTION_PATH": profile["solution_path"],
        "TEST_PROJECT_PATH": profile["default_test_project"],
        "TEST_FRAMEWORK": profile["test_frameworks"][0] if profile["test_frameworks"] else "unknown",
        "DOTNET_VERSION": profile["target_frameworks"][0] if profile["target_frameworks"] else "unknown",
        "COVERAGE_SETTINGS_PATH": profile["default_coverage_settings"],
        "COVERAGE_LINE_TARGET": profile["coverage_targets"]["line"],
        "COVERAGE_BRANCH_TARGET": profile["coverage_targets"]["branch"],
        "METRICS_LOG_PATH": _short(metrics_log_path, root),
        "SAVINGS_REPORT_PATH": _short(report_path, root),
        "BOOTSTRAPPED_AT_UTC": utc_now_iso(),
    }

    _write_json(state_dir / "variables.json", vars_payload)

    # Store context metadata for traceability across VSCode windows/developers.
    _write_json(
        state_dir / "context.json",
        {
            "context_key": resolved_context["context_key"],
            "context_id": resolved_context["context_id"],
            "developer_id": resolved_context["developer_id"],
            "workspace_id": resolved_context["workspace_id"],
            "mode": resolved_context["mode"],
            "config_path": resolved_context["config_path"],
            "project_root": str(root),
            "updated_at_utc": utc_now_iso(),
        },
    )

    memory_index = index_project_memory(
        project_root=str(root),
        include_agents=True,
        include_metrics=True,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    return {
        "status": "ok",
        "project_root": str(root),
        "state_dir": _short(state_dir, root),
        "state_dir_absolute": str(state_dir),
        "context_key": resolved_context["context_key"],
        "context_mode": resolved_context["mode"],
        "developer_id": resolved_context["developer_id"],
        "workspace_id": resolved_context["workspace_id"],
        "config_path": resolved_context["config_path"],
        "profile_path": _short(profile_path, root),
        "variables_path": _short(state_dir / "variables.json", root),
        "context_path": _short(state_dir / "context.json", root),
        "agents_path": _short(agents_dst, root),
        "metrics_log_path": _short(metrics_log_path, root),
        "savings_report_path": _short(report_path, root),
        "memory_db_path": memory_index.get("db_path"),
        "memory_indexed_sources": memory_index.get("indexed_sources"),
        "memory_token_estimate": sum(
            int(item.get("tokens_estimate", 0)) for item in memory_index.get("indexed", [])
        ),
        "execution_mode": "context_only" if bool(profile.get("virtual_project")) else "server_execution",
        "detected": profile,
    }


def _git_changed_files(root: Path, base_ref: str, include_untracked: bool) -> list[Path]:
    changed: set[Path] = set()

    diff_commands = [
        ["git", "-C", str(root), "diff", "--name-only", f"{base_ref}...HEAD", "--", "*.cs"],
        ["git", "-C", str(root), "diff", "--name-only", "HEAD", "--", "*.cs"],
    ]

    for cmd in diff_commands:
        result = _run_command(cmd, cwd=root, timeout=60)
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    changed.add((root / line).resolve())
            if changed:
                break

    if include_untracked:
        result = _run_command(
            ["git", "-C", str(root), "ls-files", "--others", "--exclude-standard", "--", "*.cs"],
            cwd=root,
            timeout=60,
        )
        if result.returncode == 0:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line:
                    changed.add((root / line).resolve())

    return sorted(changed)


def _looks_like_test_path(path: Path) -> bool:
    lower_parts = [p.lower() for p in path.parts]
    lower_name = path.name.lower()
    return (
        "test" in lower_name
        or any("test" in part for part in lower_parts)
        or lower_name.endswith("tests.cs")
        or lower_name.endswith("test.cs")
    )


def _parse_class_and_methods(content: str) -> tuple[list[dict[str, Any]], str | None]:
    namespace_match = NAMESPACE_PATTERN.search(content)
    namespace = namespace_match.group(1) if namespace_match else None

    classes: list[dict[str, Any]] = []
    class_matches = list(CLASS_PATTERN.finditer(content))

    for idx, class_match in enumerate(class_matches):
        access = class_match.group(1) or "internal"
        class_name = class_match.group(3)

        start = class_match.end()
        end = class_matches[idx + 1].start() if idx + 1 < len(class_matches) else len(content)
        class_block = content[start:end]

        methods: list[dict[str, Any]] = []
        for method_match in METHOD_PATTERN.finditer(class_block):
            visibility = method_match.group(1)
            return_type = method_match.group(2)
            method_name = method_match.group(3)
            parameters = method_match.group(4).strip()

            # Ignore obvious control statements accidentally matched.
            if method_name.lower() in {"if", "for", "while", "switch", "catch", "foreach"}:
                continue

            methods.append(
                {
                    "visibility": visibility,
                    "return_type": return_type,
                    "name": method_name,
                    "parameters": parameters,
                    "parameter_count": 0 if not parameters else len([p for p in parameters.split(",") if p.strip()]),
                }
            )

        classes.append(
            {
                "name": class_name,
                "access": access,
                "methods": methods,
            }
        )

    return classes, namespace


def _find_owning_project(file_path: Path, projects: list[Path]) -> Path | None:
    candidates = [p for p in projects if file_path.is_relative_to(p.parent)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: len(str(p.parent)), reverse=True)[0]


def _select_test_project_for_source(
    source_file: Path,
    source_project: Path | None,
    test_projects: list[Path],
) -> Path | None:
    if not test_projects:
        return None
    if len(test_projects) == 1:
        return test_projects[0]

    source_project_stem = source_project.stem.lower() if source_project else ""

    def score(test_project: Path) -> tuple[int, int, str]:
        test_stem = test_project.stem.lower()
        score_value = 0

        if source_project_stem and source_project_stem in test_stem:
            score_value += 5

        if source_project and test_project.parent.parent == source_project.parent.parent:
            score_value += 3

        source_top = source_file.parts[0].lower() if source_file.parts else ""
        test_top = test_project.parts[0].lower() if test_project.parts else ""
        if source_top and source_top == test_top:
            score_value += 1

        return (score_value, -len(str(test_project)), str(test_project))

    return sorted(test_projects, key=score, reverse=True)[0]


def _iter_source_files(root: Path, csproj_files: list[Path], test_projects: list[Path]) -> list[Path]:
    source_files: list[Path] = []
    test_roots = [project.parent.resolve() for project in test_projects]

    for file_path in _iter_files(root, ".cs"):
        if _looks_like_test_path(file_path):
            continue
        if any(file_path.is_relative_to(test_root) for test_root in test_roots):
            continue
        source_files.append(file_path)

    return sorted(source_files)


def _iter_test_code_files(root: Path, test_projects: list[Path]) -> list[Path]:
    candidates: set[Path] = set()
    if test_projects:
        search_roots = sorted({project.parent.resolve() for project in test_projects})
        for search_root in search_roots:
            for file_path in _iter_files(search_root, ".cs"):
                if _looks_like_test_path(file_path):
                    candidates.add(file_path)
    else:
        for file_path in _iter_files(root, ".cs"):
            if _looks_like_test_path(file_path):
                candidates.add(file_path)
    return sorted(candidates)


def _build_test_file_inventory(root: Path, test_projects: list[Path]) -> list[dict[str, Any]]:
    inventory: list[dict[str, Any]] = []
    for test_file in _iter_test_code_files(root, test_projects):
        content = _safe_read(test_file)
        inventory.append(
            {
                "path": test_file,
                "relative_path": _short(test_file, root),
                "name_lower": test_file.name.lower(),
                "content_lower": content.lower(),
            }
        )
    return inventory


def _candidate_test_matches(
    source_file: str,
    class_names: list[str],
    inventory: list[dict[str, Any]],
    target_test_project: str | None = None,
) -> list[dict[str, Any]]:
    source_stem = Path(source_file).stem.lower()
    class_tokens = [name.lower() for name in class_names if name]
    target_root = Path(target_test_project).parent.as_posix().lower() if target_test_project else ""

    matches: list[tuple[int, dict[str, Any]]] = []
    for entry in inventory:
        score = 0
        relative_path = str(entry["relative_path"]).lower()
        name_lower = str(entry["name_lower"])
        content_lower = str(entry["content_lower"])

        if target_root and relative_path.startswith(target_root):
            score += 3
        if source_stem and source_stem in name_lower:
            score += 4
        for token in class_tokens:
            if token in name_lower:
                score += 6
            if token in content_lower:
                score += 2
        if score > 0:
            matches.append((score, entry))

    matches.sort(key=lambda item: (item[0], len(str(item[1]["relative_path"]))), reverse=True)
    seen_paths: set[str] = set()
    unique_matches: list[dict[str, Any]] = []
    for _, entry in matches:
        rel = str(entry["relative_path"])
        if rel in seen_paths:
            continue
        seen_paths.add(rel)
        unique_matches.append(entry)
    return unique_matches


def _covered_method_names(method_names: list[str], matches: list[dict[str, Any]]) -> list[str]:
    covered: list[str] = []
    for method_name in method_names:
        token = method_name.lower()
        if any(re.search(rf"\b{re.escape(token)}\b", str(entry["content_lower"])) for entry in matches):
            covered.append(method_name)
    return covered


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    lowered = str(value).strip().lower()
    return lowered in {"1", "true", "yes", "y", "changed"}


def _snapshot_files(snapshot_payload: Any) -> list[dict[str, Any]]:
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
        entries.append(
            {
                "path": normalized_path,
                "content": str(raw_entry.get("content", "")),
                "summary": str(raw_entry.get("summary", "")).strip(),
                "kind": str(raw_entry.get("kind", "")).strip().lower(),
                "changed": _as_bool(raw_entry.get("changed")),
                "raw": raw_entry,
            }
        )
    return entries


def scan_snapshot_test_debt_lightweight(snapshot_payload: Any) -> dict[str, Any]:
    files = _snapshot_files(snapshot_payload)
    source_entries = [
        entry
        for entry in files
        if entry["path"].lower().endswith(".cs") and not _looks_like_test_path(Path(entry["path"]))
    ]
    test_inventory = [
        {
            "relative_path": entry["path"],
            "name_lower": Path(entry["path"]).name.lower(),
            "content_lower": entry["content"].lower(),
        }
        for entry in files
        if entry["path"].lower().endswith(".cs") and _looks_like_test_path(Path(entry["path"]))
    ]
    changed_files = {
        entry["path"]
        for entry in source_entries
        if entry["changed"]
    }

    files_summary: list[dict[str, Any]] = []
    without_any_tests = 0
    without_total_coverage = 0
    changed_needing_tests = 0

    for entry in sorted(source_entries, key=lambda item: item["path"]):
        content = entry["content"]
        classes, namespace = _parse_class_and_methods(content)
        public_classes = [item for item in classes if item["access"] == "public" and item["methods"]]
        if not public_classes:
            continue

        source_relative = entry["path"]
        class_names = [item["name"] for item in public_classes]
        public_methods = sorted(
            {
                str(method["name"])
                for class_entry in public_classes
                for method in class_entry["methods"]
                if str(method.get("name", "")).strip()
            }
        )
        matches = _candidate_test_matches(
            source_file=source_relative,
            class_names=class_names,
            inventory=test_inventory,
            target_test_project=None,
        )
        covered_methods = _covered_method_names(public_methods, matches)

        if not matches:
            status = "no_tests"
        elif len(covered_methods) < len(public_methods):
            status = "partial_tests"
        else:
            status = "covered"

        if status == "no_tests":
            without_any_tests += 1
        if status != "covered":
            without_total_coverage += 1
        if source_relative in changed_files and status != "covered":
            changed_needing_tests += 1

        files_summary.append(
            {
                "source_file": source_relative,
                "namespace": namespace,
                "source_project": None,
                "target_test_project": None,
                "changed": source_relative in changed_files,
                "status": status,
                "public_classes": class_names,
                "public_method_count": len(public_methods),
                "covered_method_count": len(covered_methods),
                "covered_methods": covered_methods,
                "missing_methods": [name for name in public_methods if name not in covered_methods],
                "matching_test_files": [str(match["relative_path"]) for match in matches[:8]],
                "matching_test_files_count": len(matches),
                "latest_known_line_rate": None,
                "matched_coverage_file": None,
            }
        )

    files_summary.sort(key=lambda item: (item["status"], item["source_file"]))

    return {
        "project_root": None,
        "base_ref": "snapshot",
        "include_untracked": True,
        "scan_mode": "snapshot",
        "snapshot_files_indexed": len(files),
        "total_testable_files": len(files_summary),
        "changed_files_considered": len(changed_files),
        "changed_files_needing_tests": changed_needing_tests,
        "files_without_any_tests": without_any_tests,
        "files_without_total_test_coverage": without_total_coverage,
        "files_with_total_test_coverage": len(files_summary) - without_total_coverage,
        "test_projects_found": 0,
        "test_files_indexed": len(test_inventory),
        "files": files_summary,
        "generated_at_utc": utc_now_iso(),
    }


def scan_test_debt_lightweight(
    project_root: str,
    base_ref: str = "HEAD~1",
    include_untracked: bool = True,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    csproj_files = _iter_files(root, ".csproj")
    test_projects = [path for path in csproj_files if _is_test_project(path)]
    changed_files = {
        _short(path, root)
        for path in _git_changed_files(root, base_ref=base_ref, include_untracked=include_untracked)
        if path.exists() and path.suffix.lower() == ".cs" and not _looks_like_test_path(path)
    }
    latest_coverage = _latest_coverage_file(root)
    coverage_rates = _parse_cobertura_line_rates(latest_coverage) if latest_coverage else {}
    inventory = _build_test_file_inventory(root, test_projects)

    files_summary: list[dict[str, Any]] = []
    without_any_tests = 0
    without_total_coverage = 0
    changed_needing_tests = 0

    for file_path in _iter_source_files(root, csproj_files, test_projects):
        content = _safe_read(file_path)
        classes, namespace = _parse_class_and_methods(content)
        public_classes = [item for item in classes if item["access"] == "public" and item["methods"]]
        if not public_classes:
            continue

        source_relative = _short(file_path, root)
        source_project = _find_owning_project(file_path, csproj_files)
        target_test_project = _select_test_project_for_source(
            file_path.relative_to(root),
            source_project,
            test_projects,
        )
        class_names = [item["name"] for item in public_classes]
        public_methods = sorted(
            {
                str(method["name"])
                for class_entry in public_classes
                for method in class_entry["methods"]
                if str(method.get("name", "")).strip()
            }
        )
        matches = _candidate_test_matches(
            source_file=source_relative,
            class_names=class_names,
            inventory=inventory,
            target_test_project=_short(target_test_project, root) if target_test_project else None,
        )
        covered_methods = _covered_method_names(public_methods, matches)
        coverage_match, line_rate = _match_coverage_file(source_relative, coverage_rates) if coverage_rates else (None, None)

        if not matches:
            status = "no_tests"
        elif len(covered_methods) < len(public_methods):
            status = "partial_tests"
        else:
            status = "covered"

        if line_rate is not None and line_rate < 1.0 and status == "covered":
            status = "coverage_below_total"

        if status == "no_tests":
            without_any_tests += 1
        if status != "covered":
            without_total_coverage += 1
        if source_relative in changed_files and status != "covered":
            changed_needing_tests += 1

        files_summary.append(
            {
                "source_file": source_relative,
                "namespace": namespace,
                "source_project": _short(source_project, root) if source_project else None,
                "target_test_project": _short(target_test_project, root) if target_test_project else None,
                "changed": source_relative in changed_files,
                "status": status,
                "public_classes": class_names,
                "public_method_count": len(public_methods),
                "covered_method_count": len(covered_methods),
                "covered_methods": covered_methods,
                "missing_methods": [name for name in public_methods if name not in covered_methods],
                "matching_test_files": [str(entry["relative_path"]) for entry in matches[:8]],
                "matching_test_files_count": len(matches),
                "latest_known_line_rate": round(line_rate, 4) if line_rate is not None else None,
                "matched_coverage_file": coverage_match,
            }
        )

    files_summary.sort(key=lambda item: (item["status"], item["source_file"]))

    return {
        "project_root": str(root),
        "base_ref": base_ref,
        "include_untracked": include_untracked,
        "scan_mode": "server_files",
        "total_testable_files": len(files_summary),
        "changed_files_considered": len(changed_files),
        "changed_files_needing_tests": changed_needing_tests,
        "files_without_any_tests": without_any_tests,
        "files_without_total_test_coverage": without_total_coverage,
        "files_with_total_test_coverage": len(files_summary) - without_total_coverage,
        "test_projects_found": len(test_projects),
        "test_files_indexed": len(inventory),
        "files": files_summary,
        "generated_at_utc": utc_now_iso(),
    }


def discover_changes(
    project_root: str,
    base_ref: str = "HEAD~1",
    include_untracked: bool = True,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    csproj_files = _iter_files(root, ".csproj")
    test_projects = [p for p in csproj_files if _is_test_project(p)]

    changed_files = _git_changed_files(root, base_ref=base_ref, include_untracked=include_untracked)

    source_files = [
        p for p in changed_files if p.exists() and p.suffix.lower() == ".cs" and not _looks_like_test_path(p)
    ]

    results: list[dict[str, Any]] = []

    for file_path in source_files:
        content = _safe_read(file_path)
        classes, namespace = _parse_class_and_methods(content)
        source_project = _find_owning_project(file_path, csproj_files)
        test_project = _select_test_project_for_source(file_path.relative_to(root), source_project, test_projects)

        classes_out: list[dict[str, Any]] = []
        for class_data in classes:
            # Prioritize public classes for cross-assembly test generation.
            if class_data["access"] != "public":
                continue
            if not class_data["methods"]:
                continue
            classes_out.append(class_data)

        if not classes_out:
            continue

        results.append(
            {
                "source_file": _short(file_path, root),
                "source_project": _short(source_project, root) if source_project else None,
                "target_test_project": _short(test_project, root) if test_project else None,
                "namespace": namespace,
                "classes": classes_out,
            }
        )

    return {
        "project_root": str(root),
        "base_ref": base_ref,
        "changed_files_count": len(changed_files),
        "source_files_count": len(source_files),
        "testable_files_count": len(results),
        "files": results,
        "generated_at_utc": utc_now_iso(),
    }


def _sanitize_identifier(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9_]", "_", value)
    if not sanitized:
        return "Item"
    if sanitized[0].isdigit():
        return f"_{sanitized}"
    return sanitized


def _infer_test_namespace(test_project_path: Path, source_namespace: str | None) -> str:
    test_project_content = _safe_read(test_project_path)
    root_namespace = _parse_tag(test_project_content, "RootNamespace")
    if root_namespace:
        return f"{root_namespace}.AutoGenerated"
    if source_namespace:
        return f"{source_namespace}.AutoGeneratedTests"
    return f"{test_project_path.stem}.AutoGenerated"


def _build_auto_test_content(
    test_namespace: str,
    source_namespace: str | None,
    class_name: str,
    methods: list[dict[str, Any]],
    source_file: str,
) -> str:
    lines: list[str] = []
    lines.append("using System.Reflection;")
    lines.append("using Xunit;")
    if source_namespace:
        lines.append(f"using {source_namespace};")
    lines.append("")
    lines.append(f"namespace {test_namespace};")
    lines.append("")
    lines.append("/// <summary>")
    lines.append("/// Auto-generated baseline tests.")
    lines.append("/// File generated by digital-solutions-test-mcp.")
    lines.append(f"/// Source: {source_file}")
    lines.append("/// </summary>")
    lines.append(f"public class {class_name}AutoTests")
    lines.append("{")

    for index, method in enumerate(methods, start=1):
        method_name = _sanitize_identifier(method["name"])
        lines.append("    [Fact]")
        lines.append(
            f"    public void {method_name}_ShouldExist_AutoGenerated_{index:03d}()"
        )
        lines.append("    {")
        lines.append(
            f"        var method = typeof({class_name}).GetMethod(\"{method['name']}\", "
            "BindingFlags.Public | BindingFlags.Instance | BindingFlags.Static);"
        )
        lines.append("        Assert.NotNull(method);")
        lines.append("    }")
        lines.append("")

    if methods:
        lines.pop()  # remove trailing empty line

    lines.append("}")
    lines.append("")

    return "\n".join(lines)


def generate_tests_for_changes(
    project_root: str,
    base_ref: str = "HEAD~1",
    dry_run: bool = False,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    change_map = discover_changes(project_root=str(root), base_ref=base_ref, include_untracked=True)

    generated: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for file_entry in change_map["files"]:
        target_test_project = file_entry.get("target_test_project")
        if not target_test_project:
            skipped.append(
                {
                    "source_file": file_entry["source_file"],
                    "reason": "No target test project could be inferred",
                }
            )
            continue

        test_project_path = (root / target_test_project).resolve()
        auto_dir = test_project_path.parent / "AutoGenerated"
        auto_dir.mkdir(parents=True, exist_ok=True)

        for class_entry in file_entry["classes"]:
            class_name = class_entry["name"]
            methods = class_entry["methods"]
            if not methods:
                continue

            test_namespace = _infer_test_namespace(test_project_path, file_entry.get("namespace"))
            output_file = auto_dir / f"{class_name}AutoTests.cs"
            content = _build_auto_test_content(
                test_namespace=test_namespace,
                source_namespace=file_entry.get("namespace"),
                class_name=class_name,
                methods=methods,
                source_file=file_entry["source_file"],
            )

            if not dry_run:
                output_file.write_text(content, encoding="utf-8")

            generated.append(
                {
                    "source_file": file_entry["source_file"],
                    "class_name": class_name,
                    "methods_count": len(methods),
                    "output_file": _short(output_file, root),
                    "test_project": target_test_project,
                }
            )

    return {
        "project_root": str(root),
        "base_ref": base_ref,
        "dry_run": dry_run,
        "generated_count": len(generated),
        "generated": generated,
        "skipped": skipped,
        "generated_at_utc": utc_now_iso(),
    }


def _find_profile_path(
    root: Path,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> Path:
    return _mcp_state_dir(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    ) / "project-profile.json"


def _load_profile(
    root: Path,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    profile_path = _find_profile_path(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    if profile_path.exists():
        return _read_json(profile_path)
    return detect_project_profile(str(root))


def _latest_coverage_file(root: Path) -> Path | None:
    candidates = list(root.rglob("coverage.cobertura.xml"))
    candidates = [p for p in candidates if p.is_file() and not _is_excluded(p)]
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: p.stat().st_mtime, reverse=True)[0]


def run_validation(
    project_root: str,
    run_coverage: bool = True,
    configuration: str = "Debug",
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    profile = _load_profile(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )

    solution_path = profile.get("solution_path")
    test_project = profile.get("default_test_project")
    coverage_settings = profile.get("default_coverage_settings")

    if not solution_path and not test_project:
        raise RuntimeError("No solution or test project found to validate")

    command_results: list[dict[str, Any]] = []

    if solution_path:
        build_cmd = ["dotnet", "build", solution_path, "-c", configuration]
        build_res = _run_command(build_cmd, cwd=root)
        command_results.append(build_res.as_dict())
        if build_res.returncode != 0:
            return {
                "status": "failed",
                "phase": "build",
                "results": command_results,
                "coverage_file": None,
            }

    if test_project:
        test_cmd = ["dotnet", "test", test_project, "-c", configuration, "--no-build"]
    else:
        test_cmd = ["dotnet", "test", solution_path, "-c", configuration, "--no-build"]

    test_res = _run_command(test_cmd, cwd=root)
    command_results.append(test_res.as_dict())
    if test_res.returncode != 0:
        return {
            "status": "failed",
            "phase": "test",
            "results": command_results,
            "coverage_file": None,
        }

    coverage_file: Path | None = None
    if run_coverage:
        coverage_cmd = [
            "dotnet",
            "test",
            test_project or solution_path,
            "-c",
            configuration,
            "--no-build",
            '--collect:XPlat Code Coverage',
        ]
        if coverage_settings:
            coverage_cmd.extend(["--settings", coverage_settings])

        cov_res = _run_command(coverage_cmd, cwd=root)
        command_results.append(cov_res.as_dict())
        coverage_file = _latest_coverage_file(root)

    success = all(item["returncode"] == 0 for item in command_results)
    return {
        "status": "ok" if success else "failed",
        "phase": "complete" if success else "validation",
        "results": command_results,
        "coverage_file": _short(coverage_file, root) if coverage_file else None,
    }


def _parse_cobertura_line_rates(cobertura_path: Path) -> dict[str, float]:
    tree = ET.parse(cobertura_path)
    root = tree.getroot()

    # coverlet/cobertura stores per class filename; aggregate per file using line hits.
    per_file: dict[str, dict[str, int]] = {}

    for class_elem in root.findall(".//class"):
        filename = class_elem.attrib.get("filename")
        if not filename:
            continue

        line_nodes = class_elem.findall("./lines/line")
        if not line_nodes:
            continue

        file_stats = per_file.setdefault(filename.replace("\\", "/"), {"covered": 0, "total": 0})
        for line in line_nodes:
            hits_raw = line.attrib.get("hits", "0")
            hits = int(hits_raw) if hits_raw.isdigit() else 0
            file_stats["total"] += 1
            if hits > 0:
                file_stats["covered"] += 1

    rates: dict[str, float] = {}
    for file_name, stats in per_file.items():
        total = stats["total"]
        covered = stats["covered"]
        rates[file_name] = (covered / total) if total else 0.0

    return rates


def _match_coverage_file(changed_file: str, coverage_files: dict[str, float]) -> tuple[str | None, float | None]:
    changed_normalized = changed_file.replace("\\", "/").lower()
    best_key: str | None = None

    for cov_file in coverage_files.keys():
        cov_normalized = cov_file.replace("\\", "/").lower()
        if changed_normalized.endswith(cov_normalized) or cov_normalized.endswith(changed_normalized):
            if best_key is None or len(cov_file) > len(best_key):
                best_key = cov_file

    if best_key is None:
        return None, None

    return best_key, coverage_files[best_key]


def enforce_changed_coverage(
    project_root: str,
    base_ref: str = "HEAD~1",
    min_line_rate: float = 1.0,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()

    validation = run_validation(
        project_root=str(root),
        run_coverage=True,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    if validation["status"] != "ok":
        return {
            "status": "failed",
            "reason": "Validation failed before coverage gate",
            "validation": validation,
        }

    coverage_file_rel = validation.get("coverage_file")
    if not coverage_file_rel:
        return {
            "status": "failed",
            "reason": "Coverage file not found",
            "validation": validation,
        }

    coverage_file = (root / coverage_file_rel).resolve()
    rates = _parse_cobertura_line_rates(coverage_file)

    changes = discover_changes(project_root=str(root), base_ref=base_ref, include_untracked=True)
    touched = [entry["source_file"] for entry in changes["files"]]

    details: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for changed_file in touched:
        matched_file, rate = _match_coverage_file(changed_file, rates)
        if rate is None:
            record = {
                "file": changed_file,
                "matched_coverage_file": None,
                "line_rate": None,
                "min_line_rate": min_line_rate,
                "status": "missing",
            }
            details.append(record)
            failed.append(record)
            continue

        status = "pass" if rate >= min_line_rate else "fail"
        record = {
            "file": changed_file,
            "matched_coverage_file": matched_file,
            "line_rate": round(rate, 4),
            "min_line_rate": min_line_rate,
            "status": status,
        }
        details.append(record)
        if status != "pass":
            failed.append(record)

    return {
        "status": "ok" if not failed else "failed",
        "base_ref": base_ref,
        "min_line_rate": min_line_rate,
        "coverage_file": coverage_file_rel,
        "checked_files": len(touched),
        "failed_files": len(failed),
        "details": details,
        "validation": validation,
    }


def start_test_timer(
    project_root: str,
    test_case_id: str,
    feature: str,
    test_name: str,
    complexity: str,
    test_type: str,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    state_dir = _mcp_state_dir(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    timers_path = state_dir / "metrics" / "timers.json"
    payload = _read_json(timers_path, default={"timers": {}, "records": []})

    now = utc_now_iso()
    payload.setdefault("timers", {})[test_case_id] = {
        "test_case_id": test_case_id,
        "feature": feature,
        "test_name": test_name,
        "complexity": complexity.upper(),
        "test_type": test_type,
        "start_time_utc": now,
    }
    _write_json(timers_path, payload)

    return {
        "status": "ok",
        "test_case_id": test_case_id,
        "start_time_utc": now,
        "timers_path": _short(timers_path, root),
    }


def _baseline_for_complexity(complexity: str) -> int:
    lookup = {"S": 20, "M": 45, "L": 90}
    return lookup.get(complexity.upper(), 45)


def _ensure_metrics_log(path: Path) -> None:
    if path.exists():
        return

    lines = [
        "# Test Metrics Log",
        "",
        TABLE_HEADER,
        "| --- | --- | --- | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def stop_test_timer(
    project_root: str,
    test_case_id: str,
    status: str = "PASS",
    notes: str = "",
    baseline_manual_minutes: int | None = None,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    state_dir = _mcp_state_dir(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    timers_path = state_dir / "metrics" / "timers.json"
    metrics_log_path = state_dir / "metrics" / "test-metrics-log.md"

    payload = _read_json(timers_path, default={"timers": {}, "records": []})
    timer = payload.get("timers", {}).get(test_case_id)
    if not timer:
        raise KeyError(f"Timer not found for test_case_id: {test_case_id}")

    end_dt = datetime.now(timezone.utc)
    start_dt = datetime.fromisoformat(timer["start_time_utc"].replace("Z", "+00:00"))

    actual_minutes = max(1, math.ceil((end_dt - start_dt).total_seconds() / 60))
    baseline = baseline_manual_minutes or _baseline_for_complexity(timer.get("complexity", "M"))

    savings_minutes = baseline - actual_minutes
    savings_percent = (savings_minutes / baseline * 100) if baseline else 0
    productivity_ratio = (baseline / actual_minutes) if actual_minutes else 0

    record = {
        "TEST_CASE_ID": test_case_id,
        "FEATURE": timer.get("feature", ""),
        "TEST_NAME": timer.get("test_name", ""),
        "TYPE": timer.get("test_type", ""),
        "COMPLEXITY": timer.get("complexity", "M"),
        "START_TIME_UTC": timer["start_time_utc"],
        "END_TIME_UTC": end_dt.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "ACTUAL_MINUTES": actual_minutes,
        "BASELINE_MANUAL_MINUTES": baseline,
        "SAVINGS_MINUTES": savings_minutes,
        "SAVINGS_PERCENT": round(savings_percent, 2),
        "PRODUCTIVITY_RATIO": round(productivity_ratio, 2),
        "STATUS": status.upper(),
        "NOTES": notes,
    }

    payload.setdefault("records", []).append(record)
    payload.get("timers", {}).pop(test_case_id, None)
    _write_json(timers_path, payload)

    _ensure_metrics_log(metrics_log_path)

    row = (
        f"| {record['TEST_CASE_ID']} | {record['FEATURE']} | {record['TEST_NAME']} | {record['TYPE']} | "
        f"{record['COMPLEXITY']} | {record['START_TIME_UTC']} | {record['END_TIME_UTC']} | "
        f"{record['ACTUAL_MINUTES']} | {record['BASELINE_MANUAL_MINUTES']} | {record['SAVINGS_MINUTES']} | "
        f"{record['SAVINGS_PERCENT']:.2f} | {record['PRODUCTIVITY_RATIO']:.2f} | {record['STATUS']} | "
        f"{record['NOTES']} |"
    )
    with metrics_log_path.open("a", encoding="utf-8") as handle:
        handle.write(row + "\n")

    return {
        "status": "ok",
        "record": record,
        "metrics_log_path": _short(metrics_log_path, root),
        "timers_path": _short(timers_path, root),
    }


def summarize_metrics(
    project_root: str,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    timers_path = _mcp_state_dir(
        root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    ) / "metrics" / "timers.json"
    payload = _read_json(timers_path, default={"timers": {}, "records": []})

    records = payload.get("records", [])
    pass_records = [r for r in records if r.get("STATUS") == "PASS"]

    total_baseline = sum(int(r.get("BASELINE_MANUAL_MINUTES", 0)) for r in pass_records)
    total_actual = sum(int(r.get("ACTUAL_MINUTES", 0)) for r in pass_records)
    total_savings = total_baseline - total_actual

    savings_percent = (total_savings / total_baseline * 100) if total_baseline else 0.0
    avg_ratio = (total_baseline / total_actual) if total_actual else 0.0

    return {
        "status": "ok",
        "total_records": len(records),
        "pass_records": len(pass_records),
        "total_baseline_minutes": total_baseline,
        "total_actual_minutes": total_actual,
        "total_savings_minutes": total_savings,
        "total_savings_hours": round(total_savings / 60, 2),
        "total_savings_percent": round(savings_percent, 2),
        "avg_productivity_ratio": round(avg_ratio, 2),
        "open_timers": len(payload.get("timers", {})),
        "generated_at_utc": utc_now_iso(),
    }


def resolve_context_state(
    project_root: str,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve()
    resolved = _resolve_context(
        root=root,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    state_dir = _ensure_state_dir_writable(
        state_dir=resolved["state_dir"],
        root=root,
        context_key=resolved["context_key"],
    )

    return {
        "status": "ok",
        "project_root": str(root),
        "state_dir": _short(state_dir, root),
        "state_dir_absolute": str(state_dir),
        "context_key": resolved["context_key"],
        "context_id": resolved["context_id"],
        "developer_id": resolved["developer_id"],
        "workspace_id": resolved["workspace_id"],
        "mode": resolved["mode"],
        "config_path": resolved["config_path"],
        "generated_at_utc": utc_now_iso(),
    }


def runtime_settings(
    project_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    root = Path(_normalize_fs_path(project_root)).resolve() if project_root else None
    settings = _load_toml_settings(project_root=root, config_toml_path=config_toml_path)
    if not settings:
        return {
            "status": "ok",
            "settings_found": False,
            "settings": {},
        }
    return {
        "status": "ok",
        "settings_found": True,
        "settings": settings,
    }


def list_context_states(
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    settings = _load_toml_settings(project_root=None, config_toml_path=config_toml_path)
    context_settings = settings.get("context", {}) if isinstance(settings, dict) else {}

    resolved_context_root = (
        context_root
        or os.getenv("DIGITAL_SOLUTIONS_CONTEXT_ROOT", "").strip()
        or context_settings.get("store_root")
        or ""
    )

    if not resolved_context_root:
        return {
            "status": "ok",
            "contexts_found": 0,
            "context_root": None,
            "contexts": [],
            "message": "No context_root defined in arguments, env, or TOML settings.",
        }

    base_root = Path(_normalize_fs_path(str(resolved_context_root))).expanduser().resolve()
    if not base_root.exists():
        return {
            "status": "ok",
            "contexts_found": 0,
            "context_root": str(base_root),
            "contexts": [],
        }

    contexts: list[dict[str, Any]] = []
    for entry in sorted(base_root.iterdir()):
        if not entry.is_dir():
            continue
        context_file = entry / "context.json"
        context_data = _read_json(context_file, default={}) if context_file.exists() else {}
        contexts.append(
            {
                "context_key": entry.name,
                "path": str(entry),
                "project_root": context_data.get("project_root"),
                "developer_id": context_data.get("developer_id"),
                "workspace_id": context_data.get("workspace_id"),
                "updated_at_utc": context_data.get("updated_at_utc"),
            }
        )

    return {
        "status": "ok",
        "contexts_found": len(contexts),
        "context_root": str(base_root),
        "contexts": contexts,
    }


def read_agent_file(agent_file_name: str) -> dict[str, Any]:
    agents_dir = _assets_agents_dir()
    requested = (agents_dir / agent_file_name).resolve()

    if not requested.exists() or not requested.is_file() or agents_dir not in requested.parents:
        raise FileNotFoundError(f"Agent file not found: {agent_file_name}")

    return {
        "status": "ok",
        "agent_file": requested.name,
        "content": _safe_read(requested),
    }


def auto_pipeline(
    project_root: str,
    base_ref: str = "HEAD~1",
    min_line_rate: float = 1.0,
    context_id: str | None = None,
    developer_id: str | None = None,
    workspace_id: str | None = None,
    context_root: str | None = None,
    config_toml_path: str | None = None,
) -> dict[str, Any]:
    boot = bootstrap_project(
        project_root=project_root,
        overwrite_agents=False,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    changes = discover_changes(project_root=project_root, base_ref=base_ref, include_untracked=True)
    generated = generate_tests_for_changes(project_root=project_root, base_ref=base_ref, dry_run=False)
    coverage_gate = enforce_changed_coverage(
        project_root=project_root,
        base_ref=base_ref,
        min_line_rate=min_line_rate,
        context_id=context_id,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_root=context_root,
        config_toml_path=config_toml_path,
    )
    memory = index_project_memory(
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
        "status": "ok" if coverage_gate.get("status") == "ok" else "failed",
        "bootstrap": boot,
        "changes": changes,
        "generated": generated,
        "coverage_gate": coverage_gate,
        "memory": memory,
        "generated_at_utc": utc_now_iso(),
    }
