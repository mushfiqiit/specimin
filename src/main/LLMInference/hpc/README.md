# Running the LLM inference on NJIT Wulver

`run_llm_inference.sbatch` serves `Qwen/Qwen3.8-27B` (bf16) with vLLM on one
A100 80GB and runs `RunLLMInferenceAll.py` against it with
`LLM_PROVIDER=vllm` (see `../llm_provider.py`). Wulver needs VPN + MFA, so the
slices are copied there and back by hand.

## Workflow

1. **Locally:** run the existing steps (RunSpeciminAll.py, RunCheckerAll.sh,
   ExtractRootWarning.py) to produce `speciminout/` with `root-warning.txt` in
   each slice.
2. **Copy to Wulver:**

       rsync -av speciminout/ mc2399@wulver.njit.edu:/project/mjk76/mc2399/speciminout/

3. **On Wulver (login node),** from the directory that contains
   `RunLLMInferenceAll.py` (this repo's `src/main/LLMInference`):

       sbatch hpc/run_llm_inference.sbatch
       sq                              # monitor the job
       tail -f specimin_llm.<jobid>.out

   Output: `specimin_llm.<jobid>.out` / `.err` and the server log
   `vllm.<jobid>.log`, all in the submit directory. Each slice gets
   `null-inference-report.txt` and a `<slice>LLMInferenced/` sibling.
4. **Copy back:**

       rsync -av mc2399@wulver.njit.edu:/project/mjk76/mc2399/speciminout/ speciminout/

5. **Locally:** continue with the NullAway re-check of the `*LLMInferenced`
   folders.

## Testing before a full run

1. Dry run (no GPU, no server; fine on the login node) — checks the slices
   and prints the prompt sizes:

       SPECIMIN_OUT=/project/mjk76/mc2399/speciminout python3 RunLLMInferenceAll.py --dry-run

2. A real job on a copy of 2–3 slices:

       mkdir -p /project/mjk76/mc2399/speciminout-test
       cp -r /project/mjk76/mc2399/speciminout/<slice1> /project/mjk76/mc2399/speciminout/<slice2> \
             /project/mjk76/mc2399/speciminout-test/
       SPECIMIN_OUT=/project/mjk76/mc2399/speciminout-test sbatch hpc/run_llm_inference.sbatch

## What the job does

- Loads `wulver` + `Miniforge3`, activates `/project/mjk76/mc2399/envs/llm`
  (its activation hook sets `LD_LIBRARY_PATH` and
  `VLLM_USE_FLASHINFER_SAMPLER=0`), and uses the pre-downloaded model in
  `HF_HOME=/project/mjk76/mc2399/hf_cache` with `HF_HUB_OFFLINE=1`.
- Starts `vllm serve` on `127.0.0.1:8000` in the background and polls
  `/health` for up to 20 minutes (startup takes ~6). If the server exits or
  never becomes ready, the job fails and prints the end of the vLLM log.
- Runs `RunLLMInferenceAll.py` with `LLM_PROVIDER=vllm LLM_CONCURRENCY=8
  LLM_MAX_TOKENS=16384 LLM_SKIP_DONE=1`, then stops the server (also on
  `scancel` or when the time limit is reached).

## Resuming

`LLM_SKIP_DONE=1` skips slices that already have `null-inference-report.txt`,
so if the job is preempted or hits the 6-hour limit, just submit it again. To
redo everything, submit with `LLM_SKIP_DONE=0` (or delete the reports).

## Knobs

Export any of these before `sbatch` (Slurm passes the environment through):

| Variable | Default in the job | Meaning |
|---|---|---|
| `SPECIMIN_OUT` | `/project/mjk76/mc2399/speciminout` | slices to process |
| `LLM_CONCURRENCY` | `8` | slices sent to the server at once |
| `LLM_MAX_TOKENS` | `16384` | `max_tokens` per slice (reasoning + answer) |
| `LLM_SKIP_DONE` | `1` | skip slices that already have a report |
| `LLM_TIMEOUT` | `1200` | seconds before one request times out |
| `VLLM_PORT` | `8000` | server port (change it if 8000 is taken on the node) |
| `LLM_INFERENCE_DIR` | submit dir | directory containing `RunLLMInferenceAll.py` |
| `RUN_ARGS` | — | extra arguments for `RunLLMInferenceAll.py` |

Prompt tokens + `LLM_MAX_TOKENS` must fit in `--max-model-len 32768`; vLLM
rejects a request that doesn't with HTTP 400 (that slice is reported as
failed, the rest continue). One slice takes ~3–4 min, so 8 at a time is
roughly 2 slices/min; roughly 650 slices fit in one 6-hour job (resubmit for more).
