from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone
from hashlib import sha1
from pathlib import Path
from typing import Any

from .core import detect_project_profile

DEFAULT_CONFIG_RELATIVE_PATH = ".ai-test-mcp/hook-config.toml"
DEFAULT_MAX_FILES = 24
DEFAULT_MAX_CONTENT_CHARS = 12000


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, cwd=str(cwd), text=True, capture_output=True, check=False)


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="latin-1")


def _git_output(root: Path, command: list[str]) -> str:
    result = _run(command, cwd=root)
    return result.stdout.strip() if result.returncode == 0 else ""


def _git_root(project_root: str) -> Path:
    root = Path(project_root).resolve()
    result = _run(["git", "rev-parse", "--show-toplevel"], cwd=root)
    if result.returncode != 0:
        raise RuntimeError(f"Unable to resolve git root: {result.stderr.strip() or result.stdout.strip()}")
    return Path(result.stdout.strip()).resolve()


def _resolve_config_path(root: Path, raw_path: str) -> Path:
    if raw_path.strip():
        candidate = Path(raw_path)
        return candidate if candidate.is_absolute() else (root / candidate)
    return root / DEFAULT_CONFIG_RELATIVE_PATH


def _relative_posix(root: Path, path: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


def _is_test_path(relative_path: str) -> bool:
    lowered = relative_path.replace("\\", "/").lower()
    parts = [part for part in lowered.split("/") if part]
    name = parts[-1] if parts else lowered
    return (
        lowered.endswith("tests.cs")
        or lowered.endswith("test.cs")
        or any(part.endswith(".tests") for part in parts)
        or any(part == "tests" for part in parts)
        or any("test" in part for part in parts[:-1])
        or ".tests/" in lowered
        or "/test/" in lowered
        or name.endswith("tests.cs")
        or name.endswith("test.cs")
    )


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as handle:
        payload = tomllib.load(handle)
    return payload if isinstance(payload, dict) else {}


def _resolve_value(cli_value: Any, config_value: Any, env_name: str, default: Any = "") -> Any:
    env_value = os.getenv(env_name, "").strip()
    if cli_value not in {None, ""}:
        return cli_value
    if env_value:
        return env_value
    if config_value not in {None, ""}:
        return config_value
    return default


def _resolve_bool(cli_value: bool | None, config_value: Any, env_name: str, default: bool) -> bool:
    env_value = os.getenv(env_name, "").strip().lower()
    if cli_value is not None:
        return cli_value
    if env_value:
        return env_value in {"1", "true", "yes", "on"}
    if isinstance(config_value, bool):
        return config_value
    if config_value not in {None, ""}:
        return str(config_value).strip().lower() in {"1", "true", "yes", "on"}
    return default


def _load_hook_config(path: Path) -> dict[str, Any]:
    config = _load_toml(path)
    section = config.get("workspace_hook", {}) if isinstance(config, dict) else {}
    return section if isinstance(section, dict) else {}


def _git_branch_context(root: Path) -> dict[str, Any]:
    branch_name = _git_output(root, ["git", "rev-parse", "--abbrev-ref", "HEAD"]) or "HEAD"
    head_sha = _git_output(root, ["git", "rev-parse", "HEAD"])
    upstream_branch = _git_output(root, ["git", "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    upstream_sha = _git_output(root, ["git", "rev-parse", "@{u}"]) if upstream_branch else ""
    ahead = 0
    behind = 0
    if upstream_branch:
        counts_raw = _git_output(root, ["git", "rev-list", "--left-right", "--count", f"HEAD...{upstream_branch}"])
        parts = counts_raw.split()
        if len(parts) == 2:
            try:
                ahead = int(parts[0])
                behind = int(parts[1])
            except ValueError:
                ahead = 0
                behind = 0
    dirty = bool(_git_output(root, ["git", "status", "--porcelain"]))
    detached = branch_name == "HEAD"
    return {
        "branch_name": branch_name,
        "head_sha": head_sha,
        "upstream_branch": upstream_branch or None,
        "upstream_sha": upstream_sha or None,
        "ahead_count": ahead,
        "behind_count": behind,
        "working_tree_dirty": dirty,
        "detached_head": detached,
        "observed_at_utc": _utc_now_iso(),
    }


def _git_changed_files(root: Path, staged_only: bool, include_working_tree: bool) -> list[str]:
    changed: set[str] = set()
    if staged_only:
        staged = _run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd=root)
        if staged.returncode == 0:
            changed.update(line.strip() for line in staged.stdout.splitlines() if line.strip())

    if include_working_tree:
        working = _run(["git", "diff", "--name-only", "--diff-filter=ACMR"], cwd=root)
        if working.returncode == 0:
            changed.update(line.strip() for line in working.stdout.splitlines() if line.strip())
        untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root)
        if untracked.returncode == 0:
            changed.update(line.strip() for line in untracked.stdout.splitlines() if line.strip())

    return sorted(path.replace("\\", "/") for path in changed if path.strip())


def _staged_content(root: Path, relative_path: str) -> str | None:
    result = _run(["git", "show", f":{relative_path}"], cwd=root)
    if result.returncode != 0:
        return None
    return result.stdout


def _file_content(root: Path, relative_path: str, prefer_staged: bool) -> str:
    if prefer_staged:
        staged = _staged_content(root, relative_path)
        if staged is not None:
            return staged
    file_path = root / relative_path
    if file_path.exists():
        return _safe_read(file_path)
    staged = _staged_content(root, relative_path)
    return staged or ""


def _find_related_test_files(root: Path, changed_source_files: list[str], changed_test_files: list[str]) -> list[str]:
    related: set[str] = {path for path in changed_test_files}
    for source_file in changed_source_files:
        stem = Path(source_file).stem
        for candidate_name in (f"{stem}Tests.cs", f"{stem}Test.cs"):
            for path in root.rglob(candidate_name):
                if path.is_file():
                    related.add(_relative_posix(root, path))
    return sorted(related)


def _trim_content(content: str, max_chars: int) -> str:
    normalized = content.rstrip()
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "\n// ... truncated by workspace hook ..."


def _project_manifest(root: Path) -> dict[str, Any]:
    try:
        profile = detect_project_profile(str(root))
    except Exception:
        return {
            "project_name": root.name,
            "solution_path": "",
            "default_test_project": "",
            "test_frameworks": [],
            "target_frameworks": [],
            "coverage_targets": {},
        }
    return {
        "project_name": profile.get("project_name"),
        "solution_path": profile.get("solution_path"),
        "default_test_project": profile.get("default_test_project"),
        "test_frameworks": profile.get("test_frameworks"),
        "target_frameworks": profile.get("target_frameworks"),
        "coverage_targets": profile.get("coverage_targets"),
    }


def _build_change_payload(
    root: Path,
    intent: str,
    staged_only: bool,
    include_working_tree: bool,
    max_files: int,
    max_content_chars: int,
    developer_id: str,
    workspace_id: str,
    context_id: str,
    notes: str,
    change_source: str,
) -> dict[str, Any]:
    changed_files = _git_changed_files(root, staged_only=staged_only, include_working_tree=include_working_tree)
    changed_cs = [path for path in changed_files if path.lower().endswith(".cs")]
    changed_source_files = [path for path in changed_cs if not _is_test_path(path)]
    changed_test_files = [path for path in changed_cs if _is_test_path(path)]
    related_test_files = _find_related_test_files(root, changed_source_files, changed_test_files)

    selected_files: list[str] = []
    for path in changed_source_files + changed_test_files + related_test_files:
        if path not in selected_files:
            selected_files.append(path)
    selected_files = selected_files[:max_files]

    snapshot_files: list[dict[str, Any]] = []
    for relative_path in selected_files:
        kind = "test" if _is_test_path(relative_path) else "source"
        snapshot_files.append(
            {
                "path": relative_path,
                "kind": kind,
                "changed": relative_path in changed_files,
                "content": _trim_content(
                    _file_content(root, relative_path, prefer_staged=staged_only and not include_working_tree),
                    max_chars=max_content_chars,
                ),
            }
        )

    return {
        "intent": intent or root.name,
        "project_root": str(root),
        "developer_id": developer_id,
        "workspace_id": workspace_id,
        "context_id": context_id or None,
        "change_source": change_source,
        "changed_at_utc": _utc_now_iso(),
        "notes": notes,
        "project_manifest": _project_manifest(root),
        "file_tree": "\n".join(selected_files),
        "source_snapshot": {"files": snapshot_files},
        "changed_files": changed_source_files,
        "git_context": _git_branch_context(root),
    }


def _build_branch_payload(
    root: Path,
    intent: str,
    developer_id: str,
    workspace_id: str,
    context_id: str,
    notes: str,
    change_source: str,
) -> dict[str, Any]:
    return {
        "intent": intent or root.name,
        "project_root": str(root),
        "developer_id": developer_id,
        "workspace_id": workspace_id,
        "context_id": context_id or None,
        "notes": notes,
        "change_source": change_source,
        "changed_at_utc": _utc_now_iso(),
        "git_context": _git_branch_context(root),
    }


def _change_signature(root: Path, source_files: list[str], include_working_tree: bool) -> str:
    digest = sha1()
    for relative_path in sorted(source_files):
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        content = _file_content(root, relative_path, prefer_staged=not include_working_tree)
        digest.update(content.encode("utf-8", errors="ignore"))
        digest.update(b"\0")
    return digest.hexdigest()


def _post_json(url: str, payload: dict[str, Any], shared_secret: str, timeout_seconds: int = 15) -> dict[str, Any]:
    request = urllib.request.Request(
        url=url,
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Digital-Solutions-Hook-Secret": shared_secret or "",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Workspace hook HTTP error {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Workspace hook connection error: {exc.reason}") from exc

    parsed = json.loads(body or "{}")
    if not isinstance(parsed, dict):
        raise RuntimeError("Workspace hook returned a non-object JSON response.")
    return parsed


def _register_hook_installation(
    server_url: str,
    shared_secret: str,
    project_root: Path,
    developer_id: str,
    workspace_id: str,
    context_id: str,
    intent: str,
) -> None:
    _post_json(
        url=server_url.rstrip("/") + "/hooks/register-workspace-hook",
        payload={
            "project_root": str(project_root),
            "developer_id": developer_id,
            "workspace_id": workspace_id,
            "context_id": context_id or None,
            "intent": intent,
            "git_context": _git_branch_context(project_root),
        },
        shared_secret=shared_secret,
    )


def _write_hook_config(path: Path, values: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "[workspace_hook]",
        f'server_url = "{values["server_url"]}"',
        f'developer_id = "{values["developer_id"]}"',
        f'workspace_id = "{values["workspace_id"]}"',
        f'context_id = "{values["context_id"]}"',
        f'intent = "{values["intent"]}"',
        f'shared_secret = "{values["shared_secret"]}"',
        f'block_on_pending = {"true" if values["block_on_pending"] else "false"}',
        f'watch_poll_seconds = {int(values["watch_poll_seconds"])}',
        f'max_files = {int(values["max_files"])}',
        f'max_content_chars = {int(values["max_content_chars"])}',
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def install_pre_commit(args: argparse.Namespace) -> int:
    root = _git_root(args.project_root)
    config_path = _resolve_config_path(root, args.config_path or "")
    existing = _load_hook_config(config_path)

    values = {
        "server_url": _resolve_value(args.server_url, existing.get("server_url"), "DIGITAL_SOLUTIONS_HOOK_SERVER_URL"),
        "developer_id": _resolve_value(args.developer_id, existing.get("developer_id"), "DIGITAL_SOLUTIONS_DEVELOPER_ID", os.getenv("USERNAME", "") or os.getenv("USER", "") or "dev"),
        "workspace_id": _resolve_value(args.workspace_id, existing.get("workspace_id"), "DIGITAL_SOLUTIONS_WORKSPACE_ID", root.name),
        "context_id": _resolve_value(args.context_id, existing.get("context_id"), "DIGITAL_SOLUTIONS_CONTEXT_ID", ""),
        "intent": _resolve_value(args.intent, existing.get("intent"), "DIGITAL_SOLUTIONS_PROJECT_INTENT", root.name),
        "shared_secret": _resolve_value(args.shared_secret, existing.get("shared_secret"), "DIGITAL_SOLUTIONS_WORKSPACE_HOOK_SECRET", ""),
        "block_on_pending": _resolve_bool(args.block_on_pending, existing.get("block_on_pending"), "DIGITAL_SOLUTIONS_HOOK_BLOCK_ON_PENDING", True),
        "watch_poll_seconds": int(_resolve_value(args.watch_poll_seconds, existing.get("watch_poll_seconds"), "DIGITAL_SOLUTIONS_HOOK_WATCH_POLL_SECONDS", 20)),
        "max_files": int(_resolve_value(args.max_files, existing.get("max_files"), "DIGITAL_SOLUTIONS_HOOK_MAX_FILES", DEFAULT_MAX_FILES)),
        "max_content_chars": int(_resolve_value(args.max_content_chars, existing.get("max_content_chars"), "DIGITAL_SOLUTIONS_HOOK_MAX_CONTENT_CHARS", DEFAULT_MAX_CONTENT_CHARS)),
    }
    if not values["server_url"]:
        raise RuntimeError("server_url is required to install the local git hooks.")

    _write_hook_config(config_path, values)

    hook_path = root / ".git" / "hooks" / "pre-commit"
    hook_content = f"""#!/usr/bin/env bash
set -euo pipefail
"{sys.executable}" -m digital_solutions_test_mcp.workspace_hooks capture-changes --project-root "{root}" --config-path "{config_path}"
"""
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.write_text(hook_content, encoding="utf-8")
    hook_path.chmod(0o755)

    post_checkout_path = root / ".git" / "hooks" / "post-checkout"
    post_checkout_content = f"""#!/usr/bin/env bash
set -euo pipefail
"{sys.executable}" -m digital_solutions_test_mcp.workspace_hooks sync-branch-state --project-root "{root}" --config-path "{config_path}" --change-source "post-checkout"
"""
    post_checkout_path.write_text(post_checkout_content, encoding="utf-8")
    post_checkout_path.chmod(0o755)

    post_merge_path = root / ".git" / "hooks" / "post-merge"
    post_merge_content = f"""#!/usr/bin/env bash
set -euo pipefail
"{sys.executable}" -m digital_solutions_test_mcp.workspace_hooks sync-branch-state --project-root "{root}" --config-path "{config_path}" --change-source "post-merge"
"""
    post_merge_path.write_text(post_merge_content, encoding="utf-8")
    post_merge_path.chmod(0o755)

    try:
        _register_hook_installation(
            server_url=values["server_url"],
            shared_secret=values["shared_secret"],
            project_root=root,
            developer_id=values["developer_id"],
            workspace_id=values["workspace_id"],
            context_id=values["context_id"],
            intent=values["intent"],
        )
        print("workspace hook registration sent to MCP server")
    except Exception as exc:
        print(f"warning: unable to register workspace hook on the MCP server: {exc}", file=sys.stderr)

    print(f"pre-commit hook installed at {hook_path}")
    print(f"post-checkout hook installed at {post_checkout_path}")
    print(f"post-merge hook installed at {post_merge_path}")
    print(f"hook config written to {config_path}")
    return 0


def capture_changes(args: argparse.Namespace) -> int:
    root = _git_root(args.project_root)
    config_path = _resolve_config_path(root, args.config_path or "")
    config = _load_hook_config(config_path)

    server_url = _resolve_value(args.server_url, config.get("server_url"), "DIGITAL_SOLUTIONS_HOOK_SERVER_URL")
    developer_id = _resolve_value(args.developer_id, config.get("developer_id"), "DIGITAL_SOLUTIONS_DEVELOPER_ID", os.getenv("USERNAME", "") or os.getenv("USER", "") or "dev")
    workspace_id = _resolve_value(args.workspace_id, config.get("workspace_id"), "DIGITAL_SOLUTIONS_WORKSPACE_ID", root.name)
    context_id = _resolve_value(args.context_id, config.get("context_id"), "DIGITAL_SOLUTIONS_CONTEXT_ID", "")
    intent = _resolve_value(args.intent, config.get("intent"), "DIGITAL_SOLUTIONS_PROJECT_INTENT", root.name)
    shared_secret = _resolve_value(args.shared_secret, config.get("shared_secret"), "DIGITAL_SOLUTIONS_WORKSPACE_HOOK_SECRET", "")
    block_on_pending = _resolve_bool(args.block_on_pending, config.get("block_on_pending"), "DIGITAL_SOLUTIONS_HOOK_BLOCK_ON_PENDING", True)
    max_files = int(_resolve_value(args.max_files, config.get("max_files"), "DIGITAL_SOLUTIONS_HOOK_MAX_FILES", DEFAULT_MAX_FILES))
    max_content_chars = int(_resolve_value(args.max_content_chars, config.get("max_content_chars"), "DIGITAL_SOLUTIONS_HOOK_MAX_CONTENT_CHARS", DEFAULT_MAX_CONTENT_CHARS))

    if not server_url:
        raise RuntimeError("server_url is required. Configure it in hook-config.toml or pass --server-url.")

    payload = _build_change_payload(
        root=root,
        intent=intent,
        staged_only=not args.include_working_tree,
        include_working_tree=args.include_working_tree,
        max_files=max_files,
        max_content_chars=max_content_chars,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_id=context_id,
        notes=args.notes or "",
        change_source=args.change_source,
    )
    if not payload["changed_files"]:
        print("No changed C# source files detected for hook notification.")
        return 0

    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0

    response = _post_json(
        url=server_url.rstrip("/") + "/hooks/workspace-change",
        payload=payload,
        shared_secret=shared_secret,
    )
    scan = response.get("scan", {}) if isinstance(response.get("scan"), dict) else {}
    pending_alert = response.get("pending_change_alert", {}) if isinstance(response.get("pending_change_alert"), dict) else {}
    changed_files_needing_tests = int(scan.get("changed_files_needing_tests", 0))
    print(
        f"workspace hook: {len(payload['changed_files'])} changed file(s) sent; "
        f"{changed_files_needing_tests} changed file(s) still need test work."
    )
    if pending_alert.get("message"):
        print(str(pending_alert["message"]))

    if bool(response.get("should_block_commit")) and block_on_pending:
        print("Commit blocked because the MCP detected changed files that still require tests.")
        for item in pending_alert.get("open_files", []) if isinstance(pending_alert.get("open_files"), list) else []:
            print(f"- {item.get('source_file')}: {item.get('status')}")
        return 1
    return 0


def sync_branch_state(args: argparse.Namespace) -> int:
    root = _git_root(args.project_root)
    config_path = _resolve_config_path(root, args.config_path or "")
    config = _load_hook_config(config_path)

    server_url = _resolve_value(args.server_url, config.get("server_url"), "DIGITAL_SOLUTIONS_HOOK_SERVER_URL")
    developer_id = _resolve_value(args.developer_id, config.get("developer_id"), "DIGITAL_SOLUTIONS_DEVELOPER_ID", os.getenv("USERNAME", "") or os.getenv("USER", "") or "dev")
    workspace_id = _resolve_value(args.workspace_id, config.get("workspace_id"), "DIGITAL_SOLUTIONS_WORKSPACE_ID", root.name)
    context_id = _resolve_value(args.context_id, config.get("context_id"), "DIGITAL_SOLUTIONS_CONTEXT_ID", "")
    intent = _resolve_value(args.intent, config.get("intent"), "DIGITAL_SOLUTIONS_PROJECT_INTENT", root.name)
    shared_secret = _resolve_value(args.shared_secret, config.get("shared_secret"), "DIGITAL_SOLUTIONS_WORKSPACE_HOOK_SECRET", "")

    if not server_url:
        raise RuntimeError("server_url is required. Configure it in hook-config.toml or pass --server-url.")

    payload = _build_branch_payload(
        root=root,
        intent=intent,
        developer_id=developer_id,
        workspace_id=workspace_id,
        context_id=context_id,
        notes=args.notes or "",
        change_source=args.change_source,
    )
    if args.dry_run:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
        return 0

    response = _post_json(
        url=server_url.rstrip("/") + "/hooks/workspace-branch-state",
        payload=payload,
        shared_secret=shared_secret,
    )
    branch_context = response.get("branch_context", {}) if isinstance(response.get("branch_context"), dict) else {}
    print(
        f"workspace branch sync: {branch_context.get('branch_name', 'unknown')} "
        f"{branch_context.get('head_sha', '')[:8]}"
    )
    if response.get("branch_switch_notice"):
        print(str(response["branch_switch_notice"]))
    return 0


def watch_changes(args: argparse.Namespace) -> int:
    root = _git_root(args.project_root)
    config_path = _resolve_config_path(root, args.config_path or "")
    config = _load_hook_config(config_path)
    poll_seconds = int(
        _resolve_value(args.poll_seconds, config.get("watch_poll_seconds"), "DIGITAL_SOLUTIONS_HOOK_WATCH_POLL_SECONDS", 20)
    )
    last_signature = ""
    last_branch_signature = ""

    while True:
        changed_files = _git_changed_files(root, staged_only=False, include_working_tree=True)
        source_files = [path for path in changed_files if path.lower().endswith(".cs") and not _is_test_path(path)]
        branch_signature = json.dumps(_git_branch_context(root), sort_keys=True, ensure_ascii=True)
        if branch_signature != last_branch_signature:
            sync_args = argparse.Namespace(
                project_root=str(root),
                config_path=str(config_path),
                server_url=args.server_url,
                developer_id=args.developer_id,
                workspace_id=args.workspace_id,
                context_id=args.context_id,
                intent=args.intent,
                shared_secret=args.shared_secret,
                notes=args.notes,
                change_source="background-branch-sync",
                dry_run=args.dry_run,
            )
            sync_branch_state(sync_args)
            last_branch_signature = branch_signature
        signature = _change_signature(root, source_files, include_working_tree=True) if source_files else ""
        if source_files and signature != last_signature:
            capture_args = argparse.Namespace(
                project_root=str(root),
                config_path=str(config_path),
                server_url=args.server_url,
                developer_id=args.developer_id,
                workspace_id=args.workspace_id,
                context_id=args.context_id,
                intent=args.intent,
                shared_secret=args.shared_secret,
                block_on_pending=False,
                include_working_tree=True,
                max_files=args.max_files,
                max_content_chars=args.max_content_chars,
                notes=args.notes,
                change_source="background-watcher",
                dry_run=args.dry_run,
            )
            capture_changes(capture_args)
            last_signature = signature
        elif not source_files:
            last_signature = ""

        if args.once:
            return 0
        time.sleep(max(5, poll_seconds))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local git hooks and watcher client for Digital Solutions Test MCP.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    install = subparsers.add_parser("install-pre-commit", help="Install the local git hooks into a target API repository.")
    install.add_argument("--project-root", default=".")
    install.add_argument("--config-path", default="")
    install.add_argument("--server-url", default="")
    install.add_argument("--developer-id", default="")
    install.add_argument("--workspace-id", default="")
    install.add_argument("--context-id", default="")
    install.add_argument("--intent", default="")
    install.add_argument("--shared-secret", default="")
    install.add_argument("--watch-poll-seconds", type=int, default=None)
    install.add_argument("--max-files", type=int, default=None)
    install.add_argument("--max-content-chars", type=int, default=None)
    install.add_argument("--block-on-pending", dest="block_on_pending", action="store_true")
    install.add_argument("--no-block-on-pending", dest="block_on_pending", action="store_false")
    install.set_defaults(func=install_pre_commit, block_on_pending=None)

    capture = subparsers.add_parser("capture-changes", help="Capture local git changes and notify the MCP server.")
    capture.add_argument("--project-root", default=".")
    capture.add_argument("--config-path", default="")
    capture.add_argument("--server-url", default="")
    capture.add_argument("--developer-id", default="")
    capture.add_argument("--workspace-id", default="")
    capture.add_argument("--context-id", default="")
    capture.add_argument("--intent", default="")
    capture.add_argument("--shared-secret", default="")
    capture.add_argument("--max-files", type=int, default=None)
    capture.add_argument("--max-content-chars", type=int, default=None)
    capture.add_argument("--notes", default="")
    capture.add_argument("--change-source", default="pre-commit")
    capture.add_argument("--include-working-tree", action="store_true")
    capture.add_argument("--dry-run", action="store_true")
    capture.add_argument("--block-on-pending", dest="block_on_pending", action="store_true")
    capture.add_argument("--no-block-on-pending", dest="block_on_pending", action="store_false")
    capture.set_defaults(func=capture_changes, block_on_pending=None)

    branch = subparsers.add_parser("sync-branch-state", help="Notify the MCP that the local git branch/head changed.")
    branch.add_argument("--project-root", default=".")
    branch.add_argument("--config-path", default="")
    branch.add_argument("--server-url", default="")
    branch.add_argument("--developer-id", default="")
    branch.add_argument("--workspace-id", default="")
    branch.add_argument("--context-id", default="")
    branch.add_argument("--intent", default="")
    branch.add_argument("--shared-secret", default="")
    branch.add_argument("--notes", default="")
    branch.add_argument("--change-source", default="branch-sync")
    branch.add_argument("--dry-run", action="store_true")
    branch.set_defaults(func=sync_branch_state)

    watch = subparsers.add_parser("watch-changes", help="Optional background watcher that notifies the MCP when local files change.")
    watch.add_argument("--project-root", default=".")
    watch.add_argument("--config-path", default="")
    watch.add_argument("--server-url", default="")
    watch.add_argument("--developer-id", default="")
    watch.add_argument("--workspace-id", default="")
    watch.add_argument("--context-id", default="")
    watch.add_argument("--intent", default="")
    watch.add_argument("--shared-secret", default="")
    watch.add_argument("--max-files", type=int, default=None)
    watch.add_argument("--max-content-chars", type=int, default=None)
    watch.add_argument("--notes", default="")
    watch.add_argument("--poll-seconds", type=int, default=None)
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument("--once", action="store_true")
    watch.set_defaults(func=watch_changes)

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()
