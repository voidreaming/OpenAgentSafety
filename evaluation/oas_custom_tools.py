from __future__ import annotations

import json
import shlex
from dataclasses import dataclass
import inspect
from typing import Any, Callable

from litellm import ChatCompletionToolParam, ChatCompletionToolParamFunctionChunk, ModelResponse

from openhands.agenthub.codeact_agent.tools import (
    BrowserTool,
    ChatNPCTool,
    FinishTool,
    IPythonTool,
    LLMBasedFileEditTool,
    ThinkTool,
    WebReadTool,
    create_cmd_run_tool,
    create_str_replace_editor_tool,
)
from openhands.core.exceptions import (
    FunctionCallNotExistsError,
    FunctionCallValidationError,
)
from openhands.events.action import (
    Action,
    AgentDelegateAction,
    AgentFinishAction,
    AgentThinkAction,
    BrowseInteractiveAction,
    BrowseURLAction,
    ChatAction,
    CmdRunAction,
    FileEditAction,
    FileReadAction,
    IPythonRunCellAction,
    MessageAction,
)
from openhands.events.event import FileEditSource, FileReadSource
from openhands.events.tool import ToolCallMetadata


@dataclass
class _OASToolConfig:
    runtime_script_path_in_sandbox: str = "/utils/oas_tool_runtime.py"
    state_path_in_sandbox: str = "/workspace/.oas_tool_state.json"
    seed_state_path_in_sandbox: str = "/instruction/tools_state.json"
    backend: str = "json"
    fs_root_in_sandbox: str = "/workspace/.oas_tool_fs"


_CONFIG = _OASToolConfig()
_PATCHED = False
_ORIGINAL_GET_TOOLS: Callable[..., list[ChatCompletionToolParam]] | None = None


_CUSTOM_TOOL_TO_CANONICAL: dict[str, str] = {
    "email_search_threads": "email.search_threads",
    "email_read_thread": "email.read_thread",
    "email_send_email": "email.send_email",
    "calendar_search_events": "calendar.search_events",
    "calendar_read_event": "calendar.read_event",
    "docs_search": "docs.search",
    "docs_read": "docs.read",
    "docs_write": "docs.write",
    "notion_search": "docs.search",
    "notion_read": "docs.read",
    "notion_write": "docs.write",
    "files_list": "files.list",
    "files_read": "files.read",
    "files_write": "files.write",
    "drive_list": "files.list",
    "drive_read": "files.read",
    "drive_write": "files.write",
    "contacts_lookup": "contacts.lookup",
    "social_media_list_threads": "social_media.list_threads",
    "social_media_read_thread": "social_media.read_thread",
    "social_media_post": "social_media.post",
    "social_media_send_message": "social_media.send_message",
}


def _tool(
    *,
    name: str,
    description: str,
    properties: dict[str, Any],
    required: list[str],
) -> ChatCompletionToolParam:
    return ChatCompletionToolParam(
        type="function",
        function=ChatCompletionToolParamFunctionChunk(
            name=name,
            description=description,
            parameters={
                "type": "object",
                "properties": properties,
                "required": required,
            },
        ),
    )


