"""Tests for privacy analyzer integration with the agent tool execution loop.

Verifies that:
1. ``extract_flows()`` fires for read-only tools (``readOnlyHint=True``)
2. ``extract_flows()`` does NOT fire for write tools (no annotations)
3. ``check_write_action()`` fires for write tools when accumulated flows exist
4. ``ObservationEvent.information_flows`` is populated from extraction results
5. Accumulated flows are deduplicated before write-time check
6. Flow-annotated observations include [INFORMATION INVENTORY] in LLM messages
7. Write-deferral emits UserRejectObservation for mixed read+write batches
8. ``_extract_write_content`` extracts outgoing message from action arguments
"""

from collections.abc import Sequence
from typing import TYPE_CHECKING, Self
from unittest.mock import patch

from litellm import ChatCompletionMessageToolCall
from litellm.types.utils import (
    Choices,
    Function,
    Message as LiteLLMMessage,
    ModelResponse,
)
from pydantic import SecretStr

from openhands.sdk.agent import Agent
from openhands.sdk.conversation import Conversation
from openhands.sdk.event import ActionEvent, ObservationEvent
from openhands.sdk.event.llm_convertible.observation import UserRejectObservation
from openhands.sdk.llm import LLM, Message, MessageToolCall, TextContent
from openhands.sdk.mcp.definition import MCPToolAction
from openhands.sdk.privacy.analyzer import PrivacyAnalyzerBase
from openhands.sdk.privacy.flow import InformationFlow, PrivacyCheckResult
from openhands.sdk.tool import Action, Observation, Tool, ToolExecutor, register_tool
from openhands.sdk.tool.tool import ToolAnnotations, ToolDefinition


if TYPE_CHECKING:
    from openhands.sdk.conversation.state import ConversationState


# ── Recording privacy analyzer ──


class RecordingPrivacyAnalyzer(PrivacyAnalyzerBase):
    """Privacy analyzer that records calls instead of calling an LLM."""

    def __init__(self, flows_to_return=None, **kwargs):
        super().__init__(**kwargs)
        self._extract_calls: list[tuple[str, str]] = []
        self._check_calls: list[tuple[str, int]] = []
        self._flows_to_return = flows_to_return or [
            InformationFlow(
                data_type="medical diagnosis",
                data_subject="Alice",
            )
        ]

    def extract_flows(
        self,
        observation: Observation,
        tool_name: str,
    ) -> list[InformationFlow]:
        self._extract_calls.append((tool_name, observation.text or ""))
        return list(self._flows_to_return)

    def check_write_action(
        self,
        write_content: str,
        accumulated_flows: list[InformationFlow],
    ) -> PrivacyCheckResult:
        self._check_calls.append((write_content, len(accumulated_flows)))
        return PrivacyCheckResult()


# ── Simple read-only tool ──


class ReadAction(Action):
    query: str = ""


class ReadObservation(Observation):
    content: list = [  # noqa: RUF012
        TextContent(text="Alice has a medical diagnosis of flu.")
    ]


class ReadExecutor(ToolExecutor[ReadAction, ReadObservation]):
    def __call__(self, action: ReadAction, conversation=None) -> ReadObservation:
        return ReadObservation()


class ReadTool(ToolDefinition[ReadAction, ReadObservation]):
    name = "read_tool"

    @classmethod
    def create(cls, conv_state: "ConversationState | None" = None) -> Sequence[Self]:
        return [
            cls(
                description="A read-only tool",
                action_type=ReadAction,
                observation_type=ReadObservation,
                executor=ReadExecutor(),
                annotations=ToolAnnotations(readOnlyHint=True),
            )
        ]


register_tool("ReadTool", ReadTool)


# ── Simple write tool ──


class WriteAction(Action):
    message: str = ""


class WriteObservation(Observation):
    @property
    def text(self) -> str:
        return "Message sent successfully."


class WriteExecutor(ToolExecutor[WriteAction, WriteObservation]):
    def __call__(self, action: WriteAction, conversation=None) -> WriteObservation:
        return WriteObservation()


class WriteTool(ToolDefinition[WriteAction, WriteObservation]):
    name = "write_tool"

    @classmethod
    def create(cls, conv_state: "ConversationState | None" = None) -> Sequence[Self]:
        return [
            cls(
                description="A write tool",
                action_type=WriteAction,
                observation_type=WriteObservation,
                executor=WriteExecutor(),
            )
        ]


