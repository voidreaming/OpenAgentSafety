from __future__ import annotations

from abc import ABC, abstractmethod

from openhands.sdk.privacy.flow import InformationFlow
from openhands.sdk.tool.schema import Observation
from openhands.sdk.utils.models import DiscriminatedUnionMixin


class PrivacyAnalyzerBase(DiscriminatedUnionMixin, ABC):
    """Base class for privacy analyzers that extract information flows.

    Privacy analyzers decompose tool observation content into structured
    information flow units (CI tuples) -- what information and whose
    data.  This runs post-execution on read/discovery tool results.
    """

    @abstractmethod
    def extract_flows(
        self,
        observation: Observation,
        tool_name: str,
    ) -> list[InformationFlow]:
        """Extract information flows from a tool observation.

        Called after each read/discovery tool returns content.
        Returns a list of InformationFlow objects -- one per distinct
        unit of information found in the observation.
        """
        ...
