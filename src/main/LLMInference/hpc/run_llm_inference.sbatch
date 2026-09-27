#!/bin/bash -l
#SBATCH --job-name=specimin_llm
#SBATCH --output=%x.%j.out
#SBATCH --error=%x.%j.err
#SBATCH --account=mjk76
#SBATCH --partition=gpu
#SBATCH --qos=standard
#SBATCH --time=06:00:00
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8
#SBATCH --gres=gpu:1
#
# Serves Qwen/Qwen3.8-27B with vLLM on this job's GPU and runs
# RunLLMInferenceAll.py against it (LLM_PROVIDER=vllm). See hpc/README.md.
#
# Submit from src/main/LLMInference (or its hpc/ subdirectory, or the repo
# root):   sbatch hpc/run_llm_inference.sbatch
# Overrides (export before sbatch; Slurm passes the environment through):
#   SPECIMIN_OUT       slices to process (default below)
#   LLM_INFERENCE_DIR  directory containing RunLLMInferenceAll.py
#   VLLM_PORT          server port (default 8000)
#   LLM_CONCURRENCY, LLM_MAX_TOKENS, LLM_SKIP_DONE   (defaults 8, 16384, 1)
#   RUN_ARGS           extra arguments, e.g. RUN_ARGS=--dry-run

set -o pipefail   # (no -u: conda activation scripts reference unset variables)

module purge > /dev/null 2>&1
module load wulver
module load Miniforge3
eval "$(conda shell.bash hook)"
conda activate /project/mjk76/mc2399/envs/llm
export HF_HOME=/project/mjk76/mc2399/hf_cache HF_HUB_OFFLINE=1

# ── Find RunLLMInferenceAll.py (sbatch runs a copy of this script, so its own
#    location is unknown; use the submit directory). ──────────────────────────
if [[ -z "${LLM_INFERENCE_DIR:-}" ]]; then
    for d in "$SLURM_SUBMIT_DIR" "$SLURM_SUBMIT_DIR/.." "$SLURM_SUBMIT_DIR/src/main/LLMInference"; do
        if [[ -f "$d/RunLLMInferenceAll.py" ]]; then LLM_INFERENCE_DIR="$d"; break; fi
    done
fi
if [[ -z "${LLM_INFERENCE_DIR:-}" || ! -f "$LLM_INFERENCE_DIR/RunLLMInferenceAll.py" ]]; then
    echo "ERROR: RunLLMInferenceAll.py not found. Submit from src/main/LLMInference" >&2
    echo "       or set LLM_INFERENCE_DIR." >&2
    exit 1
fi
cd "$LLM_INFERENCE_DIR" || exit 1
echo "Working directory: $(pwd)"

export SPECIMIN_OUT="${SPECIMIN_OUT:-/project/mjk76/mc2399/speciminout}"
if [[ ! -d "$SPECIMIN_OUT" ]]; then
    echo "ERROR: SPECIMIN_OUT=$SPECIMIN_OUT does not exist (rsync the slices first)." >&2
    exit 1
fi

# ── Start the vLLM server ─────────────────────────────────────────────────────
VLLM_PORT="${VLLM_PORT:-8000}"
VLLM_LOG="$SLURM_SUBMIT_DIR/vllm.${SLURM_JOB_ID}.log"
echo "Node: $(hostname)   GPU: $(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null)"
echo "Starting vLLM on 127.0.0.1:$VLLM_PORT (log: $VLLM_LOG) ..."

vllm serve Qwen/Qwen3.8-27B --host 127.0.0.1 --port "$VLLM_PORT" \
    --served-model-name qwen3.8-27b --max-model-len 32768 \
    --reasoning-parser qwen3 > "$VLLM_LOG" 2>&1 &
VLLM_PID=$!

cleanup() {
    if kill -0 "$VLLM_PID" 2>/dev/null; then
        echo "Stopping vLLM (PID $VLLM_PID) ..."
        kill "$VLLM_PID" 2>/dev/null
        for _ in $(seq 30); do kill -0 "$VLLM_PID" 2>/dev/null || break; sleep 1; done
        kill -9 "$VLLM_PID" 2>/dev/null
    fi
}
trap cleanup EXIT
trap 'exit 143' TERM INT   # run the EXIT trap on scancel / time limit too

# ── Wait for /health (up to 20 min; startup normally takes ~6) ────────────────
HEALTH_URL="http://127.0.0.1:$VLLM_PORT/health"
DEADLINE=$((SECONDS + 20 * 60))
until curl -sf -o /dev/null "$HEALTH_URL"; do
    if ! kill -0 "$VLLM_PID" 2>/dev/null; then
        echo "ERROR: vLLM exited during startup. Last lines of $VLLM_LOG:" >&2
        tail -n 40 "$VLLM_LOG" >&2
        exit 1
    fi
    if (( SECONDS >= DEADLINE )); then
        echo "ERROR: vLLM was not ready after 20 minutes. Last lines of $VLLM_LOG:" >&2
        tail -n 40 "$VLLM_LOG" >&2
        exit 1
    fi
    sleep 10
done
echo "vLLM ready after ${SECONDS}s."

# ── Run the inference ─────────────────────────────────────────────────────────
export LLM_PROVIDER=vllm
export LLM_BASE_URL="http://127.0.0.1:$VLLM_PORT/v1"
export LLM_CONCURRENCY="${LLM_CONCURRENCY:-8}"
export LLM_MAX_TOKENS="${LLM_MAX_TOKENS:-16384}"
export LLM_SKIP_DONE="${LLM_SKIP_DONE:-1}"
export PYTHONUNBUFFERED=1

# shellcheck disable=SC2086  # RUN_ARGS is intentionally word-split
python3 RunLLMInferenceAll.py ${RUN_ARGS:-}
STATUS=$?
echo "RunLLMInferenceAll.py exited with status $STATUS"
exit $STATUS
