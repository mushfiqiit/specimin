# Evaluation pipeline: Specimin slicing + LLM nullability inference

This document explains how to reproduce the paper's experiments on a target
repository (JUnit 4 is the default and the worked example). There are two
phases, run in order:

1. **Specimin performance evaluation** (`src/main/SpeciminPerformanceEvaluation/`)
   runs NullAway on the original project, slices one reduced program per
   warning with Specimin, re-runs NullAway on each slice, and measures how many
   slices reproduce the warning they were generated for.
2. **LLM inference** (`src/main/LLMInference/`) sends each reproducing slice to
   an LLM, which infers `@Nullable` / `@Nonnull` annotations for that slice's
   warning. The inferred annotations are merged back into the original project,
   and NullAway is re-run on it to compare warnings before and after.

```
                      ┌────────────────── Phase 1: SpeciminPerformanceEvaluation ─────────────────┐
original project ──► GenerateNullAwayWarnings.sh ──► ExtractWarningMethods.py ──► RunSpeciminAll.py
                      (baseline warnings)                                          │
                                                        RunCheckerAll.sh ◄─────────┘
                                                        (RunCFCheckerAll.sh, optional)
                                                              │
                                                        CompareSliceWarnings.py ──► reproduction rate
                      └────────────────────────────────────────────────────────────────────────────┘
                      ┌────────────────────────── Phase 2: LLMInference ───────────────────────────┐
copy of slices ──► FixSpeciminNullInits.py ──► RunCheckerAll.sh ──► ExtractRootWarning.py
                                                                         │
                                                        ExtractUsageContext.py ◄┘
                                                                         │
                  RunLLMInferenceAll.py (hosted API, or vLLM on an HPC GPU node) ◄┘
                                                                         │
                  ./gradlew applyAnnotations (merge into the original project) ◄┘
                                                                         │
                  GenerateNullAwayWarnings.sh (after-annotation warnings) ──► compare with baseline
                      └────────────────────────────────────────────────────────────────────────────┘
```

---

## 0. Prerequisites

| Requirement | Why |
|---|---|
| This Specimin checkout, buildable with `./gradlew` (JDK 17 toolchain) | `RunSpeciminAll.py` runs Specimin through `gradlew run`, and `applyAnnotations` is a Gradle task here. The wrapper is also copied into every throwaway NullAway build. |
| **JDK 21+ active** for the NullAway steps | Error Prone 2.50.0, used by `GenerateNullAwayWarnings.sh` and `RunCheckerAll.sh`, only loads on a JDK 21+ runtime. On macOS: `export JAVA_HOME=$(/usr/libexec/java_home -v 21)` |
| Python 3.9+ | All `.py` scripts. Phase 1 needs only the standard library. |
| Network access on the first run | Gradle, Error Prone, NullAway and hamcrest are downloaded from Maven Central. |
| For Phase 2: an LLM endpoint | Groq (`pip install groq`, `GROQ_API_KEY`), NVIDIA API catalog or any OpenAI-compatible server (`pip install openai`), or a self-hosted vLLM server (see `LLMInference/hpc/README.md`). |
| A clean git checkout of the target project | Phase 2 edits the original sources in place, and git is how you inspect and undo that. |

Every script takes its paths from environment variables. The defaults point at
the authors' machine (`/Users/mushfiqurrahmanchowdhury/Documents/...`), so set
these once in your shell:

```bash
export JUNIT_DIR=~/Documents/junit4                 # target project checkout
export JUNIT_SRC_ROOT=$JUNIT_DIR/src/main/java      # its Java source root
export SPECIMIN_DIR=~/Documents/specimin            # this repository
export JAR_PATH=~/junit-deps                        # the project's compile-time dependency jars
export SPECIMIN_OUT=$JUNIT_DIR/speciminout          # Phase 1 slice folders
export NULLAWAY_WARNINGS_FILE=$JUNIT_DIR/nullaway-warnings.txt
export WARNING_METHODS_FILE=$JUNIT_DIR/warningMethods.jsonl
export JAVA_HOME=$(/usr/libexec/java_home -v 21)    # macOS; set a JDK 21+ elsewhere

SPE=$SPECIMIN_DIR/src/main/SpeciminPerformanceEvaluation
LLM=$SPECIMIN_DIR/src/main/LLMInference
```

