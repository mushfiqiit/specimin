#!/usr/bin/env python3
"""
RunSpeciminAll.py

Reads warningMethods.jsonl (produced by ExtractWarningMethods.py in this same
folder) and runs Specimin once per WARNING (not once per unique method) to
produce a reduced program per entry under SPECIMIN_OUT. After each successful
Specimin run, the exact NullAway warning line the slice was produced for is
written into that slice's folder as warning.txt.

Each entry carries a "kind" of either "method" (sliced with Specimin's
--targetMethod) or "field" (a bare field declaration, sliced with
--targetField) -- see ExtractWarningMethods.py.

This mirrors LLMInferencePython/RunSpeciminAll.py's Specimin-invocation logic,
with two differences:
  1. warningMethods.jsonl is not deduplicated by target, so a method/field
     flagged by two different warnings gets two separate slice folders here
     (LLMInferencePython's version collapses them into a single slice).
  2. Each slice folder gets a warning.txt holding the exact warning line the
     slice was generated for (LLMInferencePython's version does not keep this).

Each line of warningMethods.jsonl looks like:
    {"target": "org.greenrobot.eventbus.EventBus#post(Object)", "kind": "method",
     "warning": "/path/EventBus.java:204: warning: [NullAway] ...",
     "file": "/path/EventBus.java", "line": 204}

Usage:
    python3 RunSpeciminAll.py            # run all
    python3 RunSpeciminAll.py --dry-run  # only derive + print targets, don't run Specimin

Paths below can be overridden with environment variables of the same name.
"""
from __future__ import annotations

import os
import re
import sys
import json
import shlex
import pathlib
import subprocess

# ── Paths ──────────────────────────────────────────────────────────────────────
def _path(env_name: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(env_name, default)).expanduser()


NULLAWAY_WARNINGS_FILE = _path(
    "NULLAWAY_WARNINGS_FILE",
    "~/EventBus/nullaway-warnings.txt",
)
WARNING_METHODS_FILE = _path(
    "WARNING_METHODS_FILE",
    str(NULLAWAY_WARNINGS_FILE.parent / "warningMethods.jsonl"),
)
# Java source root of the project being sliced (EventBus's core module).
EVENTBUS_SRC_ROOT = _path(
    "EVENTBUS_SRC_ROOT",
    "~/EventBus/EventBus/src",
)
SPECIMIN_DIR = _path(
    "SPECIMIN_DIR",
    "~/specimin",
)
SPECIMIN_OUT = _path(
    "SPECIMIN_OUT",
    str(NULLAWAY_WARNINGS_FILE.parent / "specimin-out"),
)
# EventBus's core module has no external compile-time dependencies, so an
# empty directory is fine here -- it still needs to exist.
JAR_PATH = _path("JAR_PATH", "~/eventbus-deps")
GRADLEW  = SPECIMIN_DIR / "gradlew"


# ── Location parsing ───────────────────────────────────────────────────────────

def fqcn_to_rel_file(fqcn: str) -> pathlib.Path:
    """
    Convert a fully-qualified class name to its relative .java file path.

    The first dot-separated token that starts with an uppercase letter is the
    outer class (Java convention); everything before it is the package path.
    Nested classes (e.g. SubscriberMethodFinder.FindState) share the outer
    class file.

    Example:
        org.greenrobot.eventbus.EventBus            -> org/greenrobot/eventbus/EventBus.java
        org.greenrobot.eventbus.SubscriberMethodFinder.FindState
                                                     -> org/greenrobot/eventbus/SubscriberMethodFinder.java
    """
    parts = fqcn.split('.')
    for i, part in enumerate(parts):
        if part and part[0].isupper():
            pkg_path = '/'.join(parts[:i])
            outer_class = parts[i]
            return pathlib.Path(pkg_path) / f"{outer_class}.java"
    return pathlib.Path(fqcn.replace('.', '/') + '.java')


