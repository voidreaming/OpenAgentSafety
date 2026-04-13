"""Post-hoc privacy flow extraction and write-time CI checking.

Because the agent runs inside a Docker container with a stock SDK image
(no privacy analyzer), the privacy analysis happens post-hoc on the host
after the events are collected:

1. Parse observation events to extract text content from read tools
2. Send each observation to DeepSeek-V3.2 for information flow extraction
3. For write tool actions, extract CI fields from tool_call.arguments
4. Run write-time check: compare accumulated flows against write content
5. Annotate the result file with extracted flows and check results
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pydantic import SecretStr

from privacylens_live.config import Config


logger = logging.getLogger("privacy_posthoc")


def _strip_markdown_fences(text: str) -> str:
    """Strip ```json ... ``` markdown fences from LLM output."""
    import re

    text = text.strip()
    m = re.match(r"^```(?:json)?\s*\n?(.*?)```\s*$", text, re.DOTALL)
    if m:
        return m.group(1).strip()
    return text


def _extract_flows_from_text(analyzer, text: str, tool_name: str):
    """Call extract_flows without constructing an Observation.

    The SDK's Observation is a discriminated-union ABC that can't be
    directly instantiated. For post-hoc analysis we bypass it and call
    the LLM completion directly using the analyzer's internal method.
    """
    from openhands.sdk.llm.message import Message, TextContent
    from openhands.sdk.privacy.flow import InformationFlow
    from openhands.sdk.privacy.llm_analyzer import EXTRACTION_SYSTEM_PROMPT

    response = analyzer.llm.completion(
        messages=[
            Message(
                role="system",
                content=[TextContent(text=EXTRACTION_SYSTEM_PROMPT)],
            ),
            Message(
                role="user",
                content=[TextContent(text=f"Tool: {tool_name}\n\nOutput:\n{text}")],
            ),
        ],
    )

    raw_text = ""
    for block in response.message.content:
        if isinstance(block, TextContent):
            raw_text += block.text

    import json as _json

    raw_text = _strip_markdown_fences(raw_text)
    try:
        raw = _json.loads(raw_text)
    except _json.JSONDecodeError:
        return []

    items = raw if isinstance(raw, list) else raw.get("flows", [])
    return [
        InformationFlow(
            data_type=item["data_type"],
            data_subject=item["data_subject"],
        )
        for item in items
        if "data_type" in item and "data_subject" in item
    ]


def run_posthoc_analysis(
    result_file: Path,
    events_file: Path,
    config: Config,
) -> dict:
    """Run post-hoc privacy analysis on a completed task's events.

    Returns a dict with:
    - observation_flows: list of {tool_name, flows: [{data_type, data_subject}]}
    - write_ci_fields: list of {tool_name, data_type, data_subject, data_sender,
                                data_recipient} extracted from tool_call args
    - write_check_results: list of {tool_name, transmitted_flows: [...]}
    """
    from openhands.sdk import LLM
    from openhands.sdk.privacy import LLMPrivacyAnalyzer
    from openhands.sdk.privacy.flow import InformationFlow

    events = json.loads(events_file.read_text())

    # Build the extraction LLM
    extraction_llm = LLM(
        model=config.extraction_llm_model,
        api_key=SecretStr(config.extraction_llm_api_key),
        base_url=config.extraction_llm_base_url,
        usage_id="privacylens-live-posthoc",
    )
    analyzer = LLMPrivacyAnalyzer(llm=extraction_llm)

    # Phase 1: Extract flows from read-tool observations
    observation_flows: list[dict] = []
    accumulated_flows: list[InformationFlow] = []

    for event in events:
        kind = event.get("kind", "")
        tool_name = event.get("tool_name", "")

        if kind == "ObservationEvent":
            # Extract observation text from the event
            obs_data = event.get("observation", {})
            content_blocks = obs_data.get("content", [])
            text_parts = []
            for block in content_blocks:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
            obs_text = "\n".join(text_parts)

            if obs_text and not obs_data.get("is_error", False):
                try:
                    flows = _extract_flows_from_text(analyzer, obs_text, tool_name)
                    if flows:
                        observation_flows.append(
                            {
                                "tool_name": tool_name,
                                "flows": [f.model_dump() for f in flows],
                            }
                        )
                        accumulated_flows.extend(flows)
                except Exception as exc:
                    logger.warning(
                        "Post-hoc extraction failed for %s: %s",
                        tool_name,
                        exc,
                    )

    # Phase 2: Extract CI fields from write-tool action arguments
    write_ci_fields: list[dict] = []
    write_check_results: list[dict] = []
    ci_field_names = (
        "data_type",
        "data_subject",
        "data_sender",
        "data_recipient",
    )

    for event in events:
        kind = event.get("kind", "")
        tool_name = event.get("tool_name", "")
        tool_call = event.get("tool_call")

        if kind == "ActionEvent" and tool_call:
            args_str = tool_call.get("arguments", "{}")
            try:
                args = json.loads(args_str) if isinstance(args_str, str) else args_str
            except json.JSONDecodeError:
                continue

            # Check if this action has CI fields (write tool)
            ci = {k: args.get(k) for k in ci_field_names}
            if any(ci.values()):
                ci["tool_name"] = tool_name
                write_ci_fields.append(ci)

                # Run write-time check against accumulated flows
                if accumulated_flows:
                    # Get the write content from the args
                    write_content = (
                        args.get("content")
                        or args.get("message")
                        or args.get("body")
                        or args.get("text")
                        or args.get("markdown")
                        or ""
                    )
                    if write_content:
                        try:
                            check = analyzer.check_write_action(
                                write_content, accumulated_flows
                            )
                            write_check_results.append(
                                {
                                    "tool_name": tool_name,
                                    "transmitted_flows": [
                                        tf.model_dump()
                                        for tf in check.transmitted_flows
                                    ],
                                }
                            )
                        except Exception as exc:
                            logger.warning(
                                "Post-hoc write check failed for %s: %s",
                                tool_name,
                                exc,
                            )

    analysis = {
        "observation_flows": observation_flows,
        "write_ci_fields": write_ci_fields,
        "write_check_results": write_check_results,
        "total_accumulated_flows": len(accumulated_flows),
    }

    # Write analysis alongside the result
    analysis_file = result_file.with_suffix(".privacy.json")
    analysis_file.write_text(json.dumps(analysis, indent=2))
    logger.info(
        "Post-hoc analysis for %s: %d read flows, %d write CI, %d checks → %s",
        result_file.stem,
        len(accumulated_flows),
        len(write_ci_fields),
        len(write_check_results),
        analysis_file.name,
    )
    return analysis
