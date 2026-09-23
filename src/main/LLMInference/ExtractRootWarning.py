#!/usr/bin/env python3
"""
ExtractRootWarning.py

For every slice folder under SPECIMIN_OUT (skipping *LLMInferenced folders),
finds the ONE warning in the slice's nullaway-warnings.txt that reproduces
the original warning the slice was generated for (warning.txt), and writes
that single line, verbatim, to root-warning.txt in the same folder.

A slice's nullaway-warnings.txt can contain several warnings -- ones caused
by Specimin's stubbing, or other findings in code the slice happens to keep.
RunLLMInferenceAll.py gives the LLM only root-warning.txt, so it fixes just
the warning the slice was made for instead of being misled by the others.

Matching uses exactly the same rule as
SpeciminPerformanceEvaluation/CompareSliceWarnings.py (shared via its
find_reproduction()): same file name, same message (modulo embedded
"(line N)" references, plus the initializer-fields superset rule), and a
different line. So root-warning.txt exists exactly for the slices that
CompareSliceWarnings.py reports as REPRODUCED. For any other slice, no
root-warning.txt is written (a stale one from an earlier run is removed),
and RunLLMInferenceAll.py skips that slice.

Run this AFTER RunCheckerAll.sh (which writes each slice's
nullaway-warnings.txt) and BEFORE RunLLMInferenceAll.py.

Usage:
    python3 ExtractRootWarning.py            # write root-warning.txt files
    python3 ExtractRootWarning.py --dry-run  # only report what would be written

SPECIMIN_OUT can be overridden with the environment variable of the same name
(default: the JUnit 4 slices written by
SpeciminPerformanceEvaluation/RunSpeciminAll.py).
"""
from __future__ import annotations

import os
import sys
import pathlib

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "SpeciminPerformanceEvaluation")
)
from CompareSliceWarnings import (  # noqa: E402
    SLICE_WARNINGS_NAME,
    find_original_warning_file,
    find_reproduction,
    parse_findings,
)

SPECIMIN_OUT = pathlib.Path(os.environ.get(
    "SPECIMIN_OUT",
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/speciminout",
)).expanduser()

ROOT_WARNING_NAME = "root-warning.txt"


def extract_root_warning(folder: pathlib.Path) -> tuple[str | None, str]:
    """
    Returns (root_warning_line, note): the verbatim line from the slice's
    nullaway-warnings.txt that reproduces warning.txt's warning, or None
    with a note saying why there isn't one.
    """
    original_file = find_original_warning_file(folder)
    if original_file is None:
        return None, "no warning.txt"
    original_findings = parse_findings(original_file.read_text(encoding="utf-8"))
    if not original_findings:
        return None, f"{original_file.name} did not parse as a NullAway diagnostic"

    slice_warnings_file = folder / SLICE_WARNINGS_NAME
    if not slice_warnings_file.exists():
        return None, f"no {SLICE_WARNINGS_NAME} (run RunCheckerAll.sh first)"
    slice_findings = parse_findings(slice_warnings_file.read_text(encoding="utf-8"))
    if not slice_findings:
        return None, f"no NullAway warnings in {SLICE_WARNINGS_NAME}"

    match, same_line_near_miss = find_reproduction(original_findings[0], slice_findings)
    if match is not None:
        return match.raw, f"matched 1 of {len(slice_findings)} warning(s)"
    if same_line_near_miss is not None:
        return None, "not reproduced (same-line near-miss only)"
    return None, f"not reproduced (none of {len(slice_findings)} warning(s) match)"


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    print(f"SPECIMIN_OUT : {SPECIMIN_OUT}")
    if not SPECIMIN_OUT.exists():
        print(f"ERROR: SPECIMIN_OUT not found: {SPECIMIN_OUT}")
        sys.exit(1)
    if dry_run:
        print("(dry-run — no files will be written)")

    folders = sorted(
        d for d in SPECIMIN_OUT.iterdir()
        if d.is_dir() and not d.name.endswith("LLMInferenced")
    )
    if not folders:
        print("No slice folders found.")
        sys.exit(0)

    extracted, missing = [], []
    for folder in folders:
        root_warning, note = extract_root_warning(folder)
        out_file = folder / ROOT_WARNING_NAME
        print(f"\n── {folder.name}: {note}")
        if root_warning is None:
            missing.append((folder.name, note))
            if out_file.exists() and not dry_run:
                out_file.unlink()
                print(f"   removed stale {ROOT_WARNING_NAME}")
            continue
        extracted.append(folder.name)
        print(f"   {root_warning}")
        if not dry_run:
            out_file.write_text(root_warning + "\n", encoding="utf-8")

    print(f"\n{'═' * 60}")
    print(f"Summary: {len(folders)} slice folder(s)")
    print(f"  {ROOT_WARNING_NAME} {'would be ' if dry_run else ''}written : {len(extracted)}")
    print(f"  No reproduced warning (skipped by RunLLMInferenceAll.py) : {len(missing)}")
    for name, note in missing:
        print(f"    - {name}: {note}")


if __name__ == "__main__":
    main()
