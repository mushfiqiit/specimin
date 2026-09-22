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
absolute file path (see derive_root), not one global JUNIT_SRC_ROOT: a
warning from a file under a different "src" tree resolves against its own
root instead of failing with "Specimin could not find the file for the
target class". JUNIT_SRC_ROOT is kept only as a fallback for the rare case
derive_root can't compute a root.

This mirrors LLMInferencePython/RunSpeciminAll.py's Specimin-invocation logic,
with these differences:
  1. warningMethods.jsonl is not deduplicated by target, so a method/field
     flagged by two different warnings gets two separate slice folders here
     (LLMInferencePython's version collapses them into a single slice).
  2. Each slice folder gets a warning.txt holding the exact warning line the
     slice was generated for (LLMInferencePython's version does not keep this).
  3. --root is derived per target instead of being one fixed source root, so
     warnings from other source trees resolve.

Each line of warningMethods.jsonl looks like:
    {"target": "org.junit.runner.Description#getTestClass()", "kind": "method",
     "warning": "/path/Description.java:291: warning: [NullAway] ...",
     "file": "/path/Description.java", "line": 291}

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
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/nullaway-warnings.txt",
)
WARNING_METHODS_FILE = _path(
    "WARNING_METHODS_FILE",
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/warningMethods.jsonl"
)
# Default Java source root (JUnit 4's main sources), used only as a fallback
# when a target's root can't be derived from its warning's absolute file path
# (see derive_root below).
JUNIT_SRC_ROOT = _path(
    "JUNIT_SRC_ROOT",
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/src/main/java",
)
SPECIMIN_DIR = _path(
    "SPECIMIN_DIR",
    "/Users/mushfiqurrahmanchowdhury/Documents/specimin",
)
SPECIMIN_OUT = _path(
    "SPECIMIN_OUT",
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/speciminout",
)
# JUnit 4's main sources have one external compile-time dependency --
# org.hamcrest:hamcrest-core (see junit4/pom.xml) -- so JAR_PATH must contain
# that jar for Specimin to resolve the Matcher/Hamcrest types used by
# org.junit.Assert, org.junit.Assume, org.junit.rules.*, etc.
# GenerateNullAwayWarnings.sh's PROJECT=junit path copies it there.
JAR_PATH = _path("JAR_PATH", "~/junit-deps")
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
        org.junit.runner.Description                -> org/junit/runner/Description.java
        org.junit.runners.model.TestClass.MethodComparator
                                                     -> org/junit/runners/model/TestClass.java
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
    global JUNIT_SRC_ROOT, so warnings from a file under a different "src"
    tree resolve correctly too.
    """
    abs_parts, rel_parts = abs_file.parts, rel_file.parts
    if len(abs_parts) <= len(rel_parts) or abs_parts[-len(rel_parts):] != rel_parts:
        return None
    return pathlib.Path(*abs_parts[:-len(rel_parts)])


def parse_warning_methods(jsonl_file: pathlib.Path) -> list:
    """
    Read warningMethods.jsonl. Each non-empty line is a JSON object with a
    fully-qualified Specimin target plus the exact warning it came from:
        {"target": "org.junit.runner.Description#createSuiteDescription(String, Annotation[])",
         "kind": "method", "warning": "...", "file": "...", "line": 42}
    or, for a bare field declaration:
        {"target": "org.junit.runner.Description#fTestClass",
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
    can come from different source trees, so downstream tools that need to find a
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
        root = JUNIT_SRC_ROOT
        root_note = "  (derive_root failed -- falling back to JUNIT_SRC_ROOT)"

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
        (JUNIT_SRC_ROOT,       "project src root (JUNIT_SRC_ROOT)"),
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