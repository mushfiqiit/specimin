#!/usr/bin/env python3
"""
AddNonnullImport.py

Walks every .java file under SPECIMIN_OUT and injects any missing
javax.annotation.Nonnull / javax.annotation.Nullable imports.

Usage:
    python3 AddNonnullImport.py            # patch all files in place
    python3 AddNonnullImport.py --dry-run  # show what would change
"""
from __future__ import annotations

import sys
import pathlib

SPECIMIN_OUT = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out"
)

NONNULL_IMPORT = "import javax.annotation.Nonnull;"
NULLABLE_IMPORT = "import javax.annotation.Nullable;"


def ensure_imports(source: str) -> str:
    needed = []
    if "@Nonnull" in source and NONNULL_IMPORT not in source:
        needed.append(NONNULL_IMPORT)
    if "@Nullable" in source and NULLABLE_IMPORT not in source:
        needed.append(NULLABLE_IMPORT)
    if not needed:
        return source

    lines = source.splitlines(keepends=True)
    insert_at = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("package "):
            insert_at = i + 1
            break
        if stripped.startswith("import ") or (stripped and not stripped.startswith("/") and not stripped.startswith("*")):
            insert_at = i
            break

    injection = "".join(imp + "\n" for imp in needed)
    lines.insert(insert_at, injection)
    return "".join(lines)


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    if not SPECIMIN_OUT.exists():
        print(f"ERROR: directory not found: {SPECIMIN_OUT}")
        sys.exit(1)

    patched, skipped = 0, 0
    for java_file in sorted(SPECIMIN_OUT.rglob("*.java")):
        original = java_file.read_text(encoding="utf-8")
        updated = ensure_imports(original)
        if updated == original:
            skipped += 1
            continue
        patched += 1
        print(f"{'[dry-run] would patch' if dry_run else '[patched]'} {java_file.relative_to(SPECIMIN_OUT)}")
        if not dry_run:
            java_file.write_text(updated, encoding="utf-8")

    print(f"\nDone. {patched} file(s) patched, {skipped} already correct.")


if __name__ == "__main__":
    main()
