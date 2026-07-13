# LLM Null-Inference Pipeline

This pipeline takes a Java project (EventBus), finds the locations NullAway is
unsure about, reduces each one to a minimal slice with **Specimin**, asks an LLM
to infer `@Nullable`/`@Nonnull`, and writes the inferred annotations back into the
original source.

To keep the LLM accurate without blowing up token cost, each slice is accompanied
by **usage context**: the exact lines elsewhere in the original program that
dereference, guard, or assign the slice's fields (see
[Usage context](#usage-context-token-efficient-evidence-for-the-llm)).

---

## Prerequisites

- **Java 17** (Specimin's build and NullAway/Error Prone both require it). The
  conda `(base)` env often pins Java 11 — make sure `java -version` says 17:
  ```bash
  export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # macOS
  java -version                                      # must print 17.x
  ```
- **Python 3.9+** and the Groq client: `pip install groq`
- **`GROQ_API_KEY`** exported in your shell (used by `RunLLMInferenceAll.py`).
- A **jar dependency directory** for Specimin's `--jarPath` (default
  `~/eventbus-deps`). For the EventBus *core* an empty directory is fine
  (`mkdir -p ~/eventbus-deps`) — it has no external dependencies.
- A local checkout of **Specimin** and **EventBus**.

### Paths / environment variables

Most scripts read these (all have defaults pointing at the author's machine, so
override them for your setup):

| Variable | Used by | Meaning |
|----------|---------|---------|
| `EVENTBUS_SRC_ROOT` | most | EventBus Java source root (e.g. `.../EventBus/EventBus/src`) |
| `WARNING_METHODS_FILE` | RunSpeciminAll | derived target list (default: next to the warnings file) |
| `SPECIMIN_DIR` | RunSpeciminAll | Specimin checkout (has `gradlew`) |
| `SPECIMIN_OUT` | RunSpeciminAll, RunNullAway, RunLLMInference | output dir for slices |
| `JAR_PATH` | RunSpeciminAll | Specimin `--jarPath` directory |
| `GROQ_API_KEY` | RunLLMInferenceAll | Groq API key |

> Note: `RunNullAwayAll.sh` and `RunLLMInferenceAll.py` currently hard-code a few
> paths (`SPECIMIN_OUT`, `EVENTBUS_DIR`) at the top of the file — edit those to
> match your machine, or align them with the env vars above.

---

## Execution

Run the stages in order. Stage 0 seeds the initial warnings; stages 1–8 are the
inference loop.

```bash
# 0. Generate the initial NullAway warnings on the ORIGINAL EventBus core.
#    Produces nullaway-warnings.txt next to your EventBus checkout.
EVENTBUS_DIR=/path/to/EventBus ./GenerateNullAwayWarnings.sh

# 1. Derive one Specimin target (enclosing method/constructor) per warning, via a
#    real JavaParser AST instead of regex/brace-scanning. Reads NullAway's and/or
#    the Index Checker's warnings file; pass whichever of the two flags apply.
./gradlew extractWarningMethods \
    -Psrc=$EVENTBUS_SRC_ROOT \
    -PnullawayWarnings=/path/to/nullaway-warnings.txt \
    -Poutput=/path/to/warningMethods.txt

# 2. Run Specimin (plain) on each target + extract usage context per slice.
python3 RunSpeciminAll.py                 # -> SPECIMIN_OUT/NN_name/ + usage-context.txt

# 3. Remove the spurious "= null" field initializers Specimin sometimes emits.
python3 FixSpeciminNullInits.py

# 4. Strip @NullUnmarked so NullAway actually checks the slices.
python3 RemoveNullUnmarked.py

# 5. Run NullAway on each slice -> per-slice nullaway-warnings.txt.
bash    RunNullAwayAll.sh

# 6. LLM inference: slice + per-slice warnings + usage-context.txt -> annotations.
python3 RunLLMInferenceAll.py             # writes <slice>LLMInferenced/ folders

# 7. Inject any missing javax.annotation imports.
python3 AddNonnullImport.py

# 8. Re-run NullAway to verify the inferred annotations.
bash    RunNullAwayAll.sh
```

Stages 1–8 are also wrapped by `RunPipeline.sh` (it assumes stage 0 already ran
and `nullaway-warnings.txt` exists).

Finally, to write the inferred annotations back into the **original** EventBus
source (the "post-analysis" step), from the Specimin directory:

```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 17)
./gradlew applyAnnotations \
    -PspeciminOut=$SPECIMIN_OUT \
    -PeventbusSrc=$EVENTBUS_SRC_ROOT
```

> Two `nullaway-warnings.txt` files exist and are easy to confuse: the
> **top-level** one (stage 0 output, input to stage 1) and the **per-slice** ones
> written *inside* each `SPECIMIN_OUT/NN_name/` folder by `RunNullAwayAll.sh`
> (read by the LLM step).

### What each script does

| Script | Role |
|--------|------|
| `GenerateNullAwayWarnings.sh` | Runs plain NullAway on the original EventBus core; writes `nullaway-warnings.txt`. |
| `extractWarningMethods` (Gradle task, `org.checkerframework.specimin.warningmethods.WarningMethodExtractor`) | AST-based (JavaParser): maps each NullAway/Index Checker warning line to its enclosing method/constructor (a Specimin target); a method flagged by both is only listed once. Replaces the old regex-based `ExtractWarningMethods.py`. |
| `RunSpeciminAll.py` | Runs Specimin per target; writes a minimal slice + `usage-context.txt`. |
| `ExtractUsageContext.py` | (called by the above) extracts field-usage snippets from the original source. |
| `FixSpeciminNullInits.py` | Removes spurious `= null` field stubs Specimin adds. |
| `RemoveNullUnmarked.py` | Removes `@NullUnmarked` so NullAway checks the slice. |
| `RunNullAwayAll.sh` | Runs NullAway on every slice; writes per-slice `nullaway-warnings.txt`. |
| `RunLLMInferenceAll.py` | Sends slice + warnings + usage-context to the LLM; writes `*LLMInferenced/`. |
| `AddNonnullImport.py` | Adds missing `javax.annotation.*` imports. |
| `ApplyAnnotations.java` (`./gradlew applyAnnotations`) | Writes inferred annotations back into the original source. |

---

## Usage context (token-efficient evidence for the LLM)

Specimin keeps the full body of every *target* member but stubs out all other
bodies. So the sites that constrain a field's type — its dereferences,
null-guards (`if (x != null)`), and assignments (`x = e`) in *other* methods —
are not in the slice. Without them the LLM infers the wrong type. This is not
nullability-specific: for any property-based type system, the evidence for a
declaration's type is its def/use sites.

Rather than bloat the (compilable) slice by pulling whole usage methods in — which
costs many tokens per LLM query — we keep the slice **minimal** and extract just
the relevant *lines* as read-only prompt context:

1. `RunSpeciminAll.py` runs Specimin plain (one minimal slice per target). After
   each slice it calls `ExtractUsageContext.py`, which scans the **slice** for
   every field it contains (e.g. a helper class's field like `FindState.clazz`,
   not just the target class's fields), finds those fields' usage lines in the
   original source, and writes them to `usage-context.txt` in the slice folder.
2. `RunLLMInferenceAll.py` reads `usage-context.txt` and splices it into the
   prompt under a clearly-labelled, *do-not-annotate* section.

Because the snippets are prompt context (not part of the compilable slice), a few
lines of evidence replace a 30-line preserved method — roughly a 10× token saving
on the added context — while giving the LLM *more* targeted evidence.

Controlled by env vars (read by `RunSpeciminAll.py`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `USAGE_CONTEXT` | `1` | Set to `0` to disable usage-context extraction. |
| `USAGE_CONTEXT_SCOPE` | `class` | `class` = each field's declaring file (precise); `repo` = whole source root (more recall, more noise). |
| `USAGE_CONTEXT_LINES` | `1` | Lines of surrounding context per usage. |

Inspect the context for one slice (or one target) without running the pipeline:

```bash
EVENTBUS_SRC_ROOT=/path/to/src python3 ExtractUsageContext.py --slice /path/to/specimin-out/01_Foo
EVENTBUS_SRC_ROOT=/path/to/src python3 ExtractUsageContext.py 'pkg.Class#method(T1, T2)'
```

### What this can and cannot do

Better evidence removes LLM *false positives* (e.g. over-marking a field
`@Nullable` because its dereferences were stubbed out) and generalizes to
non-nullability annotations. It **cannot** drive the original repo's warnings to
zero: warnings rooted in the code or in NullAway's modular analysis (a genuinely
nullable field dereferenced under a cross-method guard; lazy/static
initialization) persist regardless, because the correct annotation still triggers
them. The right success metric is inference accuracy, not a zero warning count.

> Note: an earlier experiment added whole usage methods to the slice — in
> `RunSpeciminAll.py`/`PreserveUsages.py` (Python) and as a native Specimin pass
> (`TargetUsageExpander` + `--preserveUsagesDepth`/`--preserveUsagesCap`). That
> approach has been reverted in the pipeline in favor of the token-efficient
> context above. The Specimin-side `TargetUsageExpander` and its flags remain in
> the codebase but are **unused by the pipeline** (the flags default to off); they
> can be removed if you want a clean tree.

---

## Troubleshooting

- **`UnsupportedClassVersionError: ... ErrorProneJavacPlugin ... class file version 61.0`**
  — you're building Specimin/NullAway with Java 11. Switch to Java 17 (see
  Prerequisites).
- **`ERROR: jar dependency directory not found`** — create `~/eventbus-deps`
  (empty is fine for EventBus core) or set `JAR_PATH`.
- **`GROQ_API_KEY environment variable is not set`** — export it before stage 6.
- **`usage-context.txt → (no field usages found)`** — the slice had no field whose
  usages were found under the current scope; try `USAGE_CONTEXT_SCOPE=repo`.