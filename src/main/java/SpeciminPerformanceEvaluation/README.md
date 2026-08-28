# Specimin Performance Evaluation Pipeline

A variant of four of
[`LLMInferencePython`](../../LLMInferencePython/README_LLMInferencePipeline.md)'s
stages -- `ExtractWarningMethods.py`, `RunSpeciminAll.py`,
`FixSpeciminNullInits.py`, `RunCheckerAll.sh` -- plus one script of its own,
`CompareSliceWarnings.py`:

```bash
python3 ExtractWarningMethods.py   # nullaway-warnings.txt -> warningMethods.jsonl
python3 RunSpeciminAll.py          # -> SPECIMIN_OUT/NN_name/ + warning.txt + root.txt
python3 FixSpeciminNullInits.py    # removes spurious `= null` field stubs Specimin added
./RunCheckerAll.sh                 # runs NullAway on each slice -> per-slice nullaway-warnings.txt
python3 CompareSliceWarnings.py    # did each slice reproduce the warning it was made for?
```

`LLMInferencePython`'s `RemoveNullUnmarked.py` step is intentionally not
included: it strips `@NullUnmarked` annotations so NullAway actually checks
a slice, but EventBus's source has zero uses of `@NullUnmarked` anywhere
(unlike gson, which this pipeline was adapted from), so for EventBus that
step is a guaranteed no-op.

It differs from `LLMInferencePython` in these ways:

1. **No deduplication by target method.** `ExtractWarningMethods.py` writes
   one entry per warning line in `nullaway-warnings.txt`, in file order. If
   two warnings are reported inside the same method, that method is written
   (and later sliced) twice — once per warning — instead of being collapsed
   into a single entry/slice.
2. **Each slice keeps a copy of the warning it was produced for.**
   `RunSpeciminAll.py` writes the exact originating warning line into
   `warning.txt` inside that slice's `SPECIMIN_OUT/NN_name/` folder right
   after Specimin succeeds.
