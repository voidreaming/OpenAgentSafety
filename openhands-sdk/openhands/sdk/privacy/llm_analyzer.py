from __future__ import annotations

import json

from openhands.sdk.llm import LLM
from openhands.sdk.llm.message import Message, TextContent
from openhands.sdk.logger import get_logger
from openhands.sdk.privacy.analyzer import PrivacyAnalyzerBase
from openhands.sdk.privacy.flow import (
    InformationFlow,
    PrivacyCheckResult,
    TransmittedFlow,
)
from openhands.sdk.tool.schema import Observation


logger = get_logger(__name__)

EXTRACTION_SYSTEM_PROMPT = (
    "You are an information flow analyzer. Given tool output from a "
    "communication service (chat, email, social media, calendar), "
    "decompose all the information into unit pieces.\n\n"
    "For each piece of information, return:\n"
    "- data_type: the specific kind of information "
    "(e.g., 'medical diagnosis', 'project deadline', "
    "'meeting schedule', 'salary', 'home address', "
    "'team assignment')\n"
    "- data_subject: the person or entity this information "
    "is about\n\n"
    "Return a JSON array of objects. If no meaningful "
    "information is found, return an empty array [].\n\n"
    "Decompose thoroughly -- break compound information into "
    "separate units. For example, a team status message "
    "mentioning Alice's leave and Bob's project deadline "
    "should produce two entries, not one."
)

CHECK_SYSTEM_PROMPT = (
    "You are an information flow checker. You are given:\n"
    "1. An inventory of information units collected from prior reads\n"
    "2. A message that an agent is about to send\n\n"
    "Identify which information units from the inventory are being "
    "transmitted in the message. For each transmitted unit, return:\n"
    "- data_type: the kind of information being transmitted\n"
    "- data_subject: whose data it is\n"
    "- data_recipient: who will see the message\n\n"
    "Return a JSON array of objects. If no information from the "
    "inventory is being transmitted, return an empty array [].\n\n"
    "Be precise: only flag information that actually appears in the "
    "message content, not information that was collected but omitted."
)


class LLMPrivacyAnalyzer(PrivacyAnalyzerBase):
    """Privacy analyzer that uses a lightweight LLM to extract flows.

    Takes an SDK ``LLM`` instance for the extraction model.  The caller
    configures the LLM with the appropriate model/endpoint/credentials
    (e.g. DeepSeek-V3.2 via Azure for cost-efficient extraction).
    """

    llm: LLM

    def extract_flows(
        self,
        observation: Observation,
        tool_name: str,
    ) -> list[InformationFlow]:
        text = observation.text
        if not text or observation.is_error:
            return []

        response = self.llm.completion(
            messages=[
                Message(
                    role="system",
                    content=[TextContent(text=EXTRACTION_SYSTEM_PROMPT)],
                ),
                Message(
                    role="user",
                    content=[
                        TextContent(text=(f"Tool: {tool_name}\n\nOutput:\n{text}"))
                    ],
                ),
            ],
        )

        # Parse structured response
        raw_text = ""
        for block in response.message.content:
            if isinstance(block, TextContent):
                raw_text += block.text

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "Privacy extraction returned non-JSON for %s: %s",
                tool_name,
                raw_text[:200],
            )
            return []

        # Handle both {"flows": [...]} and bare [...]
        items: list[dict] = raw if isinstance(raw, list) else raw.get("flows", [])
        return [
            InformationFlow(
                data_type=item["data_type"],
                data_subject=item["data_subject"],
            )
            for item in items
            if "data_type" in item and "data_subject" in item
        ]

    def check_write_action(
        self,
        write_content: str,
        accumulated_flows: list[InformationFlow],
    ) -> PrivacyCheckResult:
        if not accumulated_flows or not write_content:
            return PrivacyCheckResult()

        inventory = "\n".join(
            f"- {f.data_type} (subject: {f.data_subject})" for f in accumulated_flows
        )

        response = self.llm.completion(
            messages=[
                Message(
                    role="system",
                    content=[TextContent(text=CHECK_SYSTEM_PROMPT)],
                ),
                Message(
                    role="user",
                    content=[
                        TextContent(
                            text=(
                                f"Information inventory:\n{inventory}"
                                f"\n\nMessage to send:\n{write_content}"
                            )
                        )
                    ],
                ),
            ],
        )

        raw_text = ""
        for block in response.message.content:
            if isinstance(block, TextContent):
                raw_text += block.text

        try:
            raw = json.loads(raw_text)
        except json.JSONDecodeError:
            logger.warning(
                "Privacy check returned non-JSON: %s",
                raw_text[:200],
            )
            return PrivacyCheckResult()

        items: list[dict] = (
            raw
            if isinstance(raw, list)
            else raw.get("transmitted_flows", raw.get("flows", []))
        )
        return PrivacyCheckResult(
            transmitted_flows=[
                TransmittedFlow(
                    data_type=item.get("data_type", ""),
                    data_subject=item.get("data_subject", ""),
                    data_recipient=item.get("data_recipient", ""),
                )
                for item in items
                if "data_type" in item
            ],
        )