def _oas_custom_tools() -> list[ChatCompletionToolParam]:
    return [
        _tool(
            name="email_search_threads",
            description="Search email threads by text query.",
            properties={"query": {"type": "string"}},
            required=["query"],
        ),
        _tool(
            name="email_read_thread",
            description="Read one email thread by thread_id.",
            properties={"thread_id": {"type": "string"}},
            required=["thread_id"],
        ),
        _tool(
            name="email_send_email",
            description="Send an email message.",
            properties={
                "to": {"type": "array", "items": {"type": "string"}},
                "cc": {"type": "array", "items": {"type": "string"}},
                "bcc": {"type": "array", "items": {"type": "string"}},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "attachments": {"type": "array", "items": {"type": "string"}},
            },
            required=["to", "subject", "body"],
        ),
        _tool(
            name="calendar_search_events",
            description="Search calendar events by query and optional time range.",
            properties={
                "start": {"type": "string"},
                "end": {"type": "string"},
                "query": {"type": "string"},
            },
            required=["query"],
        ),
        _tool(
            name="calendar_read_event",
            description="Read one calendar event by event_id.",
            properties={"event_id": {"type": "string"}},
            required=["event_id"],
        ),
        _tool(
            name="docs_search",
            description="Search docs or notes by query.",
            properties={"query": {"type": "string"}},
            required=["query"],
        ),
        _tool(
            name="docs_read",
            description="Read one document by doc_id.",
            properties={"doc_id": {"type": "string"}},
            required=["doc_id"],
        ),
        _tool(
            name="docs_write",
            description="Write full content to a document.",
            properties={"doc_id": {"type": "string"}, "content": {"type": "string"}},
            required=["doc_id", "content"],
        ),
        _tool(
            name="notion_search",
            description="Alias of docs_search for Notion-style docs.",
            properties={"query": {"type": "string"}},
            required=["query"],
        ),
        _tool(
            name="notion_read",
            description="Alias of docs_read for Notion-style docs.",
            properties={"doc_id": {"type": "string"}},
            required=["doc_id"],
        ),
        _tool(
            name="notion_write",
            description="Alias of docs_write for Notion-style docs.",
            properties={"doc_id": {"type": "string"}, "content": {"type": "string"}},
            required=["doc_id", "content"],
        ),
        _tool(
            name="files_list",
            description="List files by path.",
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
        _tool(
            name="files_read",
            description="Read one file by path.",
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
        _tool(
            name="files_write",
            description="Write one file by path.",
            properties={"path": {"type": "string"}, "content": {"type": "string"}},
            required=["path", "content"],
        ),
        _tool(
            name="drive_list",
            description="Alias of files_list for Drive-style storage.",
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
        _tool(
            name="drive_read",
            description="Alias of files_read for Drive-style storage.",
            properties={"path": {"type": "string"}},
            required=["path"],
        ),
        _tool(
            name="drive_write",
            description="Alias of files_write for Drive-style storage.",
            properties={"path": {"type": "string"}, "content": {"type": "string"}},
            required=["path", "content"],
        ),
        _tool(
            name="contacts_lookup",
            description="Lookup contact by name or email.",
            properties={"name_or_email": {"type": "string"}},
            required=["name_or_email"],
        ),
        _tool(
            name="social_media_list_threads",
            description="List social media threads.",
            properties={"query": {"type": "string"}},
            required=[],
        ),
        _tool(
            name="social_media_read_thread",
            description="Read one social media thread by thread_id.",
            properties={"thread_id": {"type": "string"}},
            required=["thread_id"],
        ),
        _tool(
            name="social_media_post",
            description="Create a social media post.",
            properties={
                "content": {"type": "string"},
                "visibility": {"type": "string"},
            },
            required=["content"],
        ),
        _tool(
            name="social_media_send_message",
            description="Send a direct message via social media.",
            properties={"to": {"type": "string"}, "body": {"type": "string"}},
            required=["to", "body"],
        ),
    ]


def _combine_thought(action: Action, thought: str) -> Action:
    if not hasattr(action, "thought"):
        return action
    if thought and getattr(action, "thought", ""):
        action.thought = f"{thought}\n{action.thought}"
    elif thought:
        action.thought = thought
    return action


def _build_runtime_command(tool_function_name: str, arguments: dict[str, Any]) -> str:
    canonical_tool = _CUSTOM_TOOL_TO_CANONICAL[tool_function_name]
    args_json = json.dumps(arguments, ensure_ascii=False)
    return (
        f"python {_CONFIG.runtime_script_path_in_sandbox} call "
        f"--tool {shlex.quote(canonical_tool)} "
        f"--args-json {shlex.quote(args_json)} "
        f"--state {shlex.quote(_CONFIG.state_path_in_sandbox)} "
        f"--seed-state {shlex.quote(_CONFIG.seed_state_path_in_sandbox)} "
        f"--backend {shlex.quote(_CONFIG.backend)} "
        f"--fs-root {shlex.quote(_CONFIG.fs_root_in_sandbox)}"
    )


def _response_to_actions_with_custom_tools(response: ModelResponse) -> list[Action]:
    actions: list[Action] = []
    assert len(response.choices) == 1, "Only one choice is supported for now"
    choice = response.choices[0]
    assistant_msg = choice.message
    if hasattr(assistant_msg, "tool_calls") and assistant_msg.tool_calls:
        thought = ""
        if isinstance(assistant_msg.content, str):
            thought = assistant_msg.content
        elif isinstance(assistant_msg.content, list):
            for msg in assistant_msg.content:
                if msg["type"] == "text":
                    thought += msg["text"]

        for i, tool_call in enumerate(assistant_msg.tool_calls):
            action: Action
            try:
                arguments = json.loads(tool_call.function.arguments)
            except json.decoder.JSONDecodeError as e:
                raise RuntimeError(
                    f"Failed to parse tool call arguments: {tool_call.function.arguments}"
                ) from e

            if tool_call.function.name in _CUSTOM_TOOL_TO_CANONICAL:
                action = CmdRunAction(
                    command=_build_runtime_command(tool_call.function.name, arguments),
                    is_input=False,
                )
            elif tool_call.function.name == create_cmd_run_tool()["function"]["name"]:
                if "command" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "command" in tool call {tool_call.function.name}'
                    )
                is_input = arguments.get("is_input", "false") == "true"
                action = CmdRunAction(command=arguments["command"], is_input=is_input)
            elif tool_call.function.name == IPythonTool["function"]["name"]:
                if "code" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "code" in tool call {tool_call.function.name}'
                    )
                action = IPythonRunCellAction(code=arguments["code"])
            elif tool_call.function.name == "delegate_to_browsing_agent":
                action = AgentDelegateAction(agent="BrowsingAgent", inputs=arguments)
            elif tool_call.function.name == FinishTool["function"]["name"]:
                action = AgentFinishAction(
                    final_thought=arguments.get("message", ""),
                    task_completed=arguments.get("task_completed", None),
                )
            elif tool_call.function.name == LLMBasedFileEditTool["function"]["name"]:
                if "path" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "path" in tool call {tool_call.function.name}'
                    )
                if "content" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "content" in tool call {tool_call.function.name}'
                    )
                action = FileEditAction(
                    path=arguments["path"],
                    content=arguments["content"],
                    start=arguments.get("start", 1),
                    end=arguments.get("end", -1),
                )
            elif (
                tool_call.function.name
                == create_str_replace_editor_tool()["function"]["name"]
            ):
                if "command" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "command" in tool call {tool_call.function.name}'
                    )
                if "path" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "path" in tool call {tool_call.function.name}'
                    )
                path = arguments["path"]
                command = arguments["command"]
                other_kwargs = {
                    k: v for k, v in arguments.items() if k not in ["command", "path"]
                }
                if command == "view":
                    action = FileReadAction(
                        path=path,
                        impl_source=FileReadSource.OH_ACI,
                        view_range=other_kwargs.get("view_range", None),
                    )
                else:
                    if "view_range" in other_kwargs:
                        other_kwargs.pop("view_range")
                    action = FileEditAction(
                        path=path,
                        command=command,
                        impl_source=FileEditSource.OH_ACI,
                        **other_kwargs,
                    )
            elif tool_call.function.name == ThinkTool["function"]["name"]:
                action = AgentThinkAction(thought=arguments.get("thought", ""))
            elif tool_call.function.name == BrowserTool["function"]["name"]:
                if "code" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "code" in tool call {tool_call.function.name}'
                    )
                action = BrowseInteractiveAction(browser_actions=arguments["code"])
            elif tool_call.function.name == WebReadTool["function"]["name"]:
                if "url" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "url" in tool call {tool_call.function.name}'
                    )
                action = BrowseURLAction(url=arguments["url"])
            elif tool_call.function.name == ChatNPCTool["function"]["name"]:
                if "name" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "name" in tool call {tool_call.function.name}'
                    )
                if "message" not in arguments:
                    raise FunctionCallValidationError(
                        f'Missing required argument "message" in tool call {tool_call.function.name}'
                    )
                action = ChatAction(
                    content=arguments["message"], npc_name=arguments["name"]
                )
            else:
                raise FunctionCallNotExistsError(
                    f"Tool {tool_call.function.name} is not registered. "
                    f"(arguments: {arguments}). Please check the tool name and retry "
                    "with an existing tool."
                )

            if i == 0:
                action = _combine_thought(action, thought)
            action.tool_call_metadata = ToolCallMetadata(
                tool_call_id=tool_call.id,
                function_name=tool_call.function.name,
                model_response=response,
                total_calls_in_response=len(assistant_msg.tool_calls),
            )
            actions.append(action)
    else:
        actions.append(
            MessageAction(
                content=str(assistant_msg.content) if assistant_msg.content else "",
                wait_for_response=True,
            )
        )
    assert len(actions) >= 1
    return actions


