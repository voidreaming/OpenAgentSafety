  ---
  OpenHands Memory Architecture (What OAS Inherits)

  OpenHands has a 3-layer memory system:

  ┌──────────────────────────────────────────────────────────────┐
  │  Layer 1: Event Stream (Persistent Storage)                   │
  │                                                              │
  │  Every action + observation → serialized to disk              │
  │  Monotonic event IDs, supports resume across sessions         │
  │  Location: one file per event in storage backend              │
  │  This is the "ground truth" — never lost                      │
  ├──────────────────────────────────────────────────────────────┤
  │  Layer 2: Condenser (Context Window Management)               │
  │                                                              │
  │  Pluggable strategies to compress history for LLM context:    │
  │  ┌─────────────────────────────────────────────────────┐     │
  │  │ NoOp              → Keep everything (dev/testing)   │     │
  │  │ BrowserOutput     → Mask old screenshots ← OAS uses │     │
  │  │ ObservationMask   → Mask all old observations       │     │
  │  │ RecentEvents      → Keep only N recent events       │     │
  │  │ AmortizedForgetting → Drop old half when full       │     │
  │  │ LLMSummarizing   → LLM writes rolling summary      │     │
  │  │ LLMAttention     → LLM picks important events       │     │
  │  └─────────────────────────────────────────────────────┘     │
  ├──────────────────────────────────────────────────────────────┤
  │  Layer 3: ConversationMemory (Message Formatting)             │
  │                                                              │
  │  Converts condensed events → LLM messages                     │
  │  Handles: tool calls, observations, role alternation          │
  │  Injects: microagent knowledge (RecallObservation)            │
  │  Applies: prompt caching, truncation, image handling          │
  └──────────────────────────────────────────────────────────────┘

  How the CodeAct Agent Builds Its Context Each Turn

  # In codeact_agent.py — this runs every turn:

  1. messages = process_initial_messages()     # System prompt + first user msg
  2. events  = condenser.condensed_history(state)  # Compress history
  3. messages = process_events(events, messages)   # Convert to LLM messages
  4. messages = _enhance_messages(messages)     # Add examples/formatting
  5. → Send to LLM

  What OAS Currently Uses

  OAS uses the minimal config:

  agent_config = AgentConfig(
      enable_prompt_extensions=False,   # No microagents/knowledge injection
      enable_chat_tool=True,            # Agent can chat
      condenser=BrowserOutputCondenserConfig()  # Only masks old browser screenshots
  )

  This means:
  - No summarization of long conversations
  - No microagent knowledge injection
  - No smart event selection — just browser output masking
  - When context overflows → hard truncation (cut history in half, keep first message)

  ---
  What You Can Directly Reuse

  1. LLMSummarizingCondenser — Rolling Memory Summary

  This is the most useful for your platform. It already exists in OpenHands:

  Before:  [system] [user] [act1] [obs1] [act2] [obs2] ... [act50] [obs50]
  After:   [system] [user] [SUMMARY of act1-act40] [act41] [obs41] ... [act50] [obs50]

  The summary is regenerated each time history grows too long. You just need to switch the config:

  from openhands.core.config.condenser_config import LLMSummarizingCondenserConfig

  agent_config = AgentConfig(
      condenser=LLMSummarizingCondenserConfig(
          llm_config=env_llm_config,  # Use same LLM as environment
          max_size=100,               # Trigger summarization at 100 events
          keep_first=1,               # Always keep first message
      )
  )

  Research value: The summary becomes the agent's "working memory" — you can study what information the agent retains vs forgets,
  which directly relates to privacy norm retention.

  2. Microagent Knowledge Injection — Pre-loaded Knowledge

  OpenHands has a RecallObservation mechanism that injects knowledge into the agent's context based on triggers. OAS has it disabled
  (enable_prompt_extensions=False).

  If you enable it, you can:
  - Pre-load privacy policies, organizational norms, or role-specific rules
  - Trigger knowledge injection when the agent encounters specific keywords
  - This is essentially a read-only memory that the agent gets for free

  agent_config = AgentConfig(
      enable_prompt_extensions=True,   # Enable microagents
      # Place .md files in .openhands/microagents/ to define knowledge
  )

  3. Event Stream — Full Audit Trail

  The EventStream already persists every action and observation to disk. You can:
  - Replay any agent run event-by-event
  - Analyze what information the agent accessed, when, and how it used it
  - Build post-hoc analysis tools without changing the agent

  4. Context Truncation as a Research Variable

  The hard truncation (_handle_long_context_error) is a potential research lever. When history is cut in half, the agent may lose
  track of previously learned norms. You can:
  - Use different condensers as experimental conditions
  - Compare: NoOp (full context) vs RecentEvents vs LLMSummarizing
  - Measure: does norm compliance degrade after truncation?

  ---
  What's Missing and Needs Building

  Gap 1: No Cross-Run Memory

  All OpenHands memory is within a single agent session. When the run ends, the EventStream files exist on disk but are never loaded
  into a subsequent run.

  To build: Create a memory_store tool in your oas_tool_runtime.py:
  memory.store(key, value)   → persist to JSON/CIHub
  memory.retrieve(query)     → look up from store
  memory.list()              → show all stored memories
  The agent calls these explicitly. Seed prior-run summaries into the store for cross-run experiments.

  Gap 2: No Shared Memory Between Agent and NPCs

  The agent's EventStream and the NPC's Sotopia inbox are completely separate. Neither can see the other's internal state.

  To build: Use your CIHub backend as a shared state bus. When the agent performs a tool action, write a summary to a shared log. NPCs
   can query this log to become aware of what the agent did (not just what it said in chat).

  Gap 3: FakeUser Has No Access to Agent State

  The FakeUser only sees agent's text messages — not what tools the agent called. For multi-turn research, the FakeUser needs to react
   to agent actions.

  To build: In codeact_user_response(), pass the full state.history (not just the last MessageAction) to the FakeUser. Let it see
  recent tool calls to generate context-aware follow-ups.

  ---
  Recommended Approach

  For building a workable platform now, I'd prioritize:

  ┌──────────┬───────────────────────────────┬────────────────────────────────────────────────────────────────┬───────────────────┐
  │ Priority │             What              │                              How                               │      Effort       │
  ├──────────┼───────────────────────────────┼────────────────────────────────────────────────────────────────┼───────────────────┤
  │ 1        │ Switch condenser              │ Change BrowserOutputCondenserConfig →                          │ 5 min, config     │
  │          │                               │ LLMSummarizingCondenserConfig in run_eval.py                   │ change only       │
  ├──────────┼───────────────────────────────┼────────────────────────────────────────────────────────────────┼───────────────────┤
  │ 2        │ Raise FakeUser turn limit     │ Change if self.turns > 3 to a higher number or make it         │ 10 min            │
  │          │                               │ configurable from scenario.json                                │                   │
  ├──────────┼───────────────────────────────┼────────────────────────────────────────────────────────────────┼───────────────────┤
  │ 3        │ Give FakeUser visibility into │ Pass tool call summaries from state.history into FakeUser's    │ 1-2 hrs           │
  │          │  agent actions                │ context                                                        │                   │
  ├──────────┼───────────────────────────────┼────────────────────────────────────────────────────────────────┼───────────────────┤
  │ 4        │ Add memory tools to your tool │ New memory.* tool category in oas_tool_runtime.py              │ Half day          │
  │          │  runtime                      │                                                                │                   │
  ├──────────┼───────────────────────────────┼────────────────────────────────────────────────────────────────┼───────────────────┤
  │ 5        │ Enable microagent knowledge   │ Set enable_prompt_extensions=True, write knowledge .md files   │ 1-2 hrs           │
  │          │ injection                     │                                                                │                   │
  └──────────┴───────────────────────────────┴────────────────────────────────────────────────────────────────┴───────────────────┘

  The key insight: OpenHands already solved the hard parts (event persistence, pluggable condensers, context management). You don't
  need to build a memory system from scratch — you need to configure what exists and add a thin layer for cross-run and cross-agent
  state sharing on top.