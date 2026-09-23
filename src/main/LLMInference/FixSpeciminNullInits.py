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
    python3 FixSpeciminNullInits.py                          # patch in place
    python3 FixSpeciminNullInits.py --dry-run                # show changes only
    python3 FixSpeciminNullInits.py --verbose                # show all files processed
    python3 FixSpeciminNullInits.py \\
        --specimin-out /path/to/specimin-out \\
        --eventbus-src /path/to/EventBus/src
"""
from __future__ import annotations

import re
import sys
import pathlib

# ── Default paths ──────────────────────────────────────────────────────────────

DEFAULT_SPECIMIN_OUT = "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out"
DEFAULT_EVENTBUS_SRC = (
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src"
)

# ── Regex ──────────────────────────────────────────────────────────────────────

# Matches any line of the form:
#   <indent><anything-without-paren><whitespace><fieldName> = null;
# The `(` / `)` filter below excludes method-body assignments and method sigs.
_FIELD_NULL_RE = re.compile(
    r"^([ \t]*\S[^\n(=]*\s)(\w+)([ \t]*=[ \t]*null[ \t]*;[^\n]*)$",
    re.MULTILINE,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def has_null_init_in_original(field_name: str, original_src: str) -> bool:
    """Return True if `field_name = null;` appears in the original source."""
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
        prefix = m.group(1)
        field_name = m.group(2)
        # Skip lines that look like method signatures or local variable assignments
        # (they contain parentheses in the prefix/surrounding context)
        if "(" in prefix or ")" in prefix:
            return m.group(0)
        if not has_null_init_in_original(field_name, original_src):
            fixed.append(field_name)
            # Replace `Type fieldName = null;` → `Type fieldName;`
            return prefix + field_name + ";"
        return m.group(0)  # legitimate null init — leave untouched

    patched = _FIELD_NULL_RE.sub(_replace, reduced_src)
    return patched, fixed


def find_original(
    reduced_file: pathlib.Path,
    specimin_folder: pathlib.Path,
    eventbus_src: pathlib.Path,
    verbose: bool = False,
) -> pathlib.Path | None:
    """
    Resolve the original EventBus source file for a given reduced file.

    Reduced path:  <specimin_folder>/org/greenrobot/eventbus/Foo.java
    Original path: eventbus_src/org/greenrobot/eventbus/Foo.java
    """
    try:
        rel = reduced_file.relative_to(specimin_folder)
        original = eventbus_src / rel
        if original.exists():
            return original
        if verbose:
            print(f"      [skip-no-original] {rel}  (looked for: {original})")
        return None
    except ValueError as e:
        if verbose:
            print(f"      [skip-relative-err] {reduced_file}  ({e})")
        return None


# ── Argument parsing ───────────────────────────────────────────────────────────


def parse_args() -> tuple[pathlib.Path, pathlib.Path, bool, bool]:
    """Return (specimin_out, eventbus_src, dry_run, verbose)."""
    args = sys.argv[1:]
    specimin_out = pathlib.Path(DEFAULT_SPECIMIN_OUT)
    eventbus_src = pathlib.Path(DEFAULT_EVENTBUS_SRC)
    dry_run = False
    verbose = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            dry_run = True
        elif a == "--verbose":
            verbose = True
        elif a == "--specimin-out" and i + 1 < len(args):
            specimin_out = pathlib.Path(args[i + 1])
            i += 1
        elif a == "--eventbus-src" and i + 1 < len(args):
            eventbus_src = pathlib.Path(args[i + 1])
            i += 1
        i += 1

    return specimin_out, eventbus_src, dry_run, verbose


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    specimin_out, eventbus_src, dry_run, verbose = parse_args()

    print(f"SPECIMIN_OUT : {specimin_out}")
    print(f"EVENTBUS_SRC : {eventbus_src}")

    if not specimin_out.exists():
        print(f"ERROR: SPECIMIN_OUT not found: {specimin_out}")
        sys.exit(1)
    if not eventbus_src.exists():
        print(f"ERROR: EVENTBUS_SRC not found: {eventbus_src}")
        sys.exit(1)

    if dry_run:
        print("(dry-run — no files will be modified)\n")
    print()

    # Only process non-LLMInferenced folders (the raw Specimin output)
    folders = sorted(
        d
        for d in specimin_out.iterdir()
        if d.is_dir() and not d.name.endswith("LLMInferenced")
    )

    if not folders:
        print("No specimin-out folders found.")
        sys.exit(0)

    total_files = 0
    total_fields = 0

    for folder in folders:
        print(f"── {folder.name}")
        java_files = sorted(folder.rglob("*.java"))
        if verbose:
            print(f"   ({len(java_files)} .java files in this folder)")

        for reduced_file in java_files:
            original_file = find_original(
                reduced_file, folder, eventbus_src, verbose=verbose
            )
            if original_file is None:
                continue  # stub with no original counterpart

            if verbose:
                rel_display = reduced_file.relative_to(folder)
                print(f"   [checking] {rel_display}")

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
