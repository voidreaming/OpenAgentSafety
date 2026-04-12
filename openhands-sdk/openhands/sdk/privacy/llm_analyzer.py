from __future__ import annotations

import json

from openhands.sdk.llm import LLM
from openhands.sdk.llm.message import Message, TextContent
from openhands.sdk.logger import get_logger
from openhands.sdk.privacy.analyzer import PrivacyAnalyzerBase
from openhands.sdk.privacy.flow import InformationFlow
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
