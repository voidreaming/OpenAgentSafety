"""Parse PrivacyLens executable_trajectory strings into structured steps.

The trajectory format is:
    Action: <ActionName>
    Action Input: <JSON>
    Observation: <JSON or text>

Multiple steps are separated by double newlines. Observations can be
multi-line pretty-printed JSON.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass
class TrajectoryStep:
    """A single action-observation step from a PrivacyLens trajectory."""

    action_name: str
    action_input: dict
    observation: dict | str


def _try_parse_json(text: str) -> dict | str:
    """Try to parse text as JSON, return raw string on failure."""
    text = text.strip()
    if not text:
        return text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Handle escaped JSON (e.g., main153: {\\"keywords\\": ...})
    try:
        unescaped = text.replace('\\"', '"')
        return json.loads(unescaped)
    except (json.JSONDecodeError, ValueError):
        pass
    return text


def _clean_action_name(raw: str) -> str:
    """Clean action name, removing trailing quotes or whitespace."""
    return raw.strip().strip('"').strip("'").strip()


def parse_trajectory(text: str) -> list[TrajectoryStep]:
    """Parse an executable_trajectory string into structured steps.

    Handles edge cases found in the dataset:
    - Multi-line pretty-printed JSON observations
    - Escaped JSON in action inputs
    - Malformed action names with trailing quotes
    - Inconsistent whitespace
    """
    if not text or not text.strip():
        return []

    # Normalize: ensure every "Action: " is preceded by a newline.
    # main313 has a trajectory like ``...emails": [...]}Action: ...``
    # with no newline after the JSON close, which would otherwise
    # bundle two actions into a single block.
    text = re.sub(r"(?<!\n)Action: ", r"\nAction: ", text)

    steps: list[TrajectoryStep] = []

    # Split into action blocks. Each block starts with "Action: "
    # Use a pattern that captures from "Action:" to the next "Action:" or end
    blocks = re.split(r"\n(?=Action: )", text)

    for block in blocks:
        block = block.strip()
        if not block:
            continue

        # Extract action name
        action_match = re.match(r"Action:\s*(.+?)(?:\n|$)", block)
        if not action_match:
            continue
        action_name = _clean_action_name(action_match.group(1))

        # Extract action input
        input_match = re.search(
            r"Action Input:\s*(.*?)(?:\nObservation:|\Z)",
            block,
            re.DOTALL,
        )
        if input_match:
            raw_input = input_match.group(1).strip()
            action_input = _try_parse_json(raw_input)
            if isinstance(action_input, str):
                action_input = {"raw": action_input}
        else:
            action_input = {}

        # Extract observation
        obs_match = re.search(
            r"Observation:\s*(.*)",
            block,
            re.DOTALL,
        )
        if obs_match:
            raw_obs = obs_match.group(1).strip()
            observation = _try_parse_json(raw_obs)
        else:
            observation = ""

        steps.append(
            TrajectoryStep(
                action_name=action_name,
                action_input=action_input if isinstance(action_input, dict) else {},
                observation=observation,
            )
        )

    return steps


def get_seed_observations(steps: list[TrajectoryStep]) -> list[TrajectoryStep]:
    """Filter steps to only those with observation data (for seeding).

    Excludes 'send' actions since those are the agent's output, not seed data.
    """
    send_actions = {
        "MessengerSendMessage",
        "GmailSendEmail",
        "SlackSendMessage",
        "FacebookManagerCreatePost",
        "NotionManagerCreatePage",
        "NotionManagerEditPage",
    }
    return [s for s in steps if s.action_name not in send_actions and s.observation]
