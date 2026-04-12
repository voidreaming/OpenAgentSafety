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
