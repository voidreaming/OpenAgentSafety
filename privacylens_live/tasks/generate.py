"""Generate per-task directories from main_data.json.

Reads the PrivacyLens dataset, parses each entry's executable_trajectory,
and writes per-task directories with task.json and seed_data/*.json files.

Usage:
    python -m privacylens_live.tasks.generate \\
        --data main_data.json --output privacylens_live/tasks
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path

from privacylens_live.base.trajectory_parser import (
    TrajectoryStep,
    get_seed_observations,
    parse_trajectory,
)
from privacylens_live.config import ACTION_TO_SERVICE


logger = logging.getLogger("generate")


def _normalize_radicale_event(event: dict) -> dict:
    """Normalize a calendar event into the seeder's expected shape.

    Source data uses several shapes for the same event:
    - flat: ``{event_name, start_time, end_time, content, location, ...}``
    - structured: ``{summary, description, start: {dateTime}, end: {dateTime}}``

    The seeder only reads the flat shape, so anything else is silently
    dropped without normalization. This collapses both into the flat
    form.
    """
    name = (
        event.get("event_name") or event.get("summary") or event.get("title") or "Event"
    )
    start = event.get("start_time", "")
    if not start and isinstance(event.get("start"), dict):
        start = event["start"].get("dateTime") or event["start"].get("date") or ""
    end = event.get("end_time", "")
    if not end and isinstance(event.get("end"), dict):
        end = event["end"].get("dateTime") or event["end"].get("date") or ""
    return {
        "event_id": str(event.get("event_id") or event.get("id") or name),
        "event_name": name,
        "content": event.get("content") or event.get("description", ""),
        "start_time": start,
        "end_time": end,
        "location": event.get("location", ""),
        "attendees": event.get("attendees", []),
    }


def _warn_unknown_shape(step: TrajectoryStep, service: str) -> None:
    """Log a warning when an observation falls into the raw fallback path.

    This is the hint that the source data has a shape the extractor
    doesn't recognize and the resulting seed file will contain a
    record the seeder cannot consume. The verify command surfaces
    these counts; this warning makes them visible during ``generate``.
    """
    obs = step.observation
    if isinstance(obs, dict):
        shape = f"keys={list(obs.keys())}"
    else:
        shape = f"type={type(obs).__name__}"
    logger.warning(
        "Unknown observation shape for %s → %s (%s); falling back to raw_observation",
        step.action_name,
        service,
        shape,
    )


# PrivacyLens toolkit name → MCP server name mapping.
# Used only during task generation to compute dependencies.
_TOOLKIT_TO_MCP: dict[str, str] = {
    "NotionManager": "bookstack",
    "Messenger": "mattermost",
    "Slack": "rocketchat",
    "Gmail": "mailpit",
    "FacebookManager": "gotosocial",
    "GoogleCalendar": "radicale",
    "ZoomManager": "bookstack",
    "GoogleFormFiller": "bookstack",
}


def _observation_to_seed_records(step: TrajectoryStep, service: str) -> list[dict]:
    """Convert a trajectory step's observation into seed records.

    Extracts the actual data items (pages, messages, emails, events)
    from the observation JSON and returns them as a list of records
    ready to be injected into the service.
    """
    obs = step.observation
    if isinstance(obs, str):
        # Generic string-shaped fallback: use the same sentinel
        # key as the per-service fallbacks so verify counts it.
        return [{"raw_observation": obs, "action": step.action_name}]

    records = []

    if service == "bookstack":
        # Multiple shapes here:
        #   NotionManagerSearchContent → {"results": [{id,title,content}]}
        #   NotionManagerReadPage      → {"page_content": "<plain text>"}
        #   ZoomManagerSearchTranscript→ {"search_results": ["snippet", ...]}
        if "results" in obs:
            for item in obs["results"]:
                records.append(
                    {
                        "id": str(item.get("id", "")),
                        "title": item.get("title", ""),
                        "content": item.get("content", ""),
                        "tags": item.get("tags", []),
                    }
                )
        elif "page_content" in obs:
            content = obs["page_content"]
            if not isinstance(content, str):
                content = json.dumps(content)
            # Source has no title — synthesize from first line so the
            # page is discoverable via list_pages / search_pages.
            first_line = content.split("\n", 1)[0].strip()
            title = first_line[:80] if first_line else "Untitled Page"
            records.append(
                {
                    "id": "",
                    "title": title,
                    "content": content,
                    "tags": [],
                }
            )
        elif "search_results" in obs:
            snippets = obs["search_results"]
            if isinstance(snippets, list):
                content = "\n\n".join(str(s) for s in snippets)
                records.append(
                    {
                        "id": "",
                        "title": "Meeting transcript search results",
                        "content": content,
                        "tags": [],
                    }
                )
            else:
                _warn_unknown_shape(step, service)
                records.append({"raw_observation": obs, "action": step.action_name})
        elif "transcript" in obs:
            # ZoomManagerGetMeetingTranscript returns a single transcript.
            transcript = obs["transcript"]
            if isinstance(transcript, list):
                content = "\n".join(str(s) for s in transcript)
            else:
                content = str(transcript)
            records.append(
                {
                    "id": "",
                    "title": "Meeting transcript",
                    "content": content,
                    "tags": [],
                }
            )
        else:
            _warn_unknown_shape(step, service)
            records.append({"raw_observation": obs, "action": step.action_name})

    elif service == "mattermost":
        # MessengerReceiveMessage has {"messages": [...]} with full
        # sender_id/time/message records. MessengerSearchInChat has
        # {"results": [...]} with only message_id+message — sender
        # and time aren't carried in the source data.
        if "messages" in obs:
            inner = obs["messages"]
            # Source occasionally double-wraps: {"messages": [{"messages": [...]}]}
            # (e.g. main324). Unwrap one level when we see it.
            if (
                len(inner) == 1
                and isinstance(inner[0], dict)
                and "messages" in inner[0]
            ):
                inner = inner[0]["messages"]
            for msg in inner:
                records.append(
                    {
                        "message_id": msg.get("message_id", ""),
                        "sender_id": msg.get("sender_id", ""),
                        "time": msg.get("time", ""),
                        "message": msg.get("message", ""),
                    }
                )
        elif "results" in obs:
            for msg in obs["results"]:
                records.append(
                    {
                        "message_id": msg.get("message_id", ""),
                        "sender_id": "",
                        "time": "",
                        "message": msg.get("message", ""),
                    }
                )
        else:
            _warn_unknown_shape(step, service)
            records.append({"raw_observation": obs, "action": step.action_name})

    elif service == "rocketchat":
        # SlackSearchMessage has {"messages": [...]}. Slack uses
        # `from`/`content`/`in`/`timestamp` as the canonical keys —
        # the older names are kept as fallbacks for hand-authored
        # fixtures, but real data hits the Slack-native names first.
        if "messages" in obs:
            for msg in obs["messages"]:
                records.append(
                    {
                        "message_id": msg.get("message_id", ""),
                        "sender_id": msg.get(
                            "from", msg.get("sender_id", msg.get("user_id", ""))
                        ),
                        "time": msg.get("timestamp", msg.get("time", "")),
                        "message": msg.get(
                            "content", msg.get("message", msg.get("text", ""))
                        ),
                        "channel": msg.get("in", msg.get("channel", "")),
                    }
                )
        elif "profile" in obs:
            records.append(
                {
                    "type": "user_profile",
                    "profile": obs["profile"],
                }
            )
        elif "results" in obs:
            for item in obs["results"]:
                records.append(item)
        else:
            _warn_unknown_shape(step, service)
            records.append({"raw_observation": obs, "action": step.action_name})

    elif service == "mailpit":
        # GmailSearchEmails has {"emails": [...]} or {"results": [...]}
        if "emails" in obs:
            for email in obs["emails"]:
                records.append(email)
        elif "results" in obs:
            for item in obs["results"]:
                records.append(item)
        elif "contacts" in obs:
            for contact in obs["contacts"]:
                records.append({"type": "contact", **contact})
        else:
            # Single email read
            records.append(obs)

    elif service == "radicale":
        # GoogleCalendarSearchEvents has {"events": [...]}
        if "events" in obs:
            for event in obs["events"]:
                if isinstance(event, str):
                    records.append({"event_id": event})
                else:
                    records.append(_normalize_radicale_event(event))
        elif "event_details" in obs:
            for event in obs["event_details"]:
                records.append(_normalize_radicale_event(event))
        else:
            _warn_unknown_shape(step, service)
            records.append({"raw_observation": obs, "action": step.action_name})

    elif service == "gotosocial":
        if "error" in obs:
            # Pre-recorded error response (e.g., search returned no
            # results). Nothing to seed — the empty state is the truth.
            pass
        elif "profile" in obs:
            records.append({"type": "profile", **obs["profile"]})
        elif "posts" in obs:
            for post in obs["posts"]:
                records.append(post)
        elif "results" in obs:
            for item in obs["results"]:
                records.append(item)
        else:
            _warn_unknown_shape(step, service)
            records.append({"raw_observation": obs, "action": step.action_name})

    else:
        _warn_unknown_shape(step, service)
        records.append({"raw_observation": obs, "action": step.action_name})

    return records


# Rewrite commercial service names to match our real services.
_INSTRUCTION_REWRITES = [
    ("Notion", "BookStack"),
    ("Facebook post", "GoToSocial post"),
    ("Facebook", "GoToSocial"),
    ("Gmail", "Mailpit"),
    ("Slack", "RocketChat"),
    ("Messenger", "Mattermost"),
    ("Google Calendar", "Radicale"),
    ("Zoom", "BookStack"),
    ("Google Drive", "BookStack"),
]


def _rewrite_instruction(instruction: str) -> str:
    """Replace commercial service names with our real service names."""
    result = instruction
    for old, new in _INSTRUCTION_REWRITES:
        result = result.replace(old, new)
    return result


def generate_task(entry: dict, output_dir: Path) -> Path:
    """Convert a main_data.json entry into a task directory.

    Returns the path to the created task directory.
    """
    name = entry["name"]
    task_dir = output_dir / name
    seed_dir = task_dir / "seed_data"
    seed_dir.mkdir(parents=True, exist_ok=True)

    # Parse trajectory and extract seed observations
    steps = parse_trajectory(entry["trajectory"]["executable_trajectory"])
    seed_steps = get_seed_observations(steps)

    # Group by service and extract records
    service_records: dict[str, list[dict]] = defaultdict(list)
    for step in seed_steps:
        service = ACTION_TO_SERVICE.get(step.action_name)
        if service is None:
            print(
                f"  Warning: unknown action {step.action_name} in {name}",
                file=sys.stderr,
            )
            continue
        records = _observation_to_seed_records(step, service)
        service_records[service].extend(records)

    # Write seed data files per service
    for service, records in service_records.items():
        seed_file = seed_dir / f"{service}.json"
        seed_file.write_text(json.dumps(records, indent=2, ensure_ascii=False))

    # Compute dependencies (which MCP servers are needed)
    toolkits = entry["trajectory"]["toolkits"]
    dependencies = sorted(
        {_TOOLKIT_TO_MCP[t] for t in toolkits if t in _TOOLKIT_TO_MCP}
    )

    # Write task.json
    task_spec = {
        "name": name,
        "user_instruction": _rewrite_instruction(
            entry["trajectory"]["user_instruction"]
        ),
        "user_name": entry["trajectory"].get("user_name", "John Doe"),
        "user_email": entry["trajectory"].get("user_email", "john.doe@gmail.com"),
        "toolkits": toolkits,
        "dependencies": dependencies,
        "final_action_type": entry["trajectory"]["final_action"],
        "sensitive_info_items": entry["trajectory"]["sensitive_info_items"],
        "seed": entry["seed"],
        "vignette": entry["vignette"],
    }
    (task_dir / "task.json").write_text(
        json.dumps(task_spec, indent=2, ensure_ascii=False)
    )

    return task_dir


def generate_all(data_path: Path, output_dir: Path) -> None:
    """Generate task directories for all entries in main_data.json."""
    with open(data_path) as f:
        data = json.load(f)

    print(f"Generating {len(data)} tasks from {data_path} → {output_dir}/")

    for i, entry in enumerate(data):
        task_dir = generate_task(entry, output_dir)
        seed_files = list((task_dir / "seed_data").glob("*.json"))
        if (i + 1) % 100 == 0 or i == 0:
            print(
                f"  [{i + 1}/{len(data)}] {entry['name']}: {len(seed_files)} seed files"
            )

    print(f"Done. Generated {len(data)} task directories.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate per-task directories from main_data.json"
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=Path("main_data.json"),
        help="Path to main_data.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("privacylens_live/tasks"),
        help="Output directory for task directories",
    )
    args = parser.parse_args()
    generate_all(args.data, args.output)


if __name__ == "__main__":
    main()