def parse_warning_methods(jsonl_file: pathlib.Path) -> list:
    """
    Read warningMethods.jsonl. Each non-empty line is a JSON object with a
    fully-qualified Specimin target plus the exact warning it came from:
        {"target": "org.greenrobot.eventbus.EventBus#subscribe(Object, SubscriberMethod)",
         "kind": "method", "warning": "...", "file": "...", "line": 42}
    or, for a bare field declaration:
        {"target": "org.greenrobot.eventbus.EventBus#defaultInstance",
         "kind": "field", "warning": "...", "file": "...", "line": 46}

    Returns a list of (rel_file, target, kind, short_name, warning_text), in
    file order, WITHOUT deduplication -- the same target can appear more than
    once if more than one warning was reported inside that method/field.
    """
    entries = []
    for raw in jsonl_file.read_text(encoding='utf-8').splitlines():
        raw = raw.strip()
        if not raw:
            continue
        record = json.loads(raw)
        target = record["target"].strip()
        kind = record.get("kind", "method")
        warning_text = record["warning"]

        hash_idx = target.find('#')
        if hash_idx == -1:
            print(f"  [SKIP] malformed target (no '#'): {target!r}")
            continue

        fqcn = target[:hash_idx]
        member = target[hash_idx + 1:]
        paren = member.find('(')
        short_name = member[:paren] if paren != -1 else member

        rel_file = fqcn_to_rel_file(fqcn)
        entries.append((rel_file, target, kind, short_name, warning_text))
    return entries


# ── Specimin runner ────────────────────────────────────────────────────────────

def write_warning_copy(output_dir: pathlib.Path, warning_text: str) -> None:
    """Write the exact warning the slice was produced for into the slice folder."""
    (output_dir / "warning.txt").write_text(warning_text + "\n", encoding="utf-8")


def run_specimin(rel_file, target, kind, short_name, warning_text, index, dry_run=False) -> int:
    output_dir = SPECIMIN_OUT / f"{index:02d}_{short_name}"
    target_flag = '--targetMethod' if kind == 'method' else '--targetField'

    specimin_args = [
        '--root',            str(EVENTBUS_SRC_ROOT),
        '--targetFile',      str(rel_file),
        target_flag,         target,
        '--outputDirectory', str(output_dir),
        '--jarPath',         str(JAR_PATH),
        '--modularityModel', 'nullaway',
    ]
    args_str = ' '.join(shlex.quote(a) for a in specimin_args)
    cmd = [str(GRADLEW), "--no-daemon", "run", f"--args={args_str}"]

    print(f"\n{'─' * 60}")
    print(f"[{index:02d}] ({kind}) {target}")
    print(f"      out     → {output_dir.name}")
    print(f"      warning : {warning_text}")
    print(f"      cmd     : {' '.join(cmd)}")

    if dry_run:
        print("      [dry-run — skipped]")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, cwd=str(SPECIMIN_DIR))
    status = "[OK]" if result.returncode == 0 else f"[FAILED — exit {result.returncode}]"
    print(f"      {status}")

    if result.returncode == 0:
        write_warning_copy(output_dir, warning_text)

    return result.returncode


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    required = [
        (WARNING_METHODS_FILE, "warningMethods.jsonl"),
        (EVENTBUS_SRC_ROOT,    "project src root (EVENTBUS_SRC_ROOT)"),
    ]
    if not dry_run:
        required += [
            (SPECIMIN_DIR, "Specimin directory"),
            (GRADLEW,      "gradlew"),
            (JAR_PATH,     "jar dependency directory"),
        ]
    for path, label in required:
        if not path.exists():
            print(f"ERROR: {label} not found:\n  {path}")
            sys.exit(1)

    if not dry_run:
        SPECIMIN_OUT.mkdir(parents=True, exist_ok=True)

    entries = parse_warning_methods(WARNING_METHODS_FILE)
    print(f"Found {len(entries)} warning entry(ies) in {WARNING_METHODS_FILE.name} (duplicates kept)")
    if dry_run:
        print("(dry-run mode — Specimin will not be executed)")

    successes, failures = 0, []

    for i, (rel_file, target, kind, short_name, warning_text) in enumerate(entries, start=1):
        rc = run_specimin(rel_file, target, kind, short_name, warning_text, i, dry_run=dry_run)
        if rc == 0:
            successes += 1
        else:
            failures.append((i, target))

    print(f"\n{'═' * 60}")
    print(f"Summary: {successes}/{len(entries)} targets succeeded.")
    if failures:
        print("Failed:")
        for idx, info in failures:
            print(f"  [{idx:02d}] {info}")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