The NullAway configuration is the same everywhere, so results are comparable
across the original project, the slices, and the annotated project:
Error Prone 2.50.0, NullAway 0.13.7, `AnnotatedPackages=org.junit,junit`,
`JSpecifyMode=false`, and `--release 8`.

Start from an unmodified target project:

```bash
cd $JUNIT_DIR && git status        # should be clean; the baseline must be the original sources
```

---

## Phase 1: evaluate Specimin (`SpeciminPerformanceEvaluation/`)

### Step 1.1: baseline NullAway warnings on the original project

```bash
bash $SPE/GenerateNullAwayWarnings.sh          # PROJECT=junit is the default
```

This builds a throwaway Gradle project in `$JUNIT_DIR/.nullaway-build` whose
source set points at `$JUNIT_SRC_ROOT`, with only NullAway enabled, and compiles.

| Output | Contents |
|---|---|
| `$JUNIT_DIR/nullaway-report.txt` | full build log |
| `$JUNIT_DIR/nullaway-warnings.txt` | one `[NullAway]` warning per line: the **baseline** |
| `$JAR_PATH/*.jar` | the project's compile-time dependencies (hamcrest-core), used by Specimin and the slice checks |

**Keep a copy of the baseline.** Step 2.7 runs this script again, so write that
later run to a different `OUT_DIR`, or save the baseline now:

```bash
mkdir -p $JUNIT_DIR/nullaway-before
cp $JUNIT_DIR/nullaway-{report,warnings}.txt $JUNIT_DIR/nullaway-before/
```

