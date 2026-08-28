#!/usr/bin/env python3
"""
CompareSliceWarnings.py

For every slice folder under SPECIMIN_OUT, compares:
  - warning.txt              the exact NullAway warning the slice was
                              produced for (written by RunSpeciminAll.py)
  - nullaway-warnings.txt    the raw NullAway warnings found by running the
                              checker directly on that slice (written by
                              RunCheckerAll.sh)

and checks whether the original warning was REPRODUCED in the slice, using
this heuristic: an entry in nullaway-warnings.txt counts as a reproduction
of warning.txt's warning if and only if
  - the FILE NAME matches (basename only -- warning.txt's path is the
    original source's absolute path, nullaway-warnings.txt's paths are
    relative to the slice folder, so a full-path comparison would never
    match even for a correct reproduction), and
  - the ERROR MESSAGE matches, MODULO any "(line N)" references INSIDE the
    message text (the text after "[NullAway] "). Several NullAway message
    templates -- e.g. "initializer method does not guarantee @NonNull
    field X (line N) is initialized ..." -- embed the referenced field's
    OWN declaration line number in the message itself, not just the
    diagnostic's leading "<file>:<line>:". Specimin's slice is a
    renumbered copy of the original file, so that embedded number shifts
    exactly like the leading line number does; comparing it verbatim
    would reject a genuine reproduction as a message mismatch. Both
    messages have their "(line N)" occurrences normalized to "(line #)"
    before comparison, and
  - the LINE NUMBER (the diagnostic's own leading line, not any embedded
    in the message) is DIFFERENT -- Specimin's slice is a reduced,
    renumbered version of the original file, so a genuine reproduction is
    expected to land on a different line; a same-file, same-message,
    SAME-line "match" is treated as inconclusive, not a reproduction, and
    reported separately.

Writes ONE result file per slice folder, reproduction-check.txt, containing
the verdict and the evidence considered. Also prints a summary across all
slices.

Usage:
    python3 CompareSliceWarnings.py

Paths can be overridden via environment variables:
    SPECIMIN_OUT   slice folders to check (default: ~/EventBus/specimin-out)
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

# The exact warning a slice was produced for. RunSpeciminAll.py writes
# "warning.txt"; "warnings.txt" is accepted too in case a folder was
# renamed by hand.
ORIGINAL_WARNING_NAMES = ["warning.txt", "warnings.txt"]
SLICE_WARNINGS_NAME = "nullaway-warnings.txt"
RESULT_NAME = "reproduction-check.txt"

# Same diagnostic-location shape ExtractWarningMethods.py/RunCheckerAll.sh
# use: "<file>:<line>: warning: [NullAway] <message>". Captures the message
# separately so it can be compared independent of file/line.
_LOCATION_RE = re.compile(r'^(.+?):(\d+):\s*(?:error|warning):\s*\[([^\]]+)\]\s*(.*)$')

# Some NullAway message templates embed a referenced declaration's own line
# number in the message text, e.g. "...@NonNull field methodString (line 28)
# is initialized...". That number moves whenever the file is renumbered
# (exactly like the diagnostic's own leading line does), so it's masked out
# before comparing messages -- see normalize_message.
_LINE_REF_RE = re.compile(r'\(line \d+\)')


def normalize_message(message: str) -> str:
    return _LINE_REF_RE.sub('(line #)', message).strip()


class Finding:
    def __init__(self, raw: str, file: str, line: int, tag: str, message: str):
        self.raw = raw
        self.file = file
        self.line = line
        self.tag = tag
        self.message = message.strip()
        self.normalized_message = normalize_message(self.message)

    @property
    def basename(self) -> str:
        return pathlib.Path(self.file).name


def parse_findings(text: str) -> list:
    findings = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        m = _LOCATION_RE.match(raw)
        if not m:
            continue
        file, line, tag, message = m.groups()
        findings.append(Finding(raw, file, int(line), tag, message))
    return findings


def find_original_warning_file(slice_dir: pathlib.Path) -> pathlib.Path | None:
    for name in ORIGINAL_WARNING_NAMES:
        candidate = slice_dir / name
        if candidate.exists():
            return candidate
    return None


def check_slice(slice_dir: pathlib.Path) -> str:
    """
    Compares one slice folder's warning.txt against its nullaway-warnings.txt
    and returns the report text to write to reproduction-check.txt (and
    print). Never raises -- every failure mode is reported as text.
    """
    lines = [f"Slice: {slice_dir.name}"]

    original_file = find_original_warning_file(slice_dir)
    if original_file is None:
        lines.append(f"VERDICT: SKIPPED -- no {ORIGINAL_WARNING_NAMES[0]} found in this folder")
        return "\n".join(lines) + "\n"

    original_text = original_file.read_text(encoding="utf-8").strip()
    lines.append(f"Original warning ({original_file.name}):")
    lines.append(f"  {original_text}")

    original_findings = parse_findings(original_text)
    if not original_findings:
        lines.append(f"VERDICT: SKIPPED -- {original_file.name} did not parse as a NullAway diagnostic")
        return "\n".join(lines) + "\n"
    original = original_findings[0]

    slice_warnings_file = slice_dir / SLICE_WARNINGS_NAME
    if not slice_warnings_file.exists():
        lines.append(f"NOT REPRODUCED -- {SLICE_WARNINGS_NAME} not found (RunCheckerAll.sh not run yet?)")
        lines.append("VERDICT: NOT REPRODUCED")
        return "\n".join(lines) + "\n"

    slice_findings = parse_findings(slice_warnings_file.read_text(encoding="utf-8"))
    lines.append(f"\n{SLICE_WARNINGS_NAME}: {len(slice_findings)} finding(s)")
    for f in slice_findings:
        lines.append(f"  {f.raw}")

    if not slice_findings:
        lines.append("\nVERDICT: NOT REPRODUCED -- no NullAway warnings found in this slice")
        return "\n".join(lines) + "\n"

    match = None
    same_line_near_miss = None
    for f in slice_findings:
        if f.basename != original.basename or f.normalized_message != original.normalized_message:
            continue
        if f.line != original.line:
            match = f
            break
        same_line_near_miss = f  # same file+message, but same line too

    lines.append("")
    if match is not None:
        lines.append(f"Matched finding: {match.raw}")
        lines.append(f"  file name matches : {match.basename} == {original.basename}")
        if match.message == original.message:
            lines.append(f"  message matches   : yes (exact)")
        else:
            lines.append(f"  message matches   : yes, after normalizing embedded \"(line N)\" references")
            lines.append(f"    original message   : {original.message}")
            lines.append(f"    slice message       : {match.message}")
            lines.append(f"    normalized (both)   : {match.normalized_message}")
        lines.append(f"  line differs      : {original.line} -> {match.line}")
        lines.append("VERDICT: REPRODUCED")
    elif same_line_near_miss is not None:
        lines.append(f"Near-miss finding (same file + message, but SAME line -- not counted): {same_line_near_miss.raw}")
        lines.append("VERDICT: NOT REPRODUCED (same-line near-miss only)")
    else:
        lines.append("No finding in this slice matches the original warning's file name + message.")
        lines.append("VERDICT: NOT REPRODUCED")

    return "\n".join(lines) + "\n"


def main() -> None:
    print(f"SPECIMIN_OUT : {SPECIMIN_OUT}")

    if not SPECIMIN_OUT.exists():
        print(f"ERROR: SPECIMIN_OUT not found: {SPECIMIN_OUT}")
        sys.exit(1)

    folders = sorted(d for d in SPECIMIN_OUT.iterdir() if d.is_dir())
    if not folders:
        print("No specimin-out folders found.")
        sys.exit(0)

    reproduced, not_reproduced, skipped = [], [], []

    for folder in folders:
        report = check_slice(folder)
        (folder / RESULT_NAME).write_text(report, encoding="utf-8")

        verdict_line = next((l for l in report.splitlines() if l.startswith("VERDICT:")), "VERDICT: UNKNOWN")
        print(f"\n{'─' * 60}")
        print(f"{folder.name}: {verdict_line}")

        if "SKIPPED" in verdict_line:
            skipped.append(folder.name)
        elif "REPRODUCED" in verdict_line and "NOT REPRODUCED" not in verdict_line:
            reproduced.append(folder.name)
        else:
            not_reproduced.append(folder.name)

    total = len(reproduced) + len(not_reproduced) + len(skipped)
    print(f"\n{'═' * 60}")
    print(f"Summary: {total} slice folder(s) checked")
    print(f"  Reproduced     : {len(reproduced)}")
    print(f"  Not reproduced : {len(not_reproduced)}")
    print(f"  Skipped        : {len(skipped)}")
    if not_reproduced:
        print("\nNot reproduced:")
        for name in not_reproduced:
            print(f"  - {name}")
    if skipped:
        print("\nSkipped:")
        for name in skipped:
            print(f"  - {name}")
    print(f"\nPer-slice details written to each folder's {RESULT_NAME}")


if __name__ == "__main__":
    main()
