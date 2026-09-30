# Runbook: LLM inference on Wulver (slices + usage context)

This is a step-by-step guide to running `RunLLMInferenceAll.py` on a set of
Specimin slices using the self-hosted model on NJIT Wulver
(`hpc/run_llm_inference.sbatch`). It includes the field usage context that
`RunLLMInferenceAll.py` adds to each prompt. Run the steps in this order.
Commands marked **[laptop]** run on your Mac, and **[wulver]** on the Wulver
login node.

For how the job itself works (vLLM, health check, settings), see
[`README.md`](README.md) in this folder. For the whole evaluation pipeline, see
`src/main/README_EvaluationPipeline.md`.

---

## 0. Set these once per terminal

**[laptop]**
```bash
export JUNIT_DIR=~/Documents/junit4
export SPECIMIN_DIR=~/Documents/specimin
export SPECIMIN_OUT=$JUNIT_DIR/speciminout            # the slices for THIS run
export JAVA_HOME=$(/usr/libexec/java_home -v 21)
SPE=$SPECIMIN_DIR/src/main/SpeciminPerformanceEvaluation
LLM=$SPECIMIN_DIR/src/main/LLMInference

# A new remote folder for every run. Reusing an old one mixes in its reports,
# and LLM_SKIP_DONE=1 would then skip those slices.
RUN=round2
REMOTE=mc2399@wulver.njit.edu
REMOTE_OUT=/project/mjk76/mc2399/speciminout_$RUN
REMOTE_CODE=/project/mjk76/mc2399/LLMInference         # where RunLLMInferenceAll.py lives on Wulver
```

## 1. Prepare the slices (laptop)

These steps assume `RunSpeciminAll.py` has already produced `$SPECIMIN_OUT`.
Run them in this order. `FixSpeciminNullInits.py` changes the slices, so NullAway
and the root-warning selection must run **after** it; the LLM should see the
warnings of the code it is actually given.

**[laptop]**
```bash
python3 $LLM/FixSpeciminNullInits.py          # remove Specimin's artificial "= null" field initializers
bash    $SPE/RunCheckerAll.sh                 # NullAway on each (fixed) slice
python3 $LLM/ExtractRootWarning.py            # root-warning.txt for slices that reproduce their warning

ls $SPECIMIN_OUT/*/root-warning.txt | wc -l   # = the number of slices the LLM will process
```

If you already ran `FixSpeciminNullInits.py` after `ExtractRootWarning.py`,
just run the last two commands again; `FixSpeciminNullInits.py` doesn't need
to run twice.

## 2. Generate the usage context (laptop)

`RunLLMInferenceAll.py` adds a slice's `usage-context.txt`, if there is one, to
the prompt as read-only evidence. That file holds the lines from the original
program (outside the slice) that assign, dereference, or null-check the fields
the slice contains. **Nothing on this branch writes that file.** The extractor,
`ExtractUsageContext.py`, lives on the `LLMInference` branch, together with the
`ExtractWarningMethods.py` helpers it imports. It works on JUnit slices
unchanged, as long as `EVENTBUS_SRC_ROOT` points at the JUnit source root.