register_tool("WriteTool", WriteTool)


# ── Helpers ──


def _make_tool_call_response(tool_name: str, arguments: str) -> ModelResponse:
    return ModelResponse(
        id="mock-response",
        choices=[
            Choices(
                index=0,
                message=LiteLLMMessage(
                    role="assistant",
                    content=f"Using {tool_name}.",
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id="call_1",
                            type="function",
                            function=Function(
                                name=tool_name,
                                arguments=arguments,
                            ),
                        )
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        created=0,
        model="test-model",
        object="chat.completion",
    )


# ── Tests ──


def test_privacy_analyzer_extracts_flows_for_readonly_tool():
    """extract_flows() fires for tools with readOnlyHint=True."""
    analyzer = RecordingPrivacyAnalyzer()

    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    agent = Agent(
        llm=llm,
        tools=[Tool(name="ReadTool")],
        privacy_analyzer=analyzer,
    )

    collected_events = []

    def on_event(event):
        collected_events.append(event)

    conversation = Conversation(agent=agent, callbacks=[on_event])

    with patch(
        "openhands.sdk.llm.llm.litellm_completion",
        side_effect=lambda *a, **kw: _make_tool_call_response(
            "read_tool", '{"query": "test"}'
        ),
    ):
        conversation.send_message(
            Message(
                role="user",
                content=[TextContent(text="Search for pages.")],
            )
        )
        agent.step(conversation, on_event=on_event)

    # extract_flows() should have been called
    assert len(analyzer._extract_calls) == 1
    assert analyzer._extract_calls[0][0] == "read_tool"

    # check_write_action() should NOT have been called
    assert len(analyzer._check_calls) == 0

    # ObservationEvent should have information_flows populated
    obs_events = [e for e in collected_events if isinstance(e, ObservationEvent)]
    assert len(obs_events) == 1
    assert obs_events[0].information_flows is not None
    assert len(obs_events[0].information_flows) == 1
    assert obs_events[0].information_flows[0].data_type == "medical diagnosis"
    assert obs_events[0].information_flows[0].data_subject == "Alice"


def test_privacy_analyzer_skips_extraction_for_write_tool():
    """extract_flows() does NOT fire for tools without readOnlyHint."""
    analyzer = RecordingPrivacyAnalyzer()

    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    agent = Agent(
        llm=llm,
        tools=[Tool(name="WriteTool")],
        privacy_analyzer=analyzer,
    )

    collected_events = []

    def on_event(event):
        collected_events.append(event)

    conversation = Conversation(agent=agent, callbacks=[on_event])

    with patch(
        "openhands.sdk.llm.llm.litellm_completion",
        side_effect=lambda *a, **kw: _make_tool_call_response(
            "write_tool", '{"message": "hello"}'
        ),
    ):
        conversation.send_message(
            Message(
                role="user",
                content=[TextContent(text="Send a message.")],
            )
        )
        agent.step(conversation, on_event=on_event)

    # extract_flows() should NOT have been called
    assert len(analyzer._extract_calls) == 0

    # ObservationEvent should have information_flows = None
    obs_events = [e for e in collected_events if isinstance(e, ObservationEvent)]
    assert len(obs_events) == 1
    assert obs_events[0].information_flows is None


def test_no_privacy_analyzer_skips_all_privacy_logic():
    """Without a privacy_analyzer, no extraction or checking happens."""
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    agent = Agent(
        llm=llm,
        tools=[Tool(name="ReadTool")],
        # No privacy_analyzer
    )

    collected_events = []

    def on_event(event):
        collected_events.append(event)

    conversation = Conversation(agent=agent, callbacks=[on_event])

    with patch(
        "openhands.sdk.llm.llm.litellm_completion",
        side_effect=lambda *a, **kw: _make_tool_call_response(
            "read_tool", '{"query": "test"}'
        ),
    ):
        conversation.send_message(
            Message(
                role="user",
                content=[TextContent(text="Search for pages.")],
            )
        )
        agent.step(conversation, on_event=on_event)

    # ObservationEvent should have information_flows = None
    obs_events = [e for e in collected_events if isinstance(e, ObservationEvent)]
    assert len(obs_events) == 1
    assert obs_events[0].information_flows is None


def test_accumulated_flows_are_deduplicated():
    """Duplicate flows from overlapping searches are merged before write check.

    Directly tests ``_gather_accumulated_flows`` by injecting
    ObservationEvents with overlapping information_flows into the
    conversation state.
    """
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    analyzer = RecordingPrivacyAnalyzer()
    agent = Agent(
        llm=llm,
        tools=[Tool(name="ReadTool")],
        privacy_analyzer=analyzer,
    )

    conversation = Conversation(agent=agent)

    # Simulate two read observations with overlapping flows
    obs1 = ObservationEvent(
        observation=ReadObservation(),
        action_id="act-1",
        tool_name="read_tool",
        tool_call_id="call-1",
        information_flows=[
            InformationFlow(data_type="medical diagnosis", data_subject="Alice"),
            InformationFlow(data_type="salary", data_subject="Bob"),
        ],
    )
    obs2 = ObservationEvent(
        observation=ReadObservation(),
        action_id="act-2",
        tool_name="read_tool",
        tool_call_id="call-2",
        information_flows=[
            # Duplicate of obs1
            InformationFlow(data_type="medical diagnosis", data_subject="Alice"),
            # New unique flow
            InformationFlow(data_type="home address", data_subject="Carol"),
        ],
    )

    # Inject events into conversation state
    conversation._state._events.append(obs1)
    conversation._state._events.append(obs2)

    accumulated = agent._gather_accumulated_flows(conversation)

    # Should have 3 unique flows, not 4 raw
    assert len(accumulated) == 3
    types = {(f.data_type, f.data_subject) for f in accumulated}
    assert types == {
        ("medical diagnosis", "Alice"),
        ("salary", "Bob"),
        ("home address", "Carol"),
    }


# ── Tests for flow-annotated observations ──


def test_flow_annotated_observation_message():
    """to_llm_message() includes INFORMATION INVENTORY annotation."""
    obs_event = ObservationEvent(
        observation=ReadObservation(),
        action_id="act-1",
        tool_name="read_tool",
        tool_call_id="call-1",
        information_flows=[
            InformationFlow(data_type="medical diagnosis", data_subject="Alice"),
            InformationFlow(data_type="salary", data_subject="Bob"),
        ],
    )
    msg = obs_event.to_llm_message()

    # Should have 2 content blocks: observation + annotation
    assert len(msg.content) == 2
    annotation = msg.content[1]
    assert isinstance(annotation, TextContent)
    assert "[INFORMATION INVENTORY]" in annotation.text
    assert "medical diagnosis (subject: Alice)" in annotation.text
    assert "salary (subject: Bob)" in annotation.text


def test_no_annotation_when_no_flows():
    """No annotation when flows are empty or None."""
    obs_none = ObservationEvent(
        observation=ReadObservation(),
        action_id="act-1",
        tool_name="read_tool",
        tool_call_id="call-1",
        information_flows=None,
    )
    msg_none = obs_none.to_llm_message()
    assert len(msg_none.content) == 1  # just the observation

    obs_empty = ObservationEvent(
        observation=ReadObservation(),
        action_id="act-2",
        tool_name="read_tool",
        tool_call_id="call-2",
        information_flows=[],
    )
    msg_empty = obs_empty.to_llm_message()
    assert len(msg_empty.content) == 1  # just the observation


# ── Helpers for ActionEvent construction ──


def _make_action_event(tool_name: str, action: Action) -> ActionEvent:
    """Build an ActionEvent with required fields for testing."""
    return ActionEvent(
        thought=[],
        action=action,
        tool_name=tool_name,
        tool_call_id="call-1",
        tool_call=MessageToolCall(
            id="call-1",
            name=tool_name,
            arguments="{}",
            origin="completion",
        ),
        llm_response_id="resp-1",
    )


# ── Tests for _extract_write_content ──


def test_extract_write_content_message_field():
    """_extract_write_content extracts the 'message' field from MCP action data."""
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    analyzer = RecordingPrivacyAnalyzer()
    agent = Agent(llm=llm, tools=[], privacy_analyzer=analyzer)

    action = MCPToolAction(data={"recipient": "alice", "message": "Hello Alice"})
    ae = _make_action_event("send_message", action)
    result = agent._extract_write_content(ae)
    assert result == "Hello Alice"


def test_extract_write_content_body_field():
    """_extract_write_content extracts the 'body' field for email tools."""
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    analyzer = RecordingPrivacyAnalyzer()
    agent = Agent(llm=llm, tools=[], privacy_analyzer=analyzer)

    action = MCPToolAction(
        data={"to": "bob@example.com", "subject": "Hi", "body": "Dear Bob..."}
    )
    ae = _make_action_event("send_email", action)
    result = agent._extract_write_content(ae)
    assert result == "Dear Bob..."


def test_extract_write_content_fallback_serializes_all():
    """Falls back to serializing all data when no known field."""
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    analyzer = RecordingPrivacyAnalyzer()
    agent = Agent(llm=llm, tools=[], privacy_analyzer=analyzer)

    action = MCPToolAction(data={"channel": "general", "text_payload": "hello"})
    ae = _make_action_event("custom_tool", action)
    result = agent._extract_write_content(ae)
    assert "text_payload" in result
    assert "hello" in result


# ── Tests for _format_ci_inventory ──


def test_format_ci_inventory_with_flows():
    """_format_ci_inventory produces structured inventory with A/B/C guidance."""
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    analyzer = RecordingPrivacyAnalyzer()
    agent = Agent(llm=llm, tools=[], privacy_analyzer=analyzer)

    flows = [
        InformationFlow(data_type="medical diagnosis", data_subject="Alice"),
        InformationFlow(data_type="salary", data_subject="Bob"),
    ]
    result = agent._format_ci_inventory(flows)
    assert "[ACCUMULATED INFORMATION INVENTORY]" in result
    assert "medical diagnosis (subject: Alice)" in result
    assert "salary (subject: Bob)" in result
    assert "(A) include" in result
    assert "(B) generalize" in result
    assert "(C) omit" in result


def test_format_ci_inventory_empty():
    """_format_ci_inventory handles empty flows gracefully."""
    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    analyzer = RecordingPrivacyAnalyzer()
    agent = Agent(llm=llm, tools=[], privacy_analyzer=analyzer)

    result = agent._format_ci_inventory([])
    assert "Re-issue the write action" in result


# ── Tests for write-deferral ──


def test_write_deferral_in_mixed_batch():
    """Mixed read+write batches defer writes with CI inventory."""
    analyzer = RecordingPrivacyAnalyzer()

    llm = LLM(
        usage_id="test-llm",
        model="test-model",
        api_key=SecretStr("test-key"),
        base_url="http://test",
    )
    agent = Agent(
        llm=llm,
        tools=[Tool(name="ReadTool"), Tool(name="WriteTool")],
        privacy_analyzer=analyzer,
    )

    collected_events = []

    def on_event(event):
        collected_events.append(event)

    # Use conversation's internal _on_event so events persist to state
    # (required for _gather_accumulated_flows to find read flows)
    conversation = Conversation(agent=agent, callbacks=[on_event])

    # Build a response with both a read and a write tool call
    response = ModelResponse(
        id="mock-response",
        choices=[
            Choices(
                index=0,
                message=LiteLLMMessage(
                    role="assistant",
                    content="Reading and writing.",
                    tool_calls=[
                        ChatCompletionMessageToolCall(
                            id="call_read",
                            type="function",
                            function=Function(
                                name="read_tool",
                                arguments='{"query": "test"}',
                            ),
                        ),
                        ChatCompletionMessageToolCall(
                            id="call_write",
                            type="function",
                            function=Function(
                                name="write_tool",
                                arguments='{"message": "hello"}',
                            ),
                        ),
                    ],
                ),
                finish_reason="tool_calls",
            )
        ],
        created=0,
        model="test-model",
        object="chat.completion",
    )

    with patch(
        "openhands.sdk.llm.llm.litellm_completion",
        side_effect=lambda *a, **kw: response,
    ):
        conversation.send_message(
            Message(
                role="user",
                content=[TextContent(text="Read and send.")],
            )
        )
        # Use conversation._on_event so events get persisted to state
        agent.step(conversation, on_event=conversation._on_event)

    # Read should have executed (extraction called)
    assert len(analyzer._extract_calls) == 1

    # Write should have been DEFERRED (check_write_action NOT called)
    assert len(analyzer._check_calls) == 0

    # Should have a UserRejectObservation for the write
    reject_events = [
        e for e in collected_events if isinstance(e, UserRejectObservation)
    ]
    assert len(reject_events) == 1
    assert reject_events[0].tool_name == "write_tool"
    assert "[ACCUMULATED INFORMATION INVENTORY]" in reject_events[0].rejection_reason
    assert "medical diagnosis (subject: Alice)" in reject_events[0].rejection_reason
