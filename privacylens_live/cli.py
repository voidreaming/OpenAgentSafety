"""CLI entry point for PrivacyLens-Live.

Usage:
    python -m privacylens_live setup        # Start services + bootstrap tokens
    python -m privacylens_live write-env    # Regenerate .env from config.py
    python -m privacylens_live generate     # Generate task directories
    python -m privacylens_live verify       # Statically verify seed conversion
    python -m privacylens_live run          # Run agent on tasks
    python -m privacylens_live evaluate     # Evaluate results
    python -m privacylens_live teardown     # Stop Docker services
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import subprocess
import time
from pathlib import Path

from privacylens_live.config import Config


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("cli")

COMPOSE_FILE = Path(__file__).parent / "docker-compose.yml"

# Infrastructure services that must be healthy before bootstrap can probe
# them. The MCP servers are deliberately excluded — they get started in a
# second pass after bootstrap has provisioned the tokens they need.
INFRA_SERVICES = [
    "bookstack-db",
    "bookstack",
    "mattermost-db",
    "mattermost",
    "mongo",
    "rocketchat",
    "mailpit",
    "gotosocial",
    "radicale",
]

MCP_SERVICES = [
    "bookstack-mcp",
    "mattermost-mcp",
    "rocketchat-mcp",
    "mailpit-mcp",
    "gotosocial-mcp",
    "radicale-mcp",
]


def _compose(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a docker-compose subcommand."""
    cmd = ["docker", "compose", "-f", str(COMPOSE_FILE), *args]
    return subprocess.run(cmd, check=check, capture_output=True, text=True)


