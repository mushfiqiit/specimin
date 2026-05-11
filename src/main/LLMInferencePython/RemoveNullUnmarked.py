#!/usr/bin/env python3
"""
RemoveNullUnmarked.py

Recursively removes all @NullUnmarked annotations from every .java file
under SPECIMIN_OUT, including the corresponding import statement.

Handles all placement styles:
  - @NullUnmarked on its own line          → entire line removed
  - @NullUnmarked combined with @Override  → only the token removed
  - @NullUnmarked inline with a method sig → only the token removed

Usage:
    python3 RemoveNullUnmarked.py            # patch all files in place
    python3 RemoveNullUnmarked.py --dry-run  # show what would change
"""
from __future__ import annotations

import re
import sys
import pathlib

SPECIMIN_OUT = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out"
)

# Matches the import line for NullUnmarked (any package)
_IMPORT_RE = re.compile(r"^[ \t]*import\s+\S+\.NullUnmarked\s*;\s*\n?", re.MULTILINE)

# Matches @NullUnmarked with optional trailing whitespace
_ANNOTATION_RE = re.compile(r"@NullUnmarked\s*")


def remove_nullunmarked(source: str) -> str:
    # Remove the import line entirely
    result = _IMPORT_RE.sub("", source)

    # Process line-by-line so we can drop lines that become blank
    lines = result.splitlines(keepends=True)
    out = []
    for line in lines:
        stripped = line.strip()
        if stripped == "@NullUnmarked" or stripped == "@NullUnmarked;":
            # Standalone annotation line — drop it
            continue
        if "@NullUnmarked" in line:
            # Inline — remove just the token, collapse extra spaces
            line = _ANNOTATION_RE.sub("", line)
            # Normalise multiple spaces that may result (e.g. "public  void" → "public void")
            line = re.sub(r"([\w>)\]]) {2,}", r"\1 ", line)
        out.append(line)
    return "".join(out)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not SPECIMIN_OUT.exists():
        print(f"ERROR: directory not found: {SPECIMIN_OUT}")
        sys.exit(1)

    patched, skipped = 0, 0
    for java_file in sorted(SPECIMIN_OUT.rglob("*.java")):
        original = java_file.read_text(encoding="utf-8")
        updated = remove_nullunmarked(original)
        if updated == original:
            skipped += 1
            continue
        patched += 1
        print(f"{'[dry-run] would patch' if dry_run else '[patched]'} "
              f"{java_file.relative_to(SPECIMIN_OUT)}")
        if not dry_run:
            java_file.write_text(updated, encoding="utf-8")

    print(f"\nDone. {patched} file(s) patched, {skipped} unchanged.")


if __name__ == "__main__":
    main()
