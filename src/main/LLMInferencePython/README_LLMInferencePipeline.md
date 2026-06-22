# LLM Null-Inference Pipeline

Run the stages in order (or use `RunPipeline.sh`):

```
python3 ExtractWarningMethods.py     # NullAway warnings -> warningMethods.txt
python3 RunSpeciminAll.py            # slice each target (with usage preservation)
python3 FixSpeciminNullInits.py
python3 RemoveNullUnmarked.py
bash    RunNullAwayAll.sh
python3 RunLLMInferenceAll.py
python3 AddNonnullImport.py
bash    RunNullAwayAll.sh
cd specimin
./gradlew applyAnnotations
```

## Usage preservation (PreserveUsages.py)

Specimin keeps the full body of every *target* member but stubs out all other
bodies. So a field `x` declared in the target class but dereferenced
(`x.y = 0;`) inside a non-target method loses that dereference in the slice,
and the LLM then wrongly infers `x` as `@Nullable`.

`RunSpeciminAll.py` now adds an extra layer: for each run it finds the members
that dereference the fields the target touches and passes them as additional
`--targetMethod` / `--targetField` values in the **same** Specimin invocation,
so the usage sites survive and everything lands in one combined output slice.

Environment variables:

| Variable | Default | Meaning |
|----------|---------|---------|
| `PRESERVE_USAGES` | `1` | Set to `0` to disable the layer entirely. |
| `PRESERVE_USAGES_SCOPE` | `class` | `class` = scan only the target's file (high precision); `repo` = scan every source file under the root (broader, lower precision). |
| `PRESERVE_USAGES_FIELDS` | `member` | `member` = preserve usages of only the fields the target member touches; `class` = every field of the target class. |

Inspect what would be added for a single target, without running Specimin:

```
EVENTBUS_SRC_ROOT=/path/to/src \
    python3 PreserveUsages.py 'pkg.Class#method(Type1, Type2)' [--scope class|repo] [--fields member|class]
```

### Precision note

References are located with the same lightweight brace/regex Java scanning used
elsewhere in this pipeline, not a full symbol solver. Only unqualified (`x.`)
and `this.x.` dereferences are matched; `other.x.` is skipped. For exact,
solver-based resolution the same algorithm can be moved into Specimin itself,
reusing `JavaParserUtil` and the qualified-signature format that the
member-target strings produced here already follow.