def _wait_for_healthy(services: list[str], timeout: int = 300) -> None:
    """Poll `docker compose ps` until all listed services report healthy.

    Containers without a healthcheck are considered ready as soon as they're
    in state ``running`` (the bookstack and mattermost healthchecks set
    long start_periods, so honest "healthy" reporting is what we want).
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _compose("ps", "--format", "json", check=False)
        if result.returncode != 0:
            time.sleep(2)
            continue
        # Each line is a JSON object describing one container.
        statuses: dict[str, str] = {}
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            statuses[obj.get("Service", "")] = obj.get("Health") or obj.get("State", "")
        missing = [s for s in services if s not in statuses]
        unhealthy = [
            s
            for s in services
            if s in statuses and statuses[s] not in ("healthy", "running")
        ]
        if not missing and not unhealthy:
            logger.info("All %d infra services healthy.", len(services))
            return
        logger.info(
            "Waiting for services... missing=%s unhealthy=%s",
            missing or "[]",
            {s: statuses[s] for s in unhealthy} or "{}",
        )
        time.sleep(5)
    raise RuntimeError(
        f"Timed out after {timeout}s waiting for services to become healthy."
    )


def cmd_setup(_args: argparse.Namespace) -> None:
    """Bring up the stack and provision tokens.

    Flow:
      1. Write .env from current config.py defaults.
      2. ``docker compose up -d`` for the infrastructure services only.
      3. Wait for healthchecks.
      4. Run :func:`privacylens_live.bootstrap.bootstrap_all` to probe each
         service and provision any missing user / token.
      5. If anything was provisioned, re-write .env so the new values are
         picked up by the MCP server containers.
      6. ``docker compose up -d`` for the MCP servers (now with valid env).

    Idempotent: if everything's already provisioned, only steps 1, 3, and 6
    do real work and the bootstrap probes return immediately.
    """
    from privacylens_live.bootstrap import bootstrap_all

    # Step 1: write .env from current config defaults.
    config = Config.from_env()
    config.write_env_file()

    # Step 2: bring up the infra services only.
    logger.info("Starting infrastructure services...")
    _compose("up", "-d", *INFRA_SERVICES)

    # Step 3: wait for healthchecks.
    _wait_for_healthy(INFRA_SERVICES)

    # Step 4: bootstrap (probe + provision).
    logger.info("Running bootstrap probes...")
    config = bootstrap_all(config, COMPOSE_FILE)

    # Step 5: re-write .env in case bootstrap updated any tokens.
    config.write_env_file()

    # Step 6: bring up the MCP servers, forcing recreate so they pick up
    # any updated env vars from the regenerated .env.
    logger.info("Starting MCP servers...")
    _compose("up", "-d", "--force-recreate", *MCP_SERVICES)

    logger.info("Setup complete. Use 'docker compose ps' to check status.")


def cmd_write_env(_args: argparse.Namespace) -> None:
    """Regenerate privacylens_live/.env from config.py defaults.

    Useful for refreshing the file after editing config.py without bringing
    the stack up or down.
    """
    config = Config.from_env()
    path = config.write_env_file()
    print(f"Wrote {path}")


def cmd_generate(args: argparse.Namespace) -> None:
    """Generate task directories from main_data.json."""
    from privacylens_live.tasks.generate import generate_all

    data_path = Path(args.data)
    output_dir = Path(args.output)
    generate_all(data_path, output_dir)


def cmd_run(args: argparse.Namespace) -> None:
    """Run agent on specified tasks."""
    from privacylens_live.runner.agent_runner import PrivacyLensRunner

    config = Config.from_env()
    if args.model:
        config.llm_model = args.model

    runner = PrivacyLensRunner(
        config,
        max_clarification_rounds=args.max_clarifications,
        prompt_variant=args.prompt_variant,
        disable_security_analyzer=args.disable_security_analyzer,
        enable_privacy_analyzer=args.enable_privacy_analyzer,
    )

    # Determine which tasks to run
    tasks_base = Path(args.tasks_dir)
    results_dir = Path(args.results_dir)
    results_dir.mkdir(parents=True, exist_ok=True)

    if args.names:
        task_dirs = [tasks_base / name for name in args.names.split(",")]
    elif args.range:
        start, end = map(int, args.range.split("-"))
        all_tasks = sorted(
            [
                d
                for d in tasks_base.iterdir()
                if d.is_dir() and (d / "task.json").exists()
            ],
            key=lambda p: _task_sort_key(p.name),
        )
        task_dirs = all_tasks[start:end]
    else:
        task_dirs = sorted(
            [
                d
                for d in tasks_base.iterdir()
                if d.is_dir() and (d / "task.json").exists()
            ],
            key=lambda p: _task_sort_key(p.name),
        )

    # Apply --resume / --retry-errors filter against existing result files.
    #
    # Semantics:
    #   --resume                 : skip every task that already has any
    #                              result file (ok, no_action, OR error)
    #   --retry-errors           : run only tasks whose existing result
    #                              has status=error; skip everything else
    #                              that already has a result; still run
    #                              tasks that have NO result yet
    #   --resume --retry-errors  : skip ok/no_action, re-run errors
    if args.resume or args.retry_errors:
        kept: list[Path] = []
        skipped = 0
        for task_dir in task_dirs:
            result_file = results_dir / f"{task_dir.name}.json"
            if not result_file.exists():
                # No result yet — always run.
                kept.append(task_dir)
                continue
            try:
                data = json.loads(result_file.read_text())
            except (json.JSONDecodeError, OSError):
                # Corrupt result — re-run from scratch.
                kept.append(task_dir)
                continue
            status = data.get("status", "")

            # --retry-errors takes priority: an existing error always
            # re-runs when this flag is set, regardless of --resume.
            if args.retry_errors and status == "error":
                kept.append(task_dir)
                continue

            # Otherwise, anything with an existing result is skipped
            # (--resume covers all statuses, --retry-errors-only skips
            # non-errors).
            skipped += 1

        if skipped:
            logger.info(f"Filter: skipping {skipped} task(s) with existing results")
        task_dirs = kept

    if not task_dirs:
        logger.info(
            "Nothing to run. "
            "(Use --retry-errors to re-run failed tasks, "
            "or remove --resume to re-run everything.)"
        )
        return

    logger.info(f"Running {len(task_dirs)} tasks...")
    results = asyncio.run(runner.run_tasks(task_dirs, results_dir))

    # Final summary across the results from THIS run only (not cumulative
    # across resumes — for the cumulative view see cmd_status).
    ok = sum(1 for r in results if r.status == "ok")
    no = sum(1 for r in results if r.status == "no_action")
    err = sum(1 for r in results if r.status == "error")
    logger.info(f"Done. ok={ok}  no_action={no}  error={err}")


def _task_sort_key(name: str) -> tuple[int, str]:
    """Numeric-aware sort: ``main2`` before ``main10`` before ``main100``.

    Lexical sort gives main1, main10, main100, main11, ... — confusing
    when the user expects 1, 2, 3 order. This extracts the numeric
    suffix and falls back to the full name as a tiebreaker.
    """
    if name.startswith("main") and name[4:].isdigit():
        return (int(name[4:]), name)
    return (10**9, name)


def _fmt_duration(seconds: float) -> str:
    """Format a duration as ``Ns`` / ``NmMs`` / ``NhMm``."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02d}s"
    return f"{seconds // 3600}h{(seconds % 3600) // 60:02d}m"


