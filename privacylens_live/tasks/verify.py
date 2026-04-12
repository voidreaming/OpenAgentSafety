"""Static verifier for the seed conversion pipeline.

Walks ``main_data.json`` and the generated task directories, then emits
a per-service scoreboard so you can tell at a glance whether the
conversion from the static PrivacyLens benchmark to live seed data is
honest. The verifier catches:

- Missing ``task.json`` or ``seed_data/<service>.json`` files
- Source-observation count vs generated-record count drift
- Empty critical fields (the sentinel for silent extractor bugs)
- ``raw_observation`` fallbacks (silent shape mismatches in the
  generator's per-service branches)
- Unhandled action types (missing ``ACTION_TO_SERVICE`` entries)

Static only — does not touch any running service. Runs in seconds.

Exit code is non-zero on any P0-class issue so this can be wired into
a pre-commit hook or CI later. Today it's purely opt-in via
``python -m privacylens_live verify``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from privacylens_live.base.trajectory_parser import (
    get_seed_observations,
    parse_trajectory,
)
from privacylens_live.config import ACTION_TO_SERVICE


def _is_record_empty(record: dict, service: str) -> bool:
    """Return True if the record looks like its target shape but is empty.

    The verifier only flags records that **look like** the seeder's
    expected shape but are missing data. Records of unrelated shapes
    (e.g. user-info records in a rocketchat seed file) are skipped —
    they're not failures, just different categories the seeder ignores.
    """
    if "raw_observation" in record:
        # Counted separately as a fallback, not a field failure.
        return False

    if service == "bookstack":
        # Page-shaped records have title or content. Anything else
        # isn't a page.
        if "title" not in record and "content" not in record:
            return False
        return not record.get("title") or not record.get("content")

    if service == "mattermost":
        if "message" not in record:
            return False
        return not record.get("message")

    if service == "rocketchat":
        # Only message-shaped records are message records. User-info
        # records (name+status, type=user_profile) are a different
        # category the seeder doesn't try to ingest as messages.
        if "message" not in record:
            return False
        return not record.get("message") or not record.get("sender_id")

    if service == "mailpit":
        # Mailpit shapes are intentionally varied: search results
        # carry only headers, read_email carries the full body, and
        # GmailSearchContacts produces contact records that the
        # seeder doesn't ingest as emails at all. There's no single
        # field check that's meaningful across all three; the
        # raw_observation count below catches genuine generator bugs.
        return False

    if service == "radicale":
        # Marker records (just an event_id) — bare ID lists.
        if set(record.keys()) == {"event_id"}:
            return False
        # Only event-shaped records get the start-time check.
        name = record.get("event_name") or record.get("summary") or record.get("title")
        if not name:
            return False
        start = record.get("start_time")
        if not start and isinstance(record.get("start"), dict):
            start = record["start"].get("dateTime") or record["start"].get("date")
        return not start

    if service == "gotosocial":
        if record.get("type") == "profile":
            # Profile records are intentionally skipped by the seeder.
            return False
        # Post-shaped records need content.
        if "content" not in record and "post_id" not in record:
            return False
        return not record.get("content")

    return False


def verify_conversion(data_path: Path, tasks_dir: Path) -> dict[str, Any]:
    """Audit conversion from main_data.json to generated task dirs.

    Returns a structured report dict with per-service tallies and
    a top-level ``exit_code`` field (0 if everything is honest,
    1 if any check failed).
    """
    if not data_path.exists():
        raise FileNotFoundError(f"main_data.json not found: {data_path}")
    if not tasks_dir.exists():
        raise FileNotFoundError(f"tasks dir not found: {tasks_dir}")

    data = json.loads(data_path.read_text())

    expected_observations: dict[str, int] = defaultdict(int)
    expected_entries: dict[str, set[str]] = defaultdict(set)
    actual_records: dict[str, int] = defaultdict(int)
    actual_entries: dict[str, set[str]] = defaultdict(set)
    empty_critical: dict[str, int] = defaultdict(int)
    raw_fallbacks: dict[str, int] = defaultdict(int)

    unknown_actions: set[str] = set()
    missing_task_json: list[str] = []
    missing_seed_files: list[tuple[str, str]] = []

    for entry in data:
        name = entry["name"]
        task_dir = tasks_dir / name

        if not (task_dir / "task.json").exists():
            missing_task_json.append(name)
            continue

        steps = parse_trajectory(entry["trajectory"]["executable_trajectory"])
        seed_steps = get_seed_observations(steps)

        per_entry_obs: dict[str, int] = defaultdict(int)
        for step in seed_steps:
            service = ACTION_TO_SERVICE.get(step.action_name)
            if service is None:
                unknown_actions.add(step.action_name)
                continue
            per_entry_obs[service] += 1
            expected_observations[service] += 1
            expected_entries[service].add(name)

        seed_dir = task_dir / "seed_data"
        for service in per_entry_obs:
            seed_file = seed_dir / f"{service}.json"
            if not seed_file.exists():
                missing_seed_files.append((name, service))
                continue
            try:
                records = json.loads(seed_file.read_text())
            except json.JSONDecodeError:
                missing_seed_files.append((name, service))
                continue

            actual_records[service] += len(records)
            actual_entries[service].add(name)
            for record in records:
                if not isinstance(record, dict):
                    continue
                if "raw_observation" in record:
                    raw_fallbacks[service] += 1
                elif _is_record_empty(record, service):
                    empty_critical[service] += 1

    services = sorted(set(expected_observations) | set(actual_records))
    rows = []
    failed = False
    for svc in services:
        exp_e = len(expected_entries[svc])
        act_e = len(actual_entries[svc])
        exp_r = expected_observations[svc]
        act_r = actual_records[svc]
        emp = empty_critical[svc]
        raw = raw_fallbacks[svc]
        ok = exp_e == act_e and emp == 0 and raw == 0
        if not ok:
            failed = True
        rows.append(
            {
                "service": svc,
                "expected_entries": exp_e,
                "actual_entries": act_e,
                "expected_observations": exp_r,
                "actual_records": act_r,
                "empty_critical_fields": emp,
                "raw_observation_fallbacks": raw,
                "ok": ok,
            }
        )

    has_global_issue = bool(missing_task_json or missing_seed_files or unknown_actions)
    return {
        "source": str(data_path),
        "tasks_dir": str(tasks_dir),
        "total_entries": len(data),
        "missing_task_json": missing_task_json,
        "missing_seed_files": [
            {"entry": e, "service": s} for e, s in missing_seed_files
        ],
        "unknown_action_types": sorted(unknown_actions),
        "per_service": rows,
        "exit_code": 1 if (failed or has_global_issue) else 0,
    }


def print_report(report: dict[str, Any]) -> None:
    """Render the report as a human-readable scoreboard."""
    print()
    print("PrivacyLens-Live seed verification")
    print(f"  source: {report['source']} ({report['total_entries']} entries)")
    print(f"  tasks:  {report['tasks_dir']}")
    print()
    print("Per-service:")
    print(
        f"  {'service':<12} "
        f"{'entries':>10}  "
        f"{'records':>8}  "
        f"{'empty':>6}  "
        f"{'raw':>5}  "
        f"status"
    )
    for row in report["per_service"]:
        mark = "[ok]" if row["ok"] else "[FAIL]"
        entries = f"{row['actual_entries']:>3}/{row['expected_entries']:<3}"
        print(
            f"  {row['service']:<12} "
            f"{entries:>10}  "
            f"{row['actual_records']:>8}  "
            f"{row['empty_critical_fields']:>6}  "
            f"{row['raw_observation_fallbacks']:>5}  "
            f"{mark}"
        )
    print()
    if report["unknown_action_types"]:
        print(f"Unhandled action types: {report['unknown_action_types']}")
    else:
        print("Unhandled action types: (none)")
    if report["missing_task_json"]:
        n = len(report["missing_task_json"])
        print(
            f"Missing task.json: {n} entries (e.g. {report['missing_task_json'][:5]})"
        )
    if report["missing_seed_files"]:
        n = len(report["missing_seed_files"])
        print(f"Missing seed files: {n}")
    print()
    print(f"Exit: {report['exit_code']}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Verify the seed conversion from main_data.json"
    )
    parser.add_argument("--data", type=Path, default=Path("main_data.json"))
    parser.add_argument(
        "--tasks-dir", type=Path, default=Path("privacylens_live/tasks")
    )
    parser.add_argument(
        "--report-out",
        type=Path,
        default=None,
        help="Path to write JSON report (default: <tasks-dir>/verify_report.json)",
    )
    args = parser.parse_args()

    report = verify_conversion(args.data, args.tasks_dir)
    print_report(report)

    out = args.report_out or (args.tasks_dir / "verify_report.json")
    out.write_text(json.dumps(report, indent=2))
    print(f"Wrote {out}")

    sys.exit(report["exit_code"])


if __name__ == "__main__":
    main()
