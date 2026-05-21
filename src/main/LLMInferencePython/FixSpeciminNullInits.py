#!/usr/bin/env python3
"""
FixSpeciminNullInits.py

Specimin stubs out fields it does not need by initializing them to null, e.g.:

    private final Logger logger = null;   ← Specimin artifact

When the original EventBus source declares the field without an initializer:

    private final Logger logger;          ← original

...the LLM sees `= null` and correctly (from its view) infers @Nullable.
That inference is wrong for the original, and causes spurious NullAway
dereference warnings after ApplyAnnotations transfers it back.

This script compares each reduced .java file against the original EventBus
source and removes the `= null` from any field where the original does not
have a null initializer.

Fields that legitimately have `= null` in the original are left untouched.

Insert this step AFTER RunSpeciminAll.py and BEFORE RemoveNullUnmarked.py.

Usage:
    python3 FixSpeciminNullInits.py            # patch in place
    python3 FixSpeciminNullInits.py --dry-run  # show changes only
"""
from __future__ import annotations

import re
import sys
import pathlib

# ── Paths ──────────────────────────────────────────────────────────────────────

SPECIMIN_OUT = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/specimin-out"
)

EVENTBUS_SRC = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src"
)

# ── Regex patterns ─────────────────────────────────────────────────────────────

# Matches a field declaration whose initializer is exactly `= null`.
# Works for all visibility modifiers and generic/array types.
# Groups:
#   1 → everything up to and including the type (annotations, modifiers, type)
#   2 → field name
#   3 → the `= null;` portion (with surrounding whitespace)
_FIELD_NULL_RE = re.compile(
    r"^"
    r"([ \t]*"                                       # leading indent
    r"(?:(?:@[\w.]+(?:\([^)]*\))?\s+)*)"             # optional annotations
    r"(?:(?:private|public|protected|static|final"
    r"|volatile|transient|synchronized)\s+)*"        # optional modifiers
    r"[\w$][\w$<>\[\].,? ]*\s+)"                     # type (including generics/arrays)
    r"(\w+)"                                         # field name
    r"([ \t]*=[ \t]*null[ \t]*;[ \t]*)"             # = null ;
    r"$",
    re.MULTILINE,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def has_null_init_in_original(field_name: str, original_src: str) -> bool:
    """Return True if `field_name = null ;` appears in the original source."""
    return bool(
        re.search(
            r"\b" + re.escape(field_name) + r"[ \t]*=[ \t]*null[ \t]*;",
            original_src,
        )
    )


def fix_null_inits(
    reduced_src: str, original_src: str
) -> tuple[str, list[str]]:
    """
    Remove `= null` from fields in reduced_src that have no null initializer
    in original_src.  Returns (patched_source, list_of_fixed_field_names).
    """
    fixed: list[str] = []

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        field_name = m.group(2)
        if not has_null_init_in_original(field_name, original_src):
            fixed.append(field_name)
            # Replace `Type fieldName = null;` → `Type fieldName;`
            return m.group(1) + field_name + ";"
        return m.group(0)  # legitimate null init — leave untouched

    patched = _FIELD_NULL_RE.sub(_replace, reduced_src)
    return patched, fixed


def find_original(
    reduced_file: pathlib.Path, specimin_folder: pathlib.Path
) -> pathlib.Path | None:
    """
    Resolve the original EventBus source file for a given reduced file.

    Reduced path:  <specimin_folder>/org/greenrobot/eventbus/Foo.java
    Original path: EVENTBUS_SRC/org/greenrobot/eventbus/Foo.java
    """
    try:
        rel = reduced_file.relative_to(specimin_folder)
        original = EVENTBUS_SRC / rel
        return original if original.exists() else None
    except ValueError:
        return None


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not SPECIMIN_OUT.exists():
        print(f"ERROR: SPECIMIN_OUT not found: {SPECIMIN_OUT}")
        sys.exit(1)
    if not EVENTBUS_SRC.exists():
        print(f"ERROR: EVENTBUS_SRC not found: {EVENTBUS_SRC}")
        sys.exit(1)

    # Only process non-LLMInferenced folders (the raw Specimin output)
    folders = sorted(
        d
        for d in SPECIMIN_OUT.iterdir()
        if d.is_dir() and not d.name.endswith("LLMInferenced")
    )

    if not folders:
        print("No specimin-out folders found.")
        sys.exit(0)

    if dry_run:
        print("(dry-run — no files will be modified)\n")

    total_files = 0
    total_fields = 0

    for folder in folders:
        print(f"── {folder.name}")
        for reduced_file in sorted(folder.rglob("*.java")):
            original_file = find_original(reduced_file, folder)
            if original_file is None:
                continue  # stub with no original counterpart (e.g. NullUnmarked.java)

            reduced_src = reduced_file.read_text(encoding="utf-8")
            original_src = original_file.read_text(encoding="utf-8")

            patched_src, fixed_fields = fix_null_inits(reduced_src, original_src)
            if not fixed_fields:
                continue

            rel = reduced_file.relative_to(folder)
            status = "   [dry-run]" if dry_run else "   [patched]"
            print(f"{status} {rel}")
            for field in fixed_fields:
                print(f"             · removed `= null` from: {field}")

            total_files += 1
            total_fields += len(fixed_fields)

            if not dry_run:
                reduced_file.write_text(patched_src, encoding="utf-8")

    print(f"\n{'═' * 50}")
    print(f"Files patched : {total_files}")
    print(f"Fields fixed  : {total_fields}")


if __name__ == "__main__":
    main()
