#!/usr/bin/env python3
"""
RunSpeciminAll.py

Reads warningMethods.jsonl (produced by ExtractWarningMethods.py in this same
folder) and runs Specimin once per WARNING (not once per unique method) to
produce a reduced program per entry under SPECIMIN_OUT. After each successful
Specimin run, the exact NullAway warning line the slice was produced for is
written into that slice's folder as warning.txt, and the --root used for
that slice is written as root.txt (read by FixSpeciminNullInits.py).

Each entry carries a "kind" of either "method" (sliced with Specimin's
--targetMethod) or "field" (a bare field declaration, sliced with
--targetField) -- see ExtractWarningMethods.py.

The --root passed to Specimin is derived PER TARGET from the warning's own
absolute file path (see derive_root), not one global EVENTBUS_SRC_ROOT: a
warning from a different module with its own "src" tree (e.g. EventBus's
core module vs. EventBusAnnotationProcessor) resolves against its own
module root instead of failing with "Specimin could not find the file for
the target class". EVENTBUS_SRC_ROOT is kept only as a fallback for the
rare case derive_root can't compute a root.

This mirrors LLMInferencePython/RunSpeciminAll.py's Specimin-invocation logic,
with these differences:
  1. warningMethods.jsonl is not deduplicated by target, so a method/field
     flagged by two different warnings gets two separate slice folders here
     (LLMInferencePython's version collapses them into a single slice).
  2. Each slice folder gets a warning.txt holding the exact warning line the
     slice was generated for (LLMInferencePython's version does not keep this).
  3. --root is derived per target instead of being one fixed source root, so
     warnings from other modules (e.g. EventBusAnnotationProcessor) resolve.

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
# Default Java source root, used only as a fallback when a target's root
# can't be derived from its warning's absolute file path (see derive_root
# below) -- e.g. EventBus's core module vs. EventBusAnnotationProcessor,
# which are separate module trees with their own "src" directories.
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


def derive_root(abs_file: pathlib.Path, rel_file: pathlib.Path):
    """
    Given the warning's absolute source file and the package-relative path
    computed from its target's FQCN, return the source root R such that
    R / rel_file == abs_file -- i.e. the actual "src" directory this file
    lives under -- or None if abs_file doesn't end with rel_file's parts
    (e.g. the file couldn't be read when the warning was extracted).

    This lets each target use ITS OWN module's source root instead of one
    global EVENTBUS_SRC_ROOT, so warnings from a different module (e.g.
    EventBusAnnotationProcessor, which has its own separate "src" tree from
    EventBus's core module) resolve correctly too.
    """
    abs_parts, rel_parts = abs_file.parts, rel_file.parts
    if len(abs_parts) <= len(rel_parts) or abs_parts[-len(rel_parts):] != rel_parts:
        return None
    return pathlib.Path(*abs_parts[:-len(rel_parts)])


def parse_warning_methods(jsonl_file: pathlib.Path) -> list:
    """
    Read warningMethods.jsonl. Each non-empty line is a JSON object with a
    fully-qualified Specimin target plus the exact warning it came from:
        {"target": "org.greenrobot.eventbus.EventBus#subscribe(Object, SubscriberMethod)",
         "kind": "method", "warning": "...", "file": "...", "line": 42}
    or, for a bare field declaration:
        {"target": "org.greenrobot.eventbus.EventBus#defaultInstance",
         "kind": "field", "warning": "...", "file": "...", "line": 46}

    Returns a list of (rel_file, target, kind, short_name, warning_text,
    abs_file), in file order, WITHOUT deduplication -- the same target can
    appear more than once if more than one warning was reported inside that
    method/field. abs_file is used by derive_root to find that target's own
    module source root.
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
        abs_file = pathlib.Path(record["file"])

        hash_idx = target.find('#')
        if hash_idx == -1:
            print(f"  [SKIP] malformed target (no '#'): {target!r}")
            continue

        fqcn = target[:hash_idx]
        member = target[hash_idx + 1:]
        paren = member.find('(')
        short_name = member[:paren] if paren != -1 else member

        rel_file = fqcn_to_rel_file(fqcn)
        entries.append((rel_file, target, kind, short_name, warning_text, abs_file))
    return entries


# ── Specimin runner ────────────────────────────────────────────────────────────

def write_warning_copy(output_dir: pathlib.Path, warning_text: str) -> None:
    """Write the exact warning the slice was produced for into the slice folder."""
    (output_dir / "warning.txt").write_text(warning_text + "\n", encoding="utf-8")


def write_root_copy(output_dir: pathlib.Path, root: pathlib.Path) -> None:
    """
    Record the --root this slice was generated against, in root.txt. Slices
    can come from different module source trees (e.g. EventBus's core module
    vs. EventBusAnnotationProcessor), so downstream tools that need to find a
    slice's ORIGINAL source file (FixSpeciminNullInits.py) can't assume one
    global source root either -- they read this instead.
    """
    (output_dir / "root.txt").write_text(str(root) + "\n", encoding="utf-8")


def run_specimin(rel_file, target, kind, short_name, warning_text, abs_file, index, dry_run=False) -> int:
    output_dir = SPECIMIN_OUT / f"{index:02d}_{short_name}"
    target_flag = '--targetMethod' if kind == 'method' else '--targetField'

    root = derive_root(abs_file, rel_file)
    root_note = ""
    if root is None:
        root = EVENTBUS_SRC_ROOT
        root_note = "  (derive_root failed -- falling back to EVENTBUS_SRC_ROOT)"

    specimin_args = [
        '--root',            str(root),
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
    print(f"      root    → {root}{root_note}")
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
        write_root_copy(output_dir, root)

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

    for i, (rel_file, target, kind, short_name, warning_text, abs_file) in enumerate(entries, start=1):
        rc = run_specimin(rel_file, target, kind, short_name, warning_text, abs_file, i, dry_run=dry_run)
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