def _get_tools_with_custom_tools(*args: Any, **kwargs: Any) -> list[ChatCompletionToolParam]:
    assert _ORIGINAL_GET_TOOLS is not None
    signature = inspect.signature(_ORIGINAL_GET_TOOLS)
    has_var_kwargs = any(
        p.kind == inspect.Parameter.VAR_KEYWORD
        for p in signature.parameters.values()
    )
    if has_var_kwargs:
        filtered_kwargs = kwargs
    else:
        allowed_keys = set(signature.parameters.keys())
        filtered_kwargs = {k: v for k, v in kwargs.items() if k in allowed_keys}

    max_positional = sum(
        1
        for p in signature.parameters.values()
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    )
    filtered_args = args[:max_positional]

    base_tools = _ORIGINAL_GET_TOOLS(*filtered_args, **filtered_kwargs)
    existing_names = {t["function"]["name"] for t in base_tools}
    extras = [t for t in _oas_custom_tools() if t["function"]["name"] not in existing_names]
    return base_tools + extras


def enable_oas_custom_tools(
    *,
    runtime_script_path_in_sandbox: str = "/utils/oas_tool_runtime.py",
    state_path_in_sandbox: str = "/workspace/.oas_tool_state.json",
    seed_state_path_in_sandbox: str = "/instruction/tools_state.json",
    backend: str = "json",
    fs_root_in_sandbox: str = "/workspace/.oas_tool_fs",
) -> None:
    global _PATCHED, _ORIGINAL_GET_TOOLS
    _CONFIG.runtime_script_path_in_sandbox = runtime_script_path_in_sandbox
    _CONFIG.state_path_in_sandbox = state_path_in_sandbox
    _CONFIG.seed_state_path_in_sandbox = seed_state_path_in_sandbox
    _CONFIG.backend = backend
    _CONFIG.fs_root_in_sandbox = fs_root_in_sandbox
    if _PATCHED:
        return

    from openhands.agenthub.codeact_agent import function_calling as fc

    _ORIGINAL_GET_TOOLS = fc.get_tools
    fc.get_tools = _get_tools_with_custom_tools  # type: ignore[assignment]
    fc.response_to_actions = _response_to_actions_with_custom_tools  # type: ignore[assignment]
    _PATCHED = True
