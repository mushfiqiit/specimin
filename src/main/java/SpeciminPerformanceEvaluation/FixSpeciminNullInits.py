#!/usr/bin/env python3
"""
FixSpeciminNullInits.py

Specimin stubs out fields it does not need by initializing them to null, e.g.:

    private final Logger logger = null;   <- Specimin artifact

When the original EventBus source declares the field without an initializer:

    private final Logger logger;          <- original

...a downstream tool (e.g. an LLM inference step) that reads the slice sees
`= null` and would incorrectly infer @Nullable for it. That inference is
wrong for the original, and would cause spurious NullAway dereference
warnings if ever transferred back. It also pollutes the NullAway run this
folder's RunCheckerAll.sh does directly on the slice: a field that Specimin
nulled out but the original never initializes to null can itself trigger
warnings that have nothing to do with the warning the slice was produced
for.

This script compares each reduced .java file against the ORIGINAL EventBus
source it was sliced from and removes the `= null` from any field where the
original does not have a null initializer. Fields that legitimately have
`= null` in the original are left untouched.

Unlike LLMInferencePython/FixSpeciminNullInits.py -- which resolves each
slice's original file against one fixed --eventbus-src root -- this script
resolves the original per SLICE FOLDER, using that folder's root.txt
(written by RunSpeciminAll.py in this folder), because slices in this
pipeline can come from different module source trees (e.g. EventBus's core
module vs. EventBusAnnotationProcessor, which are two separate "src"
directories). EVENTBUS_SRC_ROOT is only a fallback for a slice folder
missing root.txt (e.g. one produced before this pipeline started writing it).

Run this AFTER RunSpeciminAll.py, on the same SPECIMIN_OUT.

Usage:
    python3 FixSpeciminNullInits.py                # patch in place
    python3 FixSpeciminNullInits.py --dry-run      # show changes only
    python3 FixSpeciminNullInits.py --verbose      # show all files processed

Paths can be overridden via environment variables:
    SPECIMIN_OUT        slice folders to patch (default: ~/EventBus/specimin-out)
    EVENTBUS_SRC_ROOT   fallback source root for a slice missing root.txt
                        (default: ~/EventBus/EventBus/src)
"""
from __future__ import annotations

import os
import re
import sys
import pathlib

# ── Paths ──────────────────────────────────────────────────────────────────────
def _path(env_name: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(env_name, default)).expanduser()


SPECIMIN_OUT = _path("SPECIMIN_OUT", "~/EventBus/specimin-out")
EVENTBUS_SRC_ROOT = _path("EVENTBUS_SRC_ROOT", "~/EventBus/EventBus/src")

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


def fix_null_inits(reduced_src: str, original_src: str) -> tuple[str, list[str]]:
    """
    Remove `= null` from fields in reduced_src that have no null initializer
    in original_src. Returns (patched_source, list_of_fixed_field_names).
    """
    fixed: list[str] = []

    def _replace(m: re.Match) -> str:  # type: ignore[type-arg]
        prefix = m.group(1)
        field_name = m.group(2)
        # Skip lines that look like method signatures or local variable
        # assignments (they contain parentheses in the prefix).
        if "(" in prefix or ")" in prefix:
            return m.group(0)
        if not has_null_init_in_original(field_name, original_src):
            fixed.append(field_name)
            # Replace `Type fieldName = null;` -> `Type fieldName;`
            return prefix + field_name + ";"
        return m.group(0)  # legitimate null init -- leave untouched

    patched = _FIELD_NULL_RE.sub(_replace, reduced_src)
    return patched, fixed


def slice_root(slice_folder: pathlib.Path, verbose: bool = False) -> pathlib.Path:
    """
    Return the source root this slice folder was generated against: read
    from root.txt (written by RunSpeciminAll.py) if present, else fall back
    to EVENTBUS_SRC_ROOT.
    """
    root_file = slice_folder / "root.txt"
    if root_file.exists():
        return pathlib.Path(root_file.read_text(encoding="utf-8").strip())
    if verbose:
        print(f"   [no root.txt] {slice_folder.name} -- falling back to EVENTBUS_SRC_ROOT")
    return EVENTBUS_SRC_ROOT


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


# ── Main ───────────────────────────────────────────────────────────────────────


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    verbose = "--verbose" in sys.argv

    print(f"SPECIMIN_OUT      : {SPECIMIN_OUT}")
    print(f"EVENTBUS_SRC_ROOT : {EVENTBUS_SRC_ROOT}  (fallback only, see root.txt per slice)")

    if not SPECIMIN_OUT.exists():
        print(f"ERROR: SPECIMIN_OUT not found: {SPECIMIN_OUT}")
        sys.exit(1)

    if dry_run:
        print("(dry-run -- no files will be modified)")
    print()

    folders = sorted(d for d in SPECIMIN_OUT.iterdir() if d.is_dir())
    if not folders:
        print("No specimin-out folders found.")
        sys.exit(0)

    total_files = 0
    total_fields = 0

    for folder in folders:
        eventbus_src = slice_root(folder, verbose=verbose)
        if not eventbus_src.exists():
            print(f"── {folder.name}  [SKIP] source root not found: {eventbus_src}")
            continue

        print(f"── {folder.name}  (root: {eventbus_src})")
        java_files = sorted(folder.rglob("*.java"))
        if verbose:
            print(f"   ({len(java_files)} .java files in this folder)")

        for reduced_file in java_files:
            original_file = find_original(reduced_file, folder, eventbus_src, verbose=verbose)
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