Fetch the two files into a separate folder (this doesn't change your checkout):

**[laptop]**
```bash
mkdir -p ~/Documents/usage-context-tool
cd $SPECIMIN_DIR && git fetch origin LLMInference
for f in ExtractUsageContext.py ExtractWarningMethods.py; do
  git show origin/LLMInference:src/main/LLMInferencePython/$f > ~/Documents/usage-context-tool/$f
done
```

Write `usage-context.txt` for every slice that has a root warning. Point
`EVENTBUS_SRC_ROOT` at the source the slices were cut from; after a merge
round, that is the annotated JUnit.

**[laptop]**
```bash
export EVENTBUS_SRC_ROOT=$JUNIT_DIR/src/main/java
cd ~/Documents/usage-context-tool
for d in "$SPECIMIN_OUT"/*/; do
  d=${d%/}
  case "$d" in *LLMInferenced) continue ;; esac
  [ -f "$d/root-warning.txt" ] || continue
  python3 ExtractUsageContext.py --slice "$d" > "$d/usage-context.txt"
  # The extractor prints this placeholder when it finds nothing.
  # RunLLMInferenceAll.py would put it into the prompt, so delete it.
  if [ "$(cat "$d/usage-context.txt")" = "(no field usages found)" ]; then
    rm "$d/usage-context.txt"
  fi
done

ls $SPECIMIN_OUT/*/usage-context.txt | wc -l   # slices that will get usage context
```

Options: `--scope repo` searches every source file instead of only each
field's declaring file (more recall, more noise). `--lines N` sets the number
of context lines around each hit (default 1). The output is capped at 120 lines
per slice.

## 3. Check the prompts locally (laptop, no LLM calls)

**[laptop]**
```bash
cd $LLM
LLM_PROVIDER=vllm python3 RunLLMInferenceAll.py --dry-run > /tmp/dryrun.txt
grep -c "Prompt     :" /tmp/dryrun.txt    # slices that will be sent
grep -c "Usage ctx  :" /tmp/dryrun.txt    # ... of which have usage context
grep "Prompt     :" /tmp/dryrun.txt | sort -t: -k2 -n -r | head -3   # largest prompts
```

The largest prompts should stay well under about 50,000 characters. The model's
context is 32,768 tokens, and a long reasoning answer needs most of that.

## 4. Copy slices (and code, if it changed) to Wulver (laptop, on the NJIT VPN)

**[laptop]**
```bash
# Must not exist yet (see REMOTE_OUT above).
ssh $REMOTE "ls -d $REMOTE_OUT 2>/dev/null && echo 'EXISTS: choose another RUN name' || echo ok"

# The slices, with their root-warning.txt and usage-context.txt. Each slice's
# Gradle build output is skipped.
rsync -av --exclude '/*/build/' --exclude '/*/.gradle/' \
  "$SPECIMIN_OUT/" "$REMOTE:$REMOTE_OUT/"

# Only if the inference code changed since the last run:
rsync -av --exclude '__pycache__/' "$LLM/" "$REMOTE:$REMOTE_CODE/"
```

## 5. Check on Wulver (login node, no GPU)

**[wulver]**
```bash
ssh mc2399@wulver.njit.edu
REMOTE_OUT=/project/mjk76/mc2399/speciminout_round2        # same value as on the laptop
cd /project/mjk76/mc2399/LLMInference

ls $REMOTE_OUT/*/root-warning.txt   | wc -l    # same count as step 1
ls $REMOTE_OUT/*/usage-context.txt  | wc -l    # same count as step 2
ls $REMOTE_OUT/*/null-inference-report.txt 2>/dev/null | wc -l   # must be 0 for a fresh run

module load wulver Miniforge3; eval "$(conda shell.bash hook)"
conda activate /project/mjk76/mc2399/envs/llm
SPECIMIN_OUT=$REMOTE_OUT LLM_PROVIDER=vllm python3 RunLLMInferenceAll.py --dry-run | tail -5
```

## 6. Submit the job (Wulver)

**[wulver]**, in the folder that contains `RunLLMInferenceAll.py`:
```bash
SPECIMIN_OUT=$REMOTE_OUT LLM_MAX_TOKENS=0 LLM_TIMEOUT=2700 \
  sbatch --time=02:00:00 hpc/run_llm_inference.sbatch
```

- `LLM_MAX_TOKENS=0` lets each answer use the rest of the context window. The
  job's default, 16384, cut off some of Qwen's long reasoning answers
  (`finish_reason='length'`, empty reply), and those slices then needed a
  second job. `LLM_TIMEOUT=2700` allows for the longer answers.
- `--time`: 8 slices run at a time, and each takes roughly 4 to 8 minutes
  without the cap. Plan on `ceil(N / 8) × 8 min + 15 min` for startup and
  margin. For example, 70 slices take about 1.5 hours, so ask for 2 hours. A
  shorter limit usually starts sooner, and if the job runs out of time you can
  simply resubmit (step 8).
- The job starts vLLM, waits for `/health`, and runs `RunLLMInferenceAll.py`
  with `LLM_PROVIDER=vllm LLM_CONCURRENCY=8 LLM_SKIP_DONE=1`. It always stops
  the server when it exits.

## 7. Monitor (Wulver)

**[wulver]**
```bash
sq                                                    # PD = pending, R = running
tail -f specimin_llm.<jobid>.out                      # Ctrl-C stops only tail
ls $REMOTE_OUT/*/null-inference-report.txt | wc -l    # progress
```

You should see `vLLM ready after …s`, then `OK — 'qwen3.8-27b' is available`,
`Concurrency: 8`, then one block per finished slice, and a line
`Usage ctx  : N line(s)` for slices that have usage context. If the job fails
before that point, the cause is in `vllm.<jobid>.log`.

## 8. When it finishes (Wulver)

**[wulver]**
```bash
tail -n 30 specimin_llm.<jobid>.out                   # Summary, Skipped, Failed
grep -A3 "\[ERROR\]\|\[WARN\]" specimin_llm.<jobid>.out | head -40
```

- **`COMPLETED`** in `sq`: every slice succeeded.
- **`FAILED`**: at least one slice failed, and the script exits with status 1.
  Look at the `Failed:` list. Resubmit the step-6 command unchanged: with
  `LLM_SKIP_DONE=1`, only the slices without a report are rerun.
- **`TIMEOUT`**: resubmit the same way.
- **`finish_reason='length'`** errors, even with `LLM_MAX_TOKENS=0`: the model
  is probably repeating itself while reasoning. Resubmitting once usually gets
  a different answer.

## 9. Copy the results back (laptop, on the VPN)

**[laptop]**
```bash
rsync -av "$REMOTE:$REMOTE_OUT/" "$SPECIMIN_OUT/"
ls -d $SPECIMIN_OUT/*LLMInferenced | wc -l
ls $SPECIMIN_OUT/*/null-inference-report.txt | wc -l
```

Each slice now has `null-inference-report.txt`, and each has a sibling
`<slice>LLMInferenced/` folder with the annotated files.

## 10. Next: merge and re-check (laptop)

See `README_EvaluationPipeline.md`, steps 2.6 to 2.8:

```bash
cd $SPECIMIN_DIR
./gradlew applyAnnotations -PspeciminOut=$SPECIMIN_OUT -PdryRun=true -Preport=$JUNIT_DIR/merge-$RUN-preview.tsv
./gradlew applyAnnotations -PspeciminOut=$SPECIMIN_OUT -Preport=$JUNIT_DIR/merge-$RUN.tsv
OUT_DIR=$JUNIT_DIR/nullaway-after-$RUN bash $SPE/GenerateNullAwayWarnings.sh
```

Compare `nullaway-after-$RUN/nullaway-warnings.txt` with the previous round's
warnings, using the comparison script in `README_EvaluationPipeline.md`
(step 2.8).

---

## Checklist

| # | Where | Step | Check |
|---|---|---|---|
| 1 | laptop | Fix → RunCheckerAll → ExtractRootWarning | `root-warning.txt` count |
| 2 | laptop | usage context | `usage-context.txt` count; no placeholder files |
| 3 | laptop | dry run | prompt count and size |
| 4 | laptop | rsync to a **new** `$REMOTE_OUT` | the remote folder did not exist |
| 5 | wulver | counts + dry run | same counts, 0 reports |
| 6 | wulver | `sbatch` with `SPECIMIN_OUT`, `LLM_MAX_TOKENS=0`, `LLM_TIMEOUT=2700`, `--time` | job id |
| 7 | wulver | monitor | `vLLM ready`, per-slice blocks |
| 8 | wulver | summary; resubmit failed slices | `Failed:` list empty |
| 9 | laptop | rsync back | `*LLMInferenced` count = report count |
| 10 | laptop | applyAnnotations → GenerateNullAwayWarnings | after-round warnings |
