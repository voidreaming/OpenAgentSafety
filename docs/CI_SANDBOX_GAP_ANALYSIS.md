# CI Privacy Sandbox: Reuse vs Redesign Analysis

This document maps the current OpenAgentSafety (OAS) pipeline to the proposed
"CI Privacy Sandbox" for action-level Contextual Integrity (CI) research.

## 1) Current OAS Pipeline (What Exists Today)

### 1.1 Batch Runner
- Entry: `evaluation/run_eval.sh`
- Responsibilities:
  - Iterate safety tasks under `workspaces/tasks/*`.
  - Resolve LLM config groups from `evaluation/config.toml`.
  - Call `evaluation/run_eval.py` per task.

### 1.2 Per-Task Runtime Orchestration
- Entry: `evaluation/run_eval.py`
- Responsibilities:
  - Parse agent/environment LLM config groups.
  - Create OpenHands runtime and sandbox config.
  - Copy task assets into runtime (`/instruction`, `/utils`, optional `/workspace`).
  - Run task init (`/utils/init.sh`), reset services, pre-login browser sessions.
  - Execute agent controller loop.
  - Save trajectory/state artifacts.
  - Run evaluator (`/utils/eval.py`) and persist result.

### 1.3 Task Packaging Model
- Root: `workspaces/tasks/<task_id>/`
- Typical files:
  - `task.md` (agent-facing instruction)
  - `utils/evaluator.py` (task-specific scoring logic)
  - `utils/dependencies.yml` (service dependencies)
  - optional `workspace/`, `scenarios.json`, `populate_data.py`

### 1.4 Evaluation Protocol
- Shared evaluator entrypoint: `workspaces/openagentsafety_base_image/eval.py`
  - Decrypts task evaluator, calls `grade_checkpoints(trajectory)`.
- Shared score schema: `workspaces/openagentsafety_base_image/scoring.py`
  - `Checkpoint(total, result)`, `Result(checkpoints, final_score)`.

### 1.5 Trajectory Artifact
- Produced by OpenHands in JSON form (`traj_*.json`).
- Contains per-step events, actions, args, tool metadata, observations, and model metadata.


## 2) Fit Against CI Privacy Sandbox Goals

Your target requires:
- executable tools with state + side effects (local mock or remote real services)
- explicit info-flow extraction from action traces
- CI rule checking over (subject, sender, recipient, info_type, transmission_principle)
- configurable long-horizon scenarios with norms
- deterministic baseline policy and optional LLM policy adapter

OAS has strong runtime/evaluation scaffolding, but is not CI-native.


## 3) Reuse Matrix

## 3.1 Reuse As-Is
- **Task/eval loop orchestration pattern**
  - Reusable concept from `evaluation/run_eval.sh` + `evaluation/run_eval.py`.
- **Checkpoint score schema**
  - `Result` / `Checkpoint` in `workspaces/openagentsafety_base_image/scoring.py`.
- **Long-trajectory execution mindset**
  - Existing system already supports long multi-step runs and artifact persistence.

## 3.2 Reuse With Adaptation
- **Trajectory logging**
  - Existing `traj_*.json` is rich; adapt to CI-focused JSONL trace with per-step CI fields.
- **Task packaging convention**
  - `task.md + utils + workspace + scenario` can inspire CI scenario bundle layout.
- **Evaluation entrypoint pattern**
  - Keep one generic evaluator entrypoint, but replace task-specific custom graders with
    CI flow extraction + norm rule engine.

## 3.3 Redesign Required
- **Tool layer**
  - Current tasks rely on real services (ownCloud/GitLab/Plane/RocketChat), but CI needs
    explicit tool contracts and CI metadata on each call.
  - CI Sandbox still needs a unified tool API surface:
    `gmail/calendar/docs/chat/files/web/contacts`.
  - Tool backend can be either:
    - local mocked implementations (deterministic mode), or
    - remote real service adapters (integration mode).
- **CI rule representation**
  - No first-class CI norm schema currently exists.
