#!/bin/bash
# Run all Tier 1 privacy tasks and save trajectories for offline evaluation.
# Usage: bash run_privacy_tier1.sh [--start-from NAME] [--agent-llm-config CFG] [--env-llm-config CFG]

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TASKS_DIR="$(cd "$SCRIPT_DIR/../workspaces/tasks" && pwd)"

AGENT_LLM_CONFIG="${AGENT_LLM_CONFIG:-group1}"
ENV_LLM_CONFIG="${ENV_LLM_CONFIG:-group2}"
OUTPUTS_PATH="${OUTPUTS_PATH:-$SCRIPT_DIR/outputs_privacy_tier1}"
SERVER_HOSTNAME="${SERVER_HOSTNAME:-localhost}"
START_FROM=""

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        --start-from)       START_FROM="$2"; shift 2 ;;
        --agent-llm-config) AGENT_LLM_CONFIG="$2"; shift 2 ;;
        --env-llm-config)   ENV_LLM_CONFIG="$2"; shift 2 ;;
        --outputs-path)     OUTPUTS_PATH="$2"; shift 2 ;;
        --server-hostname)  SERVER_HOSTNAME="$2"; shift 2 ;;
        *) echo "Unknown arg: $1"; exit 1 ;;
    esac
done

# Absolute path
if [[ ! "$OUTPUTS_PATH" = /* ]]; then
    OUTPUTS_PATH="$(pwd)/$OUTPUTS_PATH"
fi

mkdir -p "$OUTPUTS_PATH"

# Resolve poetry
POETRY_BIN="${POETRY_BIN:-$(command -v poetry)}"
if [[ -z "$POETRY_BIN" ]]; then
    echo "Error: poetry not found. Activate conda env first."
    exit 1
fi
export POETRY_VIRTUALENVS_CREATE="${POETRY_VIRTUALENVS_CREATE:-false}"

echo "=========================================="
echo "Privacy Tier 1 Evaluation Runner"
echo "=========================================="
echo "Agent LLM:  $AGENT_LLM_CONFIG"
echo "Env LLM:    $ENV_LLM_CONFIG"
echo "Outputs:    $OUTPUTS_PATH"
echo "Server:     $SERVER_HOSTNAME"
echo "Start from: ${START_FROM:-<beginning>}"
echo "=========================================="

TOTAL=0
DONE=0
SKIPPED=0
FAILED=0
SKIP_UNTIL_MATCH=${START_FROM:+true}

# Collect and sort privacy task dirs
TASK_DIRS=()
for task_dir in "$TASKS_DIR"/privacy-*/; do
    [ -d "$task_dir" ] && TASK_DIRS+=("$task_dir")
done

# Sort numerically by main number
IFS=$'\n' TASK_DIRS=($(printf '%s\n' "${TASK_DIRS[@]}" | sort -t'-' -k3 -n)); unset IFS

TOTAL=${#TASK_DIRS[@]}
echo "Found $TOTAL privacy tasks."
echo ""

for task_dir in "${TASK_DIRS[@]}"; do
    task_name=$(basename "$task_dir")

    # Skip until we reach START_FROM
    if [[ "$SKIP_UNTIL_MATCH" == "true" ]]; then
        if [[ "$task_name" == *"$START_FROM"* ]]; then
            SKIP_UNTIL_MATCH=false
        else
            echo "[$DONE/$TOTAL] Skipping $task_name (before --start-from)"
            ((SKIPPED++))
            continue
        fi
    fi

    # Skip if trajectory already exists
    if [ -f "$OUTPUTS_PATH/traj_${task_name}.json" ]; then
        echo "[$DONE/$TOTAL] Skipping $task_name — trajectory exists"
        ((SKIPPED++))
        continue
    fi

    echo ""
    echo "=========================================="
    echo "[$((DONE+1))/$TOTAL] Running: $task_name"
    echo "  Started: $(date '+%Y-%m-%d %H:%M:%S')"
    echo "=========================================="

    cd "$SCRIPT_DIR"
    "$POETRY_BIN" run python run_eval.py \
        --agent-llm-config "$AGENT_LLM_CONFIG" \
        --env-llm-config "$ENV_LLM_CONFIG" \
        --outputs-path "$OUTPUTS_PATH" \
        --server-hostname "$SERVER_HOSTNAME" \
        --task-path "$task_dir" \
        --no-fake-user \
        --max-iterations 25 \
        --enable-mcp \
        2>&1 | tee "$OUTPUTS_PATH/log_${task_name}.txt"

    EXIT_CODE=${PIPESTATUS[0]}
    ((DONE++))

    if [ $EXIT_CODE -eq 0 ]; then
        echo "  Completed: $(date '+%Y-%m-%d %H:%M:%S') — OK"
    else
        echo "  Completed: $(date '+%Y-%m-%d %H:%M:%S') — FAILED (exit $EXIT_CODE)"
        ((FAILED++))
    fi

    # Brief cooldown to let Docker clean up
    sleep 2
done

echo ""
echo "=========================================="
echo "DONE"
echo "  Total:   $TOTAL"
echo "  Run:     $DONE"
echo "  Skipped: $SKIPPED"
echo "  Failed:  $FAILED"
echo "  Results: $OUTPUTS_PATH"
echo "=========================================="
