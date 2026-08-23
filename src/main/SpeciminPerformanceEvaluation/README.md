# Specimin Performance Evaluation Pipeline

A two-stage variant of
[`LLMInferencePython`](../../LLMInferencePython/README_LLMInferencePipeline.md)'s
`ExtractWarningMethods.py` + `RunSpeciminAll.py` stages:

```bash
python3 ExtractWarningMethods.py   # nullaway-warnings.txt -> warningMethods.jsonl
python3 RunSpeciminAll.py          # -> SPECIMIN_OUT/NN_name/ + warning.txt
```

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
  02_post/            (a second warning inside the same method -> a second slice)
    ...
    warning.txt
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

## Known limitation (inherited from `LLMInferencePython`)

`EVENTBUS_SRC_ROOT`/`--root` is a single source root. EventBus warnings that
originate in a different module (e.g. `EventBusAnnotationProcessor`, which
has its own `src/` tree) will still be sliced against the core module's
root and can fail to resolve. This is the same limitation
`LLMInferencePython/RunSpeciminAll.py` has; fixing it (e.g. deriving the
root per-module from the warning's file path) is out of scope here.
