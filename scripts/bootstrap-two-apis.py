#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from gosystem_test_mcp.core import bootstrap_project, discover_changes


def main() -> None:
    parser = argparse.ArgumentParser(description="Bootstrap gosystem-test-mcp in two or more APIs")
    parser.add_argument("project_roots", nargs="+", help="Absolute path(s) to API project roots")
    parser.add_argument("--base-ref", default="HEAD~1", help="Git base ref for change detection")
    parser.add_argument(
        "--include-untracked",
        action="store_true",
        default=False,
        help="Include untracked .cs files in change detection",
    )
    args = parser.parse_args()

    output: list[dict] = []

    for root in args.project_roots:
        root_path = Path(root).resolve()
        boot = bootstrap_project(str(root_path), overwrite_agents=False)
        changes = discover_changes(
            project_root=str(root_path),
            base_ref=args.base_ref,
            include_untracked=args.include_untracked,
        )

        output.append(
            {
                "project_root": str(root_path),
                "bootstrap_state": boot.get("state_dir"),
                "solution": boot.get("detected", {}).get("solution_path"),
                "default_test_project": boot.get("detected", {}).get("default_test_project"),
                "changed_files_count": changes.get("changed_files_count"),
                "testable_files_count": changes.get("testable_files_count"),
            }
        )

    print(json.dumps(output, indent=2, ensure_ascii=True))


if __name__ == "__main__":
    main()
