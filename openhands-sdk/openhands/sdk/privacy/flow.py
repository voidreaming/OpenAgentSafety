from __future__ import annotations

from pydantic import BaseModel, Field


class InformationFlow(BaseModel):
    """A single CI tuple extracted from a tool observation.

    Represents one unit of information found in a tool result,
    decomposed into what kind of information and whose data it is.
    """

    data_type: str = Field(
        description=(
            "The specific information "
            "(e.g., 'medical diagnosis', 'project deadline', "
            "'meeting schedule')."
        ),
    )
    data_subject: str = Field(
        description="The person or entity this information is about.",
    )


class TransmittedFlow(BaseModel):
    """An information flow identified as being transmitted by a write action."""

    data_type: str = Field(
        description="The specific information being transmitted.",
    )
    data_subject: str = Field(
        description="Whose data is being transmitted.",
    )
    data_recipient: str = Field(
        description="Who will receive this information.",
    )


class PrivacyCheckResult(BaseModel):
    """Result of checking a write action against accumulated flows."""

    transmitted_flows: list[TransmittedFlow] = Field(
        default_factory=list,
        description=(
            "Information flows from the accumulated inventory "
            "that are being transmitted by this write action."
        ),
    )