3. **Field-level warnings are targeted too.** `LLMInferencePython`'s
   extractor only ever emits `--targetMethod` targets and silently skips a
   warning on a bare field declaration (e.g. "@NonNull field X not
   initialized"). This pipeline detects those and emits a `--targetField`
   target (`pkg.Class#fieldName`) instead, so they get sliced as well. The
   only warnings still skipped are ones that fall inside an anonymous
   `static { ... }` / instance initializer block — Specimin unconditionally
   removes those blocks (`PrunerVisitor#visit(InitializerDeclaration)`)
   regardless of what's targeted, so there is no target that keeps such a
   warning reproducible in a slice. The `[SKIP]` log line says so explicitly.

Everything else about how a target is derived from a warning location, and
how Specimin is invoked, is the same as `LLMInferencePython`'s versions.
Only NullAway's `nullaway-warnings.txt` is read (no Index Checker log), and
usage-context extraction is not included — this is a minimal
extract-and-slice pipeline, not the full 8-stage inference pipeline.

## Output format

`ExtractWarningMethods.py` writes `warningMethods.jsonl`, one JSON object per
warning (duplicates kept), e.g.:

```json
{"target": "org.greenrobot.eventbus.EventBus#post(Object)", "kind": "method", "warning": "/path/EventBus.java:204: warning: [NullAway] passing @Nullable parameter 'stickyEvent' where @NonNull is required", "file": "/path/EventBus.java", "line": 204}
```

or, for a bare field declaration (`"kind": "field"`, no parens on the target):

```json
{"target": "org.greenrobot.eventbus.EventBus#defaultInstance", "kind": "field", "warning": "/path/EventBus.java:46: warning: [NullAway] @NonNull static field defaultInstance not initialized", "file": "/path/EventBus.java", "line": 46}
```

`RunSpeciminAll.py` then produces, per entry:

```
SPECIMIN_OUT/
  01_post/
    ...slice files...
    warning.txt      <- the exact warning line above
    root.txt          <- the --root this slice was generated against
  02_post/            (a second warning inside the same method -> a second slice)
    ...
    warning.txt
    root.txt
```

After `FixSpeciminNullInits.py` and `RunCheckerAll.sh`, each slice folder
additionally has:

```
  01_post/
    nullaway-report.txt     <- full Gradle build log for this slice alone
    nullaway-warnings.txt   <- just this slice's [NullAway] findings
```

## Environment variables

Same names as `LLMInferencePython`, with defaults pointing at this
checkout's sibling `EventBus` clone:

| Variable | Meaning | Default |
|----------|---------|---------|
| `NULLAWAY_WARNINGS_FILE` | Input NullAway warnings file | `~/EventBus/nullaway-warnings.txt` |
| `WARNING_METHODS_FILE` | Extractor output | next to `NULLAWAY_WARNINGS_FILE`, `warningMethods.jsonl` |
| `EVENTBUS_SRC_ROOT` | Java source root passed to Specimin's `--root` | `~/EventBus/EventBus/src` |
| `SPECIMIN_DIR` | Specimin checkout (has `gradlew`) | `~/specimin` |
| `SPECIMIN_OUT` | Output dir for slices | next to `NULLAWAY_WARNINGS_FILE`, `specimin-out` |
| `JAR_PATH` | Specimin `--jarPath` directory | `~/eventbus-deps` |

Generate `nullaway-warnings.txt` first with `EventBus/run-nullaway.sh` (see
`EventBus/NULLAWAY.md`).

`RunSpeciminAll.py --dry-run` prints the derived target list and the exact
Specimin command for each entry without invoking Specimin or writing
`warning.txt` — useful for sanity-checking the extraction before running the
real (network- and JDK-dependent) Specimin build.

## FixSpeciminNullInits.py

Specimin stubs out fields it doesn't need with `= null` (e.g. `private final
Logger logger = null;`), even when the original field has no initializer.
Left in place, that spurious `= null` can itself trigger a NullAway warning
when `RunCheckerAll.sh` checks the slice — one that has nothing to do with
the warning the slice was produced for. This script compares each slice's
`.java` files against their original EventBus source and removes `= null`
from any field the original doesn't actually null-initialize; a field that
legitimately has `= null` in the original is left untouched.

It resolves each slice's original source using that slice's own `root.txt`
(not one global root), for the same multi-module reason `RunSpeciminAll.py`
derives `--root` per target — see "Multi-module roots" below.

```bash
python3 FixSpeciminNullInits.py             # patch in place
python3 FixSpeciminNullInits.py --dry-run   # show changes only, don't modify files
python3 FixSpeciminNullInits.py --verbose   # log every file considered, not just patched ones
```

Env vars: `SPECIMIN_OUT` (default `~/EventBus/specimin-out`) and
`EVENTBUS_SRC_ROOT` (default `~/EventBus/EventBus/src`, used only as a
fallback for a slice folder missing `root.txt`).

## RunCheckerAll.sh

Runs NullAway directly on each slice folder under `SPECIMIN_OUT`: injects a
throwaway `settings.gradle` + `build.gradle` into the slice (source set =
the slice itself), copies the Gradle wrapper from `SPECIMIN_DIR`, and runs
`./gradlew clean compileJava` with NullAway enabled via Error Prone —
exactly like `EventBus/gradle/nullaway.gradle`'s own configuration
(`org.greenrobot.eventbus`, Error Prone 2.18.0, NullAway 0.10.10, `WARN`
severity), so the same finding is reproduced against the slice, not
suppressed by a different config. Writes `nullaway-report.txt` (full build
log) and `nullaway-warnings.txt` (just the `[NullAway]` lines, in the exact
same format `run-nullaway.sh` produces for the whole project) inside that
slice folder.

Unlike `LLMInferencePython/RunCheckerAll.sh`, this version only runs
NullAway — no Index Checker, no `CF_HOME` dependency.

```bash
export JAVA_HOME=$(/usr/libexec/java_home -v 17)   # see Prerequisites below
./RunCheckerAll.sh
```

Env vars: `SPECIMIN_OUT`, `SPECIMIN_DIR`, `JAR_PATH` (same defaults as
`RunSpeciminAll.py`), plus `ANNOTATED_PACKAGES` (default
`org.greenrobot.eventbus`), `NULLAWAY_SEVERITY` (default `WARN`),
`ERRORPRONE_VERSION` (default `2.18.0`), `NULLAWAY_VERSION` (default
`0.10.10`), `GRADLE_DIST_VERSION` (default `8.7`).

## CompareSliceWarnings.py

Checks whether each slice actually reproduces the warning it was made for,
by comparing that slice's `warning.txt` (the original warning) against its
`nullaway-warnings.txt` (what NullAway found when `RunCheckerAll.sh` ran
directly on the slice). An entry in `nullaway-warnings.txt` counts as a
reproduction of `warning.txt`'s warning only if:

- the **file name** matches (basename only — `warning.txt` holds the
  original source's absolute path, `nullaway-warnings.txt`'s paths are
  relative to the slice folder, so a full-path comparison would never match
  even for a correct reproduction), and
- the **error message** matches (the text after `[NullAway] `), and
- the **line number differs** — Specimin's slice is a reduced, renumbered
  copy of the original file, so a genuine reproduction is expected to land
  on a different line. A same-file, same-message match on the exact same
  line is treated as an inconclusive near-miss, not a reproduction, and
  reported as such rather than silently counted either way.

```bash
python3 CompareSliceWarnings.py
```

Writes one `reproduction-check.txt` per slice folder (the original warning,
every finding in that slice's `nullaway-warnings.txt`, which one matched if
any, and the verdict), and prints a REPRODUCED / NOT REPRODUCED / SKIPPED
summary across all slices. A slice is SKIPPED (not counted as reproduced or
not) only when its `warning.txt` is missing or unparseable; a missing or
empty `nullaway-warnings.txt` (e.g. `RunCheckerAll.sh` hasn't been run yet,
or the slice's build failed) counts as NOT REPRODUCED, with the reason
noted in that slice's `reproduction-check.txt`.

Env var: `SPECIMIN_OUT` (same default as the rest of the pipeline).

## Prerequisites

Same as the rest of this pipeline (and `LLMInferencePython`): **JDK 17+**
active (`export JAVA_HOME=$(/usr/libexec/java_home -v 17)`; a conda
`(base)` shell often shadows this with JDK 11, which fails Specimin's own
`spotlessJava` build step on Java 16+ source syntax like pattern-matching
`instanceof`) and network access to Maven Central for Gradle to resolve
Error Prone / NullAway.

## Multi-module roots

Unlike `LLMInferencePython` (which always passes one fixed
`EVENTBUS_SRC_ROOT`), this pipeline derives the source root **per target**
instead of using one global root, because EventBus warnings can come from
more than one module's separate `src/` tree (e.g. the core `EventBus`
module vs. `EventBusAnnotationProcessor`):

- `RunSpeciminAll.py` derives `--root` from the warning's own absolute file
  path: it strips the target's package-relative path off the end of that
  absolute path, leaving whatever "src" directory the file actually lives
  under, then records it as `root.txt` in the slice folder. Without this, a
  warning from a module other than the one `EVENTBUS_SRC_ROOT` points at
  fails with "Specimin could not find the file for the target class".
- `FixSpeciminNullInits.py` reads each slice's `root.txt` to resolve that
  slice's original source, instead of assuming one global root too.

`EVENTBUS_SRC_ROOT` is kept only as a fallback for the rare case a target's
root can't be derived this way (e.g. `RunSpeciminAll.py`'s derivation fails,
or a slice folder predates `root.txt` being written). Each
`RunSpeciminAll.py` log line prints the `root` it derived for that target,
so a fallback is visible immediately.