- **Flow extraction**
  - No generic action-to-flow extractor exists today.
- **Provenance labels**
  - Current tool outputs are not consistently field-level provenance-tagged.
- **Deterministic policy baseline**
  - Current pipeline is primarily LLM-agent centric; scripted deterministic policy is not a first-class mode.


## 4) Compatibility Notes and Constraints

### 4.1 Strongly Compatible
- OAS already values executable environments and action-level evaluation.
- The trajectory-first grading style in many task evaluators is conceptually close to CI action auditing.

### 4.2 Weakly Compatible / Friction
- OAS evaluator model is per-task Python script; CI sandbox needs one reusable policy-agnostic evaluator.
- OAS scenario metadata is not standardized for privacy norms.
- OAS depends on service reset/health infra; CI sandbox needs explicit environment profiles
  (deterministic local vs remote integration) to keep experiments reproducible.


## 5) Recommended Architecture for CI Sandbox (Inside This Repo)

Create a new package:

`ci_sandbox/`
- `main.py` CLI
- `tools/` tool interfaces + implementations
  - `tools/mocked/` deterministic local tools
  - `tools/adapters/` remote real-service adapters
- `evaluator/` CI flow extraction + rules + run-level metrics
- `logging/` unified JSONL trace writer
- `scenarios/` CI-configured scenarios
- `tests/` deterministic tests (tools + evaluator + end-to-end smoke)

Use configuration to select mode:
- `mode=integration` for real tool-calling experiments (recommended default)
- `mode=mocked` for deterministic tests and regression


## 6) Detailed Mapping: Existing -> Proposed

- **`evaluation/run_eval.sh`**
  - Keep idea: batch scenario execution.
  - Replace task scanning logic with scenario registry for CI experiments.

- **`evaluation/run_eval.py`**
  - Keep orchestration shape: init -> run policy loop -> evaluate -> persist artifacts.
  - Add a backend selector so the same policy loop can run against mocked tools or remote adapters.

- **`workspaces/openagentsafety_base_image/scoring.py`**
  - Reuse score dataclasses or mirror structure for CI metrics output.

- **Task-level `utils/evaluator.py`**
  - Replace with generic CI evaluator modules:
    - `ci_extractor.py`
    - `rules.py`
    - `evaluate.py`

- **`workspaces/tasks/*/scenarios.json`**
  - Keep "scenario file" idea.
  - Replace content with CI-native schema (actors, data store, norms, sensitive mappings, utility rubric).


## 7) Key Built-In Issues to Account for During CI Refactor

These are useful issue candidates if you want to improve base OAS while building CI sandbox:

- `evaluation/run_eval.sh` does not fail-fast by default on per-task failure.
- Server/image dependencies can be brittle for reproducibility (external image tags, service resets).
- Evaluator dependencies are installed at runtime; this is slower and less deterministic.
- Some docs reference paths/templates that do not currently exist (task authoring docs drift).


## 8) Minimal Design Decisions to Lock Before Implementation

1. **Scenario schema versioning**
   - Use `schema_version` for forward compatibility.

2. **Norm precedence**
   - Define deterministic rule order: `deny` overrides `allow` on conflicts.

3. **Transmission principle taxonomy**
   - Fixed initial enum (email, dm, file_share, doc_share, internal_note, etc.).

4. **Sensitive item mapping**
   - Start deterministic: exact match + regex + explicit field mapping.

5. **Trace schema stability**
   - JSONL with run metadata header + per-step entries; never mutate old fields.

6. **Policy interface**
   - Must support both scripted deterministic policy and pluggable LLM policy adapter.

7. **Tool backend mode**
   - Standardize backend selection (`mocked` vs `integration`) per run/scenario.
   - Keep output schemas identical across backends so evaluator logic is backend-agnostic.


## 9) Suggested Next Milestone

Implement a minimal vertical slice:
- one scenario
- two tools (`gmail`, `docs`)
- CI extractor + rules engine
- deterministic scripted policy
- JSONL trace + metrics report

Then scale to full toolset and three benchmark scenarios.
