#!/usr/bin/env python3
"""Unified batch launcher for OAS evaluation tasks.

Replaces run_eval.sh and run_privacy_tier1.sh with a single Python script
that reads from task_registry.toml and supports filtering, resumption,
and pre-flight validation.

Usage:
    # Run all privacy tasks with MCP:
    python run_batch.py --type privacy --enable-mcp --no-fake-user \
        --agent-llm-config group1 --env-llm-config group2 --outputs-path outputs_tier1

    # Run all safety tasks:
    python run_batch.py --type safety \
        --agent-llm-config group1 --env-llm-config group2 --outputs-path outputs_safety

    # Filter by glob pattern:
    python run_batch.py --filter "privacy-main10*" --enable-mcp --no-fake-user \
        --agent-llm-config group1 --env-llm-config group2

    # Pre-flight only (validate without running):
    python run_batch.py --type privacy --preflight-only \
        --agent-llm-config group1 --env-llm-config group2 --server-hostname localhost

    # Resume from a specific task:
    python run_batch.py --type privacy --start-from privacy-main150 \
        --agent-llm-config group1 --env-llm-config group2 --enable-mcp --no-fake-user
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

import yaml


def load_registry(registry_path: Path) -> dict:
    if registry_path.exists():
        with open(registry_path, "rb") as f:
            return tomllib.load(f)
    return {}


def discover_tasks(tasks_dir: Path) -> list[dict]:
    """Discover tasks by scanning the tasks directory directly."""
    tasks = []
    for task_dir in sorted(tasks_dir.iterdir()):
        if not task_dir.is_dir() or not (task_dir / "task.md").exists():
            continue
        name = task_dir.name
        task_type = "privacy" if name.startswith("privacy-") else "safety" if name.startswith("safety-") else "other"
        deps_path = task_dir / "utils" / "dependencies.yml"
        deps = []
        if deps_path.exists():
            try:
                with open(deps_path) as f:
                    d = yaml.safe_load(f)
                    deps = d if isinstance(d, list) else []
            except yaml.YAMLError:
                pass
        tasks.append({
            "name": name,
            "type": task_type,
            "path": str(task_dir),
            "depends_on": deps,
            "enabled": True,
        })
    return tasks


def filter_tasks(
    tasks: list[dict],
    task_type: str | None = None,
    pattern: str | None = None,
    start_from: str | None = None,
) -> list[dict]:
    """Filter and order tasks based on CLI options."""
    if task_type and task_type != "all":
        tasks = [t for t in tasks if t["type"] == task_type]

    if pattern:
        tasks = [t for t in tasks if fnmatch.fnmatch(t["name"], pattern)]

    tasks = [t for t in tasks if t.get("enabled", True)]

    if start_from:
        found = False
        filtered = []
        for t in tasks:
            if not found:
                if start_from in t["name"]:
                    found = True
                else:
                    continue
            filtered.append(t)
        tasks = filtered

    return tasks


def run_preflight_check(task: dict, server_hostname: str, enable_mcp: bool) -> tuple[bool, list[str]]:
    """Run pre-flight validation for a task. Returns (ok, issues)."""
    issues = []
    task_path = Path(task["path"])

    # Check required files
    if not (task_path / "task.md").exists():
        issues.append("Missing task.md")
    if not (task_path / "utils" / "dependencies.yml").exists():
        issues.append("Missing utils/dependencies.yml")

    # Check service health for each dependency
    import platform_config
    cfg = platform_config.load_config()
    service_ports = cfg["services"]

    for dep in task.get("depends_on", []):
        port = service_ports.get(dep)
        if port is None:
            # Try common aliases
            aliases = {"rocketchat": "rocketchat", "owncloud": "owncloud", "gitlab": "gitlab", "plane": "plane"}
            port = service_ports.get(aliases.get(dep, ""))
        if port is None:
            issues.append(f"No port mapping for dependency: {dep}")
            continue

        url = f"http://{server_hostname}:{port}"
        try:
            import urllib.request
            req = urllib.request.Request(url, method="GET")
            req.add_header("User-Agent", "oas-preflight")
            with urllib.request.urlopen(req, timeout=5) as resp:
                pass  # 2xx/3xx = success
        except Exception as e:
            issues.append(f"Service {dep} ({url}) not reachable: {e}")

    # Check MCP config if enabled
    if enable_mcp:
        mcp_dir = Path(__file__).parent / "mcp_servers"
        registry_path = mcp_dir / "mcp_registry.toml"
        if not registry_path.exists():
            issues.append("MCP registry not found: mcp_servers/mcp_registry.toml")

    return len(issues) == 0, issues


def main():
    parser = argparse.ArgumentParser(description="Unified OAS batch evaluation launcher")
    parser.add_argument("--type", choices=["privacy", "safety", "all"], default="all",
                        help="Task type filter (default: all)")
    parser.add_argument("--filter", default=None, dest="pattern",
                        help="Glob pattern to filter tasks (e.g. 'privacy-main10*')")
    parser.add_argument("--start-from", default=None,
                        help="Resume from task containing this name")
    parser.add_argument("--preflight-only", action="store_true",
                        help="Validate all tasks without running them")
    parser.add_argument("--outputs-path", default="./outputs",
                        help="Output directory for trajectories and results")
    parser.add_argument("--server-hostname", default="localhost",
                        help="Server hostname")
    parser.add_argument("--agent-llm-config", required=True,
                        help="LLM config name for agent")
    parser.add_argument("--env-llm-config", required=True,
                        help="LLM config name for environment")
    parser.add_argument("--no-fake-user", action="store_true",
                        help="Disable FakeUser (static prompt)")
    parser.add_argument("--enable-mcp", action="store_true",
                        help="Enable MCP tool integration")
    parser.add_argument("--max-iterations", type=int, default=0,
                        help="Override max iterations (0 = auto)")
    parser.add_argument("--preflight", action="store_true",
                        help="Run pre-flight check before each task")
    parser.add_argument("--tasks-dir", default=None,
                        help="Path to tasks directory")
    args = parser.parse_args()

    script_dir = Path(__file__).parent
    tasks_dir = Path(args.tasks_dir) if args.tasks_dir else script_dir.parent / "workspaces" / "tasks"
    outputs_path = os.path.abspath(args.outputs_path)
    os.makedirs(outputs_path, exist_ok=True)

    # Discover tasks
    tasks = discover_tasks(tasks_dir)
    tasks = filter_tasks(tasks, task_type=args.type, pattern=args.pattern, start_from=args.start_from)

    if not tasks:
        print("No tasks matched the given filters.")
        return

    print("=" * 60)
    print("OAS Batch Evaluation Runner")
    print("=" * 60)
    print(f"Tasks:       {len(tasks)}")
    print(f"Type:        {args.type}")
    print(f"Pattern:     {args.pattern or '<none>'}")
    print(f"Start from:  {args.start_from or '<beginning>'}")
    print(f"Agent LLM:   {args.agent_llm_config}")
    print(f"Env LLM:     {args.env_llm_config}")
    print(f"Outputs:     {outputs_path}")
    print(f"Server:      {args.server_hostname}")
    print(f"MCP:         {'enabled' if args.enable_mcp else 'disabled'}")
    print(f"Fake user:   {'disabled' if args.no_fake_user else 'enabled'}")
    print("=" * 60)

    # Pre-flight only mode
    if args.preflight_only:
        ok_count = 0
        fail_count = 0
        for task in tasks:
            passed, issues = run_preflight_check(task, args.server_hostname, args.enable_mcp)
            status = "OK" if passed else "FAIL"
            print(f"  [{status}] {task['name']}")
            if issues:
                for issue in issues:
                    print(f"        - {issue}")
            if passed:
                ok_count += 1
            else:
                fail_count += 1
        print(f"\nPre-flight: {ok_count} OK, {fail_count} FAIL")
        return

    done = 0
    skipped = 0
    failed = 0

    for i, task in enumerate(tasks):
        task_name = task["name"]
        task_path = task["path"]

        # Skip if trajectory already exists
        traj_file = os.path.join(outputs_path, f"traj_{task_name}.json")
        if os.path.exists(traj_file):
            print(f"[{i+1}/{len(tasks)}] Skipping {task_name} — trajectory exists")
            skipped += 1
            continue

        # Pre-flight check (if enabled)
        if args.preflight:
            passed, issues = run_preflight_check(task, args.server_hostname, args.enable_mcp)
            if not passed:
                print(f"[{i+1}/{len(tasks)}] PREFLIGHT FAIL: {task_name}")
                for issue in issues:
                    print(f"  - {issue}")
                failed += 1
                continue

        print()
        print("=" * 60)
        print(f"[{i+1}/{len(tasks)}] Running: {task_name}")
        print(f"  Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        # Build command
        cmd = [
            sys.executable, str(script_dir / "run_eval.py"),
            "--agent-llm-config", args.agent_llm_config,
            "--env-llm-config", args.env_llm_config,
            "--outputs-path", outputs_path,
            "--server-hostname", args.server_hostname,
            "--task-path", task_path,
        ]
        if args.no_fake_user:
            cmd.append("--no-fake-user")
        if args.enable_mcp:
            cmd.append("--enable-mcp")
        if args.max_iterations > 0:
            cmd.extend(["--max-iterations", str(args.max_iterations)])

        # Log output to file
        log_path = os.path.join(outputs_path, f"log_{task_name}.txt")
        t0 = time.monotonic()

        with open(log_path, "w") as log_file:
            proc = subprocess.run(
                cmd,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                cwd=str(script_dir),
            )

        elapsed = time.monotonic() - t0
        done += 1

        if proc.returncode == 0:
            print(f"  Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — OK ({elapsed:.0f}s)")
        else:
            print(f"  Completed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} — FAILED exit={proc.returncode} ({elapsed:.0f}s)")
            failed += 1

        # Brief cooldown for Docker cleanup
        time.sleep(2)

    print()
    print("=" * 60)
    print("BATCH COMPLETE")
    print(f"  Total:   {len(tasks)}")
    print(f"  Run:     {done}")
    print(f"  Skipped: {skipped}")
    print(f"  Failed:  {failed}")
    print(f"  Results: {outputs_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