def _progress_bar(done: int, total: int, width: int = 40) -> str:
    """ASCII progress bar: ``[####------] 12.3% (61/493)``."""
    if total <= 0:
        return "[" + "-" * width + "]   0.0% (0/0)"
    pct = min(1.0, done / total)
    filled = int(round(pct * width))
    bar = "#" * filled + "-" * (width - filled)
    return f"[{bar}] {pct * 100:5.1f}% ({done}/{total})"


def cmd_verify(args: argparse.Namespace) -> None:
    """Statically verify the seed conversion from main_data.json."""
    from privacylens_live.tasks.verify import (
        print_report,
        verify_conversion,
    )

    report = verify_conversion(Path(args.data), Path(args.tasks_dir))
    print_report(report)

    out = (
        Path(args.report_out)
        if args.report_out
        else Path(args.tasks_dir) / "verify_report.json"
    )
    out.write_text(json.dumps(report, indent=2))
    logger.info(f"Wrote {out}")

    if report["exit_code"] != 0:
        raise SystemExit(report["exit_code"])


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Evaluate collected results.

    Runs the full hybrid evaluation pass:
      - rule-based leakage scan (always)
      - LLM-judge leakage + helpfulness (unless ``--rule-only``)
      - routing correctness, clarification-bucket split, per-channel /
        per-expected-type breakdowns, refusal-message leakage

    Judge calls are cached at ``<results_dir>/.eval_cache.json`` keyed
    by content hashes, so re-running after adding a metric or fixing
    the aggregator is free.
    """
    from privacylens_live.base.evaluator import (
        LLMJudgeEvaluator,
        evaluate_results_dir,
    )

    results_dir = Path(args.results_dir)
    tasks_dir = Path(args.tasks_dir)
    if not results_dir.exists() or not results_dir.is_dir():
        logger.error(f"Results dir not found: {results_dir}")
        raise SystemExit(1)

    # Build the judge, unless asked to skip it.
    judge: LLMJudgeEvaluator | None = None
    if not args.rule_only:
        config = Config.from_env()
        judge_model = args.judge_model or config.eval_model
        judge_base_url = args.judge_base_url or config.llm_base_url
        judge_api_version = args.judge_api_version or config.llm_api_version
        # Prefer explicit --judge-api-key, then fall back to the
        # extraction LLM key (handy for DeepSeek), then the main key.
        judge_api_key = (
            args.judge_api_key or config.extraction_llm_api_key or config.llm_api_key
        )
        if not judge_api_key:
            logger.error(
                "No API key found for the judge. Set LLM_API_KEY, "
                "EXTRACTION_LLM_API_KEY, or pass --judge-api-key."
            )
            raise SystemExit(1)
        logger.info(
            "Using LLM judge: model=%s base_url=%s api_version=%s",
            judge_model,
            judge_base_url,
            judge_api_version,
        )
        judge = LLMJudgeEvaluator(
            model=judge_model,
            api_key=judge_api_key,
            base_url=judge_base_url,
            api_version=judge_api_version,
        )
    else:
        logger.info("Rule-only mode — skipping LLM judge.")

    def _progress(done: int, total: int) -> None:
        pct = (done / total * 100) if total else 0.0
        logger.info("Evaluated %d/%d (%.1f%%)", done, total, pct)

    report, judgments = asyncio.run(
        evaluate_results_dir(
            results_dir=results_dir,
            tasks_dir=tasks_dir,
            judge=judge,
            max_concurrency=args.max_concurrency,
            use_cache=not args.no_cache,
            sanitize_trajectory=args.sanitize_trajectory,
            progress_callback=_progress,
        )
    )

    print(json.dumps(report, indent=2))

    report_path = (
        Path(args.report_out) if args.report_out else results_dir / "report.json"
    )
    report_path.write_text(json.dumps(report, indent=2))
    logger.info(f"Report saved to {report_path}")

    # Also dump per-task judgments so downstream analysis doesn't need
    # to re-parse result files.
    judgments_path = results_dir / "judgments.json"
    judgments_path.write_text(
        json.dumps(
            [
                {
                    "name": j.name,
                    "status": j.status,
                    "bucket": j.bucket,
                    "expected_action_type": j.expected_action_type,
                    "actual_tool": j.actual_tool,
                    "routing_correct": j.routing_correct,
                    "helpfulness_score": j.helpfulness_score,
                    "rule_leaked": j.rule_leaked,
                    "llm_leaked": j.llm_leaked,
                    "llm_per_item": j.llm_per_item,
                    "refusal_leaked": j.refusal_leaked,
                    "refusal_per_item": j.refusal_per_item,
                    "sensitive_items": j.sensitive_items,
                }
                for j in judgments
            ],
            indent=2,
            ensure_ascii=False,
        )
    )
    logger.info(f"Per-task judgments saved to {judgments_path}")

    # Post-hoc privacy flow analysis (optional)
    if getattr(args, "privacy_analysis", False):
        from privacylens_live.runner.privacy_posthoc import (
            run_posthoc_analysis,
        )

        privacy_config = Config.from_env()
        if not privacy_config.extraction_llm_api_key:
            logger.error(
                "EXTRACTION_LLM_API_KEY is not set. Required for --privacy-analysis."
            )
            raise SystemExit(1)

        result_files = sorted(results_dir.glob("*.json"))
        privacy_count = 0
        for rf in result_files:
            if rf.name.startswith("_") or rf.name in (
                "report.json",
                "judgments.json",
            ):
                continue
            events_file = rf.with_suffix(".events.json")
            if not events_file.exists():
                continue
            if rf.name.endswith(".privacy.json"):
                continue
            try:
                run_posthoc_analysis(rf, events_file, privacy_config)
                privacy_count += 1
            except Exception as exc:
                logger.warning(
                    "Privacy analysis failed for %s: %s",
                    rf.name,
                    exc,
                )
        logger.info(
            "Privacy analysis complete: %d results processed",
            privacy_count,
        )


def cmd_status(args: argparse.Namespace) -> None:
    """Show progress for a results directory.

    Reads every ``<results_dir>/<name>.json`` (skipping events sidecars,
    report.json, _summary.json, and any file that isn't a result-shape
    dict), groups by status, and reports the breakdown. If ``--tasks-dir``
    is also given, also reports how many tasks remain to be run.
    """
    if not args.results_dir:
        logger.error(
            "--results-dir is required and must not be empty. "
            "(If you're using a shell variable, make sure it's set.)"
        )
        raise SystemExit(1)
    results_dir = Path(args.results_dir)
    if not results_dir.exists() or not results_dir.is_dir():
        logger.error(f"Results dir not found or not a directory: {results_dir}")
        raise SystemExit(1)

    result_files = sorted(
        p
        for p in results_dir.glob("*.json")
        if not p.name.endswith(".events.json")
        and p.name not in ("report.json", "_summary.json")
    )

    by_status: dict[str, int] = {"ok": 0, "no_action": 0, "error": 0, "unknown": 0}
    error_names: list[str] = []
    no_action_names: list[str] = []
    total_tool_calls = 0
    total_clarifications = 0
    total_recovered_errors = 0
    skipped_non_result = 0

    for f in result_files:
        try:
            data = json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            by_status["unknown"] += 1
            continue
        # Defensive: skip files that aren't a result-shape dict (e.g.
        # someone pointed --results-dir at the project root, which has
        # main_data.json as a top-level list).
        if not isinstance(data, dict) or "status" not in data:
            skipped_non_result += 1
            continue
        status = data.get("status", "unknown")
        by_status[status] = by_status.get(status, 0) + 1
        if status == "error":
            error_names.append(data.get("name", f.stem))
        if status == "no_action":
            no_action_names.append(data.get("name", f.stem))
        stats = data.get("stats", {}) or {}
        total_tool_calls += stats.get("tool_call_count", 0)
        total_clarifications += stats.get("clarification_rounds", 0)
        total_recovered_errors += stats.get("errors_recovered", 0)

    completed = sum(by_status.values())
    print()
    print(f"Results dir: {results_dir}")
    print(f"  completed: {completed}")
    for s in ("ok", "no_action", "error", "unknown"):
        n = by_status.get(s, 0)
        pct = (n / completed * 100) if completed else 0
        print(f"    {s:9} {n:>4}  ({pct:5.1f}%)")
    if skipped_non_result:
        print(
            f"  (also found {skipped_non_result} non-result JSON file(s) "
            f"in this dir — skipped)"
        )

    if completed > 0:
        print()
        print("  per-task means:")
        print(f"    tool_calls          {total_tool_calls / completed:.1f}")
        print(f"    errors_recovered    {total_recovered_errors / completed:.1f}")
        print(f"    clarification_rounds {total_clarifications / completed:.2f}")

    if error_names:
        print()
        n_show = min(10, len(error_names))
        print(f"  errored ({len(error_names)}, showing first {n_show}):")
        for name in error_names[:n_show]:
            print(f"    {name}")
        if len(error_names) > n_show:
            print(f"    ... and {len(error_names) - n_show} more")

    summary_file = results_dir / "_summary.json"
    summary_data: dict | None = None
    if summary_file.exists():
        try:
            loaded = json.loads(summary_file.read_text())
            if isinstance(loaded, dict):
                summary_data = loaded
        except (json.JSONDecodeError, OSError):
            pass

    # Compute timing: prefer the summary file (batch finished), else
    # fall back to the earliest result file's mtime as the start.
    started_at: float | None = None
    finished_at: float | None = None
    if summary_data:
        started_at = summary_data.get("started_at_unix")
        finished_at = summary_data.get("finished_at_unix")
    if started_at is None and result_files:
        started_at = min(f.stat().st_mtime for f in result_files)

    if started_at is not None:
        end_ref = finished_at if finished_at is not None else time.time()
        elapsed = max(0.0, end_ref - started_at)
    else:
        elapsed = 0.0

    if args.tasks_dir:
        tasks_base = Path(args.tasks_dir)
        if tasks_base.exists():
            all_task_names = {
                d.name
                for d in tasks_base.iterdir()
                if d.is_dir() and (d / "task.json").exists()
            }
            done_names = {f.stem for f in result_files}
            remaining = all_task_names - done_names
            total_expected = len(all_task_names)

            print()
            print(f"  Progress: {_progress_bar(completed, total_expected)}")
            if elapsed > 0:
                print(f"  Elapsed:  {_fmt_duration(elapsed)}")
                if completed > 0:
                    mean_per_task = elapsed / completed
                    rate_per_min = (completed / elapsed) * 60
                    print(
                        f"  Rate:     {_fmt_duration(mean_per_task)} per task  "
                        f"({rate_per_min:.1f} tasks/min)"
                    )
                    if remaining:
                        eta_seconds = mean_per_task * len(remaining)
                        print(f"  ETA:      {_fmt_duration(eta_seconds)}")

            print()
            print(f"  vs {tasks_base}:")
            print(f"    total tasks expected: {total_expected}")
            print(f"    remaining to run:     {len(remaining)}")
            if remaining:
                print(
                    f"    (resume with: python -m privacylens_live run "
                    f"--resume --results-dir {results_dir})"
                )

    # If a _summary.json exists from a finished batch, surface it.
    if summary_data:
        print()
        print("  last batch summary:")
        for k, v in summary_data.items():
            print(f"    {k}: {v}")


def cmd_teardown(_args: argparse.Namespace) -> None:
    """Stop all Docker services."""
    logger.info("Stopping PrivacyLens-Live services...")
    subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), "down"],
        check=True,
    )
    logger.info("Services stopped.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="PrivacyLens-Live: Real-service privacy evaluation platform"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    sub.add_parser("setup", help="Start Docker services + bootstrap tokens")

    # write-env
    sub.add_parser("write-env", help="Regenerate .env from config.py")

    # generate
    gen = sub.add_parser("generate", help="Generate task directories")
    gen.add_argument("--data", default="main_data.json")
    gen.add_argument("--output", default="privacylens_live/tasks")

    # run
    run = sub.add_parser("run", help="Run agent on tasks")
    run.add_argument("--tasks-dir", default="privacylens_live/tasks")
    run.add_argument("--names", help="Comma-separated task names")
    run.add_argument("--range", help="Index range (e.g., 0-10)")
    run.add_argument("--model", help="Override LLM model")
    run.add_argument("--results-dir", default="results")
    run.add_argument(
        "--max-clarifications",
        type=int,
        default=3,
        help=(
            "Max times to auto-respond when the agent ends a turn on "
            "a clarification question instead of a tool call. Set to "
            "0 to disable (single-shot mode, matches the original "
            "PrivacyLens single-turn benchmark)."
        ),
    )
    run.add_argument(
        "--resume",
        action="store_true",
        help=(
            "Skip tasks where a successful result file already exists "
            "in --results-dir. Use this to continue an interrupted batch."
        ),
    )
    run.add_argument(
        "--retry-errors",
        action="store_true",
        help=(
            "Re-run tasks whose existing result file has status=error. "
            "Combine with --resume to keep already-OK tasks and only "
            "rerun the failed ones."
        ),
    )
    run.add_argument(
        "--prompt-variant",
        default="baseline",
        choices=[
            "baseline",
            "privacy_enhanced",
            "ci_reasoning",
            "ci_audit",
        ],
        help=(
            "Privacy mitigation level. "
            "'baseline' (L0): stock OpenHands SWE prompt, no privacy. "
            "'privacy_enhanced' (L1): privacy-conscious system prompt + "
            "persona suffix. "
            "'ci_reasoning' (L2): L1 + structured CI 5-tuple "
            "information-flow analysis and per-flow judgment in suffix. "
            "'ci_audit' (L3): L1 + PRIVACY_RISK_ASSESSMENT block + "
            "CI schema fields on write tools + DeepSeek extraction "
            "(requires --enable-privacy-analyzer). "
            "Recorded per-result as prompt_variant."
        ),
    )
    run.add_argument(
        "--disable-security-analyzer",
        action="store_true",
        help=(
            "Pass system_prompt_kwargs={'llm_security_analyzer': False} "
            "to the Agent, suppressing the SECURITY_RISK_ASSESSMENT "
            "block in the system prompt. This is an orthogonal axis to "
            "--prompt-variant. The tool-schema security_risk/summary "
            "fields remain (hard-coded in the SDK's make_llm_completion), "
            "but the model is no longer taught how to use them, which "
            "should reduce the 'sends only security_risk+summary' "
            "failure mode observed in the baseline. Recorded per-result "
            "as security_analyzer_disabled."
        ),
    )
    run.add_argument(
        "--enable-privacy-analyzer",
        action="store_true",
        help=(
            "Enable the contextual privacy analyzer: adds "
            "PRIVACY_RISK_ASSESSMENT block to the system prompt and "
            "injects four structured CI fields (data_type, data_subject, "
            "data_sender, data_recipient) into every non-readOnly tool's "
            "inputSchema. Also runs post-read DeepSeek extraction to "
            "decompose observation content into information flow units."
        ),
    )

    # verify
    ver = sub.add_parser("verify", help="Statically verify the seed conversion")
    ver.add_argument("--data", default="main_data.json")
    ver.add_argument("--tasks-dir", default="privacylens_live/tasks")
    ver.add_argument(
        "--report-out",
        default=None,
        help="Path to write JSON report (default: <tasks-dir>/verify_report.json)",
    )

    # status
    st = sub.add_parser(
        "status",
        help="Show progress for a results dir (live-monitor a running batch)",
    )
    st.add_argument("--results-dir", required=True)
    st.add_argument(
        "--tasks-dir",
        default=None,
        help="Optional: also report how many tasks remain to be run",
    )

    # evaluate
    ev = sub.add_parser("evaluate", help="Evaluate results")
    ev.add_argument("--results-dir", default="results")
    ev.add_argument(
        "--tasks-dir",
        default="privacylens_live/tasks",
        help=(
            "Directory of task.json files — needed to pull "
            "user_instruction for the helpfulness judge."
        ),
    )
    ev.add_argument(
        "--rule-only",
        action="store_true",
        help=(
            "Skip the LLM judge, run only the keyword-based leakage "
            "scan. Fast sanity check; no API calls."
        ),
    )
    ev.add_argument(
        "--judge-model",
        default=None,
        help=(
            "Override the judge model. Defaults to config.eval_model "
            "(azure/gpt-5.2). Use a different model from the one that "
            "produced the actions to avoid self-judging artifacts."
        ),
    )
    ev.add_argument(
        "--judge-base-url",
        default=None,
        help="Override the judge LLM base URL (e.g. for DeepSeek).",
    )
    ev.add_argument(
        "--judge-api-key",
        default=None,
        help="Override the judge LLM API key.",
    )
    ev.add_argument(
        "--judge-api-version",
        default=None,
        help="Override the judge LLM API version (empty string to clear).",
    )
    ev.add_argument(
        "--max-concurrency",
        type=int,
        default=16,
        help=(
            "Max concurrent LLM judge calls. Bumps this for faster "
            "evaluation, but watch for provider rate limits."
        ),
    )
    ev.add_argument(
        "--no-cache",
        action="store_true",
        help=(
            "Ignore .eval_cache.json and re-call the LLM for every "
            "prompt. Useful after changing the prompts."
        ),
    )
    ev.add_argument(
        "--sanitize-trajectory",
        action="store_true",
        help=(
            "Strip long text fields (body, markdown, content) from "
            "trajectory observations before sending to the helpfulness "
            "judge. Avoids Azure content-policy filter blocks."
        ),
    )
    ev.add_argument(
        "--report-out",
        default=None,
        help="Path to write the report JSON (default: <results-dir>/report.json)",
    )
    ev.add_argument(
        "--privacy-analysis",
        action="store_true",
        help=(
            "Run post-hoc privacy flow analysis on each result. "
            "Extracts CI flows from read observations (via DeepSeek), "
            "captures write-tool CI fields from tool_call arguments, "
            "and runs write-time checks. Produces a .privacy.json "
            "sidecar for each result file. Requires EXTRACTION_LLM_API_KEY."
        ),
    )

    # teardown
    sub.add_parser("teardown", help="Stop Docker services")

    args = parser.parse_args()
    cmd_map = {
        "setup": cmd_setup,
        "write-env": cmd_write_env,
        "generate": cmd_generate,
        "verify": cmd_verify,
        "run": cmd_run,
        "status": cmd_status,
        "evaluate": cmd_evaluate,
        "teardown": cmd_teardown,
    }
    cmd_map[args.command](args)


if __name__ == "__main__":
    main()
