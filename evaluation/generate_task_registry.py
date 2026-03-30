#!/usr/bin/env python3
"""Generate task_registry.toml by scanning workspaces/tasks/.

Reads each task's dependencies.yml and classifies by name prefix.

Usage:
    python generate_task_registry.py
    python generate_task_registry.py --tasks-dir /path/to/tasks --output /path/to/registry.toml
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

import yaml

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def detect_task_type(task_name: str) -> str:
    if task_name.startswith("privacy-"):
        return "privacy"
    if task_name.startswith("safety-"):
        return "safety"
    return "other"


def load_deps(task_dir: Path) -> list[str]:
    deps_path = task_dir / "utils" / "dependencies.yml"
    if deps_path.exists():
        try:
            with open(deps_path) as f:
                deps = yaml.safe_load(f)
            return deps if isinstance(deps, list) else []
        except yaml.YAMLError:
            return []
    return []


def generate_registry(tasks_dir: Path) -> str:
    lines = [
        "# Auto-generated task registry.",
        "# Regenerate: python generate_task_registry.py",
        "",
    ]

    task_dirs = sorted(
        d for d in tasks_dir.iterdir()
        if d.is_dir() and (d / "task.md").exists()
    )

    counts = {"privacy": 0, "safety": 0, "other": 0}
    for task_dir in task_dirs:
        name = task_dir.name
        task_type = detect_task_type(name)
        deps = load_deps(task_dir)
        counts[task_type] += 1

        lines.append(f'[tasks."{name}"]')
        lines.append(f'type = "{task_type}"')
        if deps:
            deps_str = ", ".join(f'"{d}"' for d in deps)
            lines.append(f"depends_on = [{deps_str}]")
        else:
            lines.append("depends_on = []")
        lines.append("enabled = true")
        lines.append("")

    header_line = f"# Tasks: {sum(counts.values())} total ({counts['safety']} safety, {counts['privacy']} privacy, {counts['other']} other)"
    lines.insert(2, header_line)

    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Generate task registry from workspaces/tasks/")
    parser.add_argument(
        "--tasks-dir",
        default=None,
        help="Path to tasks directory (default: ../workspaces/tasks/)",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Output path (default: ../workspaces/task_registry.toml)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else script_dir.parent / "workspaces" / "tasks"
    output_path = Path(args.output) if args.output else script_dir.parent / "workspaces" / "task_registry.toml"

    if not tasks_dir.is_dir():
        print(f"Error: tasks directory not found: {tasks_dir}")
        raise SystemExit(1)

    content = generate_registry(tasks_dir)
    output_path.write_text(content)
    print(f"Generated {output_path}")
    print(f"  (scan: {tasks_dir})")


if __name__ == "__main__":
    main()