Other projects: set `PROJECT=gson` or `PROJECT=eventbus` (see the script
header for each project's one-time setup).

### Step 1.2: find the target of each warning

```bash
python3 $SPE/ExtractWarningMethods.py
```

This writes `warningMethods.jsonl`, with one entry per warning (warnings are
not deduplicated). Each entry holds the enclosing method or constructor
(`"kind": "method"`, sliced with `--targetMethod`) or a bare field
(`"kind": "field"`, sliced with `--targetField`). Warnings inside `static { }`
or instance initializer blocks are skipped, because Specimin always prunes
those blocks.

### Step 1.3: slice one program per warning with Specimin

```bash
python3 $SPE/RunSpeciminAll.py --dry-run     # optional: print the targets and roots only
python3 $SPE/RunSpeciminAll.py
```

This creates one folder per entry, `$SPECIMIN_OUT/<NN>_<member>/`, containing:
- the reduced `.java` files (the slice), under their package paths;
- `warning.txt`: the exact original warning the slice was generated for;
- `root.txt`: the `--root` Specimin was run with. It is derived per target from
  the warning's file path, and later steps read it to locate the original files.

A folder with no `warning.txt` means Specimin failed on that target.

### Step 1.4: run NullAway on every slice

```bash
bash $SPE/RunCheckerAll.sh
```

This puts a throwaway Gradle project into each slice (same NullAway
configuration as step 1.1, with jsr305 and `$JAR_PATH` on the classpath) and
writes `nullaway-report.txt` and `nullaway-warnings.txt` into each slice folder.

Optional: `bash $SPE/RunCFCheckerAll.sh` runs the Checker Framework's Nullness
Checker on each slice as well (writes `cf-report.txt` and `cf-warnings.txt`,
using a nested `cf-project/`). It doesn't interfere with `RunCheckerAll.sh`.

### Step 1.5: measure reproduction

```bash
python3 $SPE/CompareSliceWarnings.py
```

For each slice, this checks whether its own `nullaway-warnings.txt` contains
the warning from `warning.txt`. A match needs the same file name and the same
message; `(line N)` references inside the message are normalized, and a
superset of fields is allowed for "initializer does not guarantee..." messages.
A same-line match counts as inconclusive, not reproduced.

| Output | Contents |
|---|---|
| `<slice>/reproduction-check.txt` | the verdict (`REPRODUCED` / `NOT REPRODUCED`) and the evidence considered |
| `$SPECIMIN_OUT/summary.txt` | totals across all slices: **the Specimin evaluation result** |

**Phase 1 results** are the number of baseline warnings, targets, successful
Specimin runs (folders with `warning.txt`), and reproduced slices
(`summary.txt`).

---

## Phase 2: LLM nullability inference (`LLMInference/`)

### Step 2.0: work on a copy of the slices

Phase 2 changes the slices (step 2.1) and adds files to them (reports and
`*LLMInferenced/` folders). Keep the Phase 1 slices untouched:

```bash
cp -R $JUNIT_DIR/speciminout $JUNIT_DIR/speciminoutllm
export SPECIMIN_OUT=$JUNIT_DIR/speciminoutllm     # every Phase 2 step uses the copy
```

### Step 2.1: remove Specimin's artificial `= null` field initializers

```bash
python3 $LLM/FixSpeciminNullInits.py --dry-run
python3 $LLM/FixSpeciminNullInits.py
```

Specimin sometimes stubs a field as `Foo f = null;` even though the original
declares it without an initializer. The LLM would then (reasonably) infer
`@Nullable` for a field that isn't nullable in the original. This script
removes `= null` wherever the original source, found through each slice's
`root.txt`, has no null initializer.

### Step 2.2: re-check the corrected slices

```bash
bash $SPE/RunCheckerAll.sh        # with SPECIMIN_OUT still set to the copy
```

Step 2.1 can change a slice's NullAway warnings, so rerun the check to make
the next step select warnings from the code the LLM will actually see.

### Step 2.3: select each slice's root warning

```bash
python3 $LLM/ExtractRootWarning.py
```

This writes `root-warning.txt`: the one line of the slice's
`nullaway-warnings.txt` that reproduces `warning.txt`. It uses the same
matching rule as `CompareSliceWarnings.py`, which it imports. Slices that don't
reproduce their warning get no `root-warning.txt` and are skipped by the next
step. The LLM is shown only this warning, not the side-effect warnings caused
by stubbing.

### Step 2.3b: usage context for each root warning

```bash
python3 $LLM/ExtractUsageContext.py
```

For each slice with a `root-warning.txt`, this finds the field(s)/method(s)
the warning is about, and collects their declarations and uses from the
original, full source tree. The original location comes from `warning.txt` and
the source root from `root.txt`; `--src-root` overrides it. The excerpts are
written to `usage-context.txt` in that slice's folder, and step 2.4 adds them to
the prompt. Warnings about local variables get no file.

### Step 2.4: infer annotations with the LLM

```bash
python3 $LLM/RunLLMInferenceAll.py --dry-run   # builds the prompts, calls no API
python3 $LLM/RunLLMInferenceAll.py
```

For every slice with a `root-warning.txt`, this sends one prompt containing
the reduced source and the warning. It asks the LLM for (A) annotation
decisions, (B) the cause of the warning, and (C) the fully annotated files.
Only annotations are allowed (no added null checks), using `javax.annotation`
(jsr305).

| Output | Contents |
|---|---|
| `<slice>/null-inference-report.txt` | the model's full answer |
| `<slice>LLMInferenced/` | a sibling folder holding the annotated files (from section C, with imports added by `AddNonnullImport.ensure_imports`) plus the unchanged files |

**Choosing the LLM** (see `llm_provider.py`):

| Setting | Values |
|---|---|
| `LLM_PROVIDER` | `groq` (default), `nvidia`, `openai-compatible`, `vllm` |
| `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL`, `LLM_TIMEOUT` | provider overrides |
| `LLM_MAX_RPM` | request rate limit (default 25/min; none for `vllm`) |
| `LLM_CONCURRENCY` | slices processed in parallel (default 1) |
| `LLM_MAX_TOKENS` | `max_tokens` per slice; `0` or unset = the server's default |
| `LLM_REASONING_EFFORT` | `low` / `medium` / `high`, for reasoning models |
| `LLM_SKIP_DONE=1` | skip slices that already have a report, so an interrupted run can resume |

If the API calls fail, run `python3 $LLM/DiagnoseGroq.py` first (it works for
every provider).

**Self-hosted model on an HPC cluster (the paper's Qwen runs):** `LLMInference/hpc/`
contains a Slurm job that starts vLLM (Qwen3.8-27B on one A100 80GB) and runs
this step with `LLM_PROVIDER=vllm LLM_CONCURRENCY=8 LLM_SKIP_DONE=1`. Copy the
slice folder to the cluster, `sbatch` the job, and copy the results back; see
`LLMInference/hpc/README.md`. For a reasoning model, set `LLM_MAX_TOKENS=0` (use
the full context) and `LLM_TIMEOUT=2700`. With a 16k cap, some slices end with
`finish_reason='length'` and an empty answer, and then need a second job to
redo them.

Check the run summary: `Summary: N/M folder(s) succeeded`, plus the lists of
skipped and failed slices. With `LLM_SKIP_DONE=1`, resubmitting reruns only the
slices without a report.

### Step 2.5: optional, check the annotated slices

```bash
# Temporarily point RunCheckerAll.sh at the *LLMInferenced folders:
mkdir -p $JUNIT_DIR/llm-slices && cp -R $SPECIMIN_OUT/*LLMInferenced $JUNIT_DIR/llm-slices/
SPECIMIN_OUT=$JUNIT_DIR/llm-slices bash $SPE/RunCheckerAll.sh
```

This shows, slice by slice, whether the root warning is gone after the LLM's
annotations, before anything is merged.

### Step 2.6: merge the inferred annotations into the original project

```bash
cd $SPECIMIN_DIR
./gradlew applyAnnotations -PspeciminOut=$SPECIMIN_OUT -PdryRun=true -Preport=$JUNIT_DIR/merge-preview.tsv
./gradlew applyAnnotations -PspeciminOut=$SPECIMIN_OUT -Preport=$JUNIT_DIR/merge.tsv
cd $JUNIT_DIR && git diff --stat
```

`ApplyAnnotations`
(`src/main/java/org/checkerframework/specimin/nullaway/ApplyAnnotations.java`)
works like this:
- **Input:** it reads every `*LLMInferenced/` folder and maps each file to
  `<root.txt>/<same relative path>`. Stub files with no original are skipped.
- **Matching:** declarations are matched by enclosing type (nested classes
  included), then name, then parameter types with generics and packages erased.
  It handles fields, method returns, and method and constructor parameters.
  Anonymous and local classes are skipped.
- **Overlapping slices:** all slices are read before anything is written, so
  each original declaration gets one decision. When slices disagree,
  `-PonConflict=nullable|nonnull|skip` decides (default `nullable`); every
  conflict is printed.
- **Output:** each original file is written once, with its formatting preserved
  (`LexicalPreservingPrinter`), and `javax.annotation` imports are added. The
  run is idempotent.
- **Options:** `-PsrcRoot=<dir>` overrides `root.txt`, and `-PskipNonnull=true`
  transfers only `@Nullable` (NullAway treats unannotated code as non-null).

`merge.tsv` lists every decision with its status (`added`, `replaced`,
`already present`, `not in original`, `ambiguous`, `conflict ...`) and the
slices it came from.

### Step 2.7: NullAway warnings after the annotations

```bash
OUT_DIR=$JUNIT_DIR/nullaway-after bash $SPE/GenerateNullAwayWarnings.sh
```

This is the same build as step 1.1, now on the annotated sources, writing to
`nullaway-after/` so the baseline isn't overwritten. The JUnit build puts
`com.google.code.findbugs:jsr305:3.0.2` on the compile classpath (as
`compileOnly`), because the merged annotations import
`javax.annotation.Nullable` / `Nonnull`. Without it, every annotated file fails
with `cannot find symbol: class Nullable`, and NullAway never runs.

### Step 2.7b: optional, fix spurious @Nullable

```bash
python3 $LLM/FixSpuriousNullable.py --warnings $JUNIT_DIR/nullaway-after/nullaway-warnings.txt --dry-run
python3 $LLM/FixSpuriousNullable.py --warnings $JUNIT_DIR/nullaway-after/nullaway-warnings.txt --verify
```

For each "dereferenced expression X is @Nullable" warning, this finds the
declaration behind X: the method return, field, or parameter. If the
declaration carries a `@Nullable` that nothing in the original source
justifies, it is rewritten to `@Nonnull`. The source counts as justifying the
`@Nullable` when the method does `return null` (or returns `Map.get(...)`,
another `@Nullable` value, ...), when the field is assigned null, or when a
caller passes a nullable argument. Such declarations are kept and the evidence
is printed, because flipping them only moves the warning to the `return null`
or to the caller. Warnings on local variables are reported and skipped.
`--verify` re-runs NullAway after each flip and keeps it only if the warning
count goes down. Re-run step 2.7 afterwards.

### Step 2.8: compare before and after

```bash
wc -l $JUNIT_DIR/nullaway-before/nullaway-warnings.txt $JUNIT_DIR/nullaway-after/nullaway-warnings.txt
```

Raw counts hide the full picture: an annotation can fix one warning and cause
new ones elsewhere. The inserted imports and annotation lines also shift line
numbers, so match warnings by file and message, not by line:

```bash
python3 - "$JUNIT_DIR/nullaway-before/nullaway-warnings.txt" "$JUNIT_DIR/nullaway-after/nullaway-warnings.txt" <<'EOF'
import re, sys, collections
def load(path):
    out = []
    for line in open(path):
        m = re.match(r'.*?/src/main/java/(.+?):(\d+): warning: \[NullAway\] (.*)', line.strip())
        if m:
            out.append((m[1], int(m[2]), re.sub(r'\(line \d+\)', '(line N)', m[3])))
    return out
before, after = load(sys.argv[1]), load(sys.argv[2])
remaining = collections.Counter((f, msg) for f, _, msg in before)
persisting, new = [], []
for f, line, msg in after:
    if remaining[(f, msg)] > 0:
        remaining[(f, msg)] -= 1
        persisting.append((f, line, msg))
    else:
        new.append((f, line, msg))
print(f"before {len(before)}  after {len(after)}  fixed {sum(remaining.values())}  "
      f"persisting {len(persisting)}  new {len(new)}")
for tag, rows in (("PERSISTING", persisting), ("NEW", new)):
    print(f"\n== {tag}")
    for f, line, msg in rows:
        print(f"{f}:{line}  {msg}")
EOF
```

Matching by (file, message) is a heuristic. A new warning with the same message
in a different method of the same file can be counted as persisting, so check
the ambiguous ones against the source.

To trace a persisting baseline warning back through the pipeline, find its
slice (the folder whose `warning.txt` holds it) and check, in this order:
- Did Specimin produce the slice? (`warning.txt` exists)
- Did the slice reproduce the warning? (`root-warning.txt` exists)
- Did the LLM answer? (`null-inference-report.txt` exists, and the slice isn't
  in the run's "Failed" list)
- Was the annotation merged? (`merge.tsv`)

### Resetting

```bash
cd $JUNIT_DIR && git checkout -- src/      # undo the merge (step 2.6)
rm -rf $JUNIT_DIR/speciminoutllm           # Phase 2 slices; Phase 1's speciminout is untouched
```

---

## Reference results (JUnit 4, Qwen3.8-27B via vLLM)

These are the numbers from the authors' run, for comparison.

| Stage | Count |
|---|---|
| Baseline NullAway warnings (step 1.1) | 157 |
| Slice folders created by `RunSpeciminAll.py` | 153 |
| … Specimin succeeded (folder has `warning.txt`) | 149 |
| … original warning reproduced (`root-warning.txt`) | 140 |
| Slices answered by the LLM (after a second job with no token cap for 8 that hit the 16k limit) | 140 |
| NullAway warnings after merging the annotations (step 2.7) | 92 |
| … baseline warnings fixed | 138 |
| … baseline warnings still present | 19 (12 of them never reached the LLM: 8 unreproduced slices, 4 without a slice) |
| … new warnings caused by the annotations | 73 |

Most new warnings come from `@Nullable` annotations not being propagated. Each
slice is inferred for one warning in isolation, so a parameter or return value
made `@Nullable` flows into callees, fields, or callers that are still
non-null. Examples: `Assert.assertEquals(@Nullable String message, …)` passes
`message` on to a non-null `failNotEquals(String message, …)`, and
`getExpectedException()` becomes `@Nullable` while its callers dereference it.
Some baseline warnings can't be fixed with declaration annotations at all:
- local variables initialized to `null` inside `try` or loops;
- unboxing of `Map.get`;
- a null array passed to a varargs parameter;
- `return null` in an anonymous class (which `ApplyAnnotations` doesn't
  annotate).

---

## File reference

| File | Phase / step | Role |
|---|---|---|
| `SpeciminPerformanceEvaluation/GenerateNullAwayWarnings.sh` | 1.1, 2.7 | NullAway on the whole project (baseline / after); copies the dependency jars |
| `SpeciminPerformanceEvaluation/ExtractWarningMethods.py` | 1.2 | warnings → `warningMethods.jsonl` targets |
| `SpeciminPerformanceEvaluation/RunSpeciminAll.py` | 1.3 | one Specimin slice per warning (`warning.txt`, `root.txt`) |
| `SpeciminPerformanceEvaluation/RunCheckerAll.sh` | 1.4, 2.2, 2.5 | NullAway on each slice |
| `SpeciminPerformanceEvaluation/RunCFCheckerAll.sh` | 1.4 (optional) | Checker Framework Nullness Checker on each slice |
| `SpeciminPerformanceEvaluation/CompareSliceWarnings.py` | 1.5 | reproduction verdicts + `summary.txt` |
| `LLMInference/FixSpeciminNullInits.py` | 2.1 | remove Specimin's `= null` stub initializers |
| `LLMInference/ExtractRootWarning.py` | 2.3 | `root-warning.txt` for reproducing slices |
| `LLMInference/ExtractUsageContext.py` | 2.3b | `usage-context.txt`: declarations and uses of the member(s) behind each root warning, from the original source |
| `LLMInference/RunLLMInferenceAll.py` | 2.4 | prompts the LLM, writes reports and `*LLMInferenced/` |
| `LLMInference/llm_provider.py` | 2.4 | provider/model/key selection (Groq, NVIDIA, OpenAI-compatible, vLLM) |
| `LLMInference/AddNonnullImport.py` | 2.4 | adds missing `javax.annotation` imports (library for step 2.4; also a standalone script) |
| `LLMInference/DiagnoseGroq.py` | troubleshooting | checks key, model and endpoint for any provider |
| `LLMInference/hpc/run_llm_inference.sbatch`, `hpc/README.md` | 2.4 on HPC | vLLM + inference as one Slurm job (NJIT Wulver) |
| `ApplyAnnotations.java` + `./gradlew applyAnnotations` | 2.6 | merge inferred annotations into the original sources |
| `LLMInference/FixSpuriousNullable.py` | 2.7b | rewrites unjustified `@Nullable` behind dereference warnings to `@Nonnull` (optionally verified with NullAway) |
