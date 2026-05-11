#!/usr/bin/env python3
"""
RunSpeciminAll.py

Reads nullunmarked-locations.txt, derives Specimin arguments for each
@NullUnmarked method, and runs Specimin to produce a reduced program
repository per location under SPECIMIN_OUT.

Usage:
    python3 RunSpeciminAll.py            # run all
    python3 RunSpeciminAll.py --dry-run  # print commands only, no execution
"""
from __future__ import annotations

import re
import sys
import pathlib
import subprocess

# ── Paths ──────────────────────────────────────────────────────────────────────
NULLUNMARKED_FILE = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/nullunmarked-locations.txt"
)
EVENTBUS_SRC_ROOT = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src"
)
SPECIMIN_DIR = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/specimin"
)
SPECIMIN_OUT = pathlib.Path(
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out"
)
JAR_PATH = pathlib.Path.home() / "eventbus-deps"
GRADLEW  = SPECIMIN_DIR / "gradlew"

# ── Java source helpers ────────────────────────────────────────────────────────

def strip_inline(line: str) -> str:
    """Remove // comments and string/char literals from a single line."""
    result, i, n = [], 0, len(line)
    while i < n:
        c = line[i]
        if c in ('"', "'"):
            q, i = c, i + 1
            while i < n:
                if line[i] == '\\':
                    i += 2
                    continue
                if line[i] == q:
                    i += 1
                    break
                i += 1
        elif c == '/' and i + 1 < n and line[i + 1] == '/':
            break
        else:
            result.append(c)
            i += 1
    return ''.join(result)


def get_package(lines: list) -> str:
    for line in lines:
        m = re.match(r'\s*package\s+([\w.]+)\s*;', line)
        if m:
            return m.group(1)
    return ""


def get_class_stack_at(lines: list, target_idx: int) -> list:
    """
    Walk lines up to target_idx tracking brace depth and class/interface/enum
    declarations. Returns the list of enclosing type names at that line.
    Handles block comments and defers class push until its opening '{' is seen.
    """
    class_stack   = []
    open_depths   = []
    brace_depth   = 0
    pending_class = None
    in_block      = False

    for i, raw in enumerate(lines):
        if i > target_idx:
            break

        # ── strip block comments ──────────────────────────────────────────────
        line = raw
        if in_block:
            if '*/' in line:
                line = line[line.index('*/') + 2:]
                in_block = False
            else:
                continue
        if '/*' in line:
            pre  = line[:line.index('/*')]
            tail = line[line.index('/*') + 2:]
            if '*/' in tail:
                line = pre + tail[tail.index('*/') + 2:]
            else:
                in_block = True
                line = pre

        cleaned = strip_inline(line)

        # ── detect type declaration ───────────────────────────────────────────
        m = re.search(r'\b(?:class|interface|enum)\s+(\w+)', cleaned)
        if m:
            pending_class = m.group(1)

        # ── process braces ────────────────────────────────────────────────────
        for ch in cleaned:
            if ch == '{':
                brace_depth += 1
                if pending_class is not None:
                    class_stack.append(pending_class)
                    open_depths.append(brace_depth)
                    pending_class = None
            elif ch == '}':
                if open_depths and brace_depth == open_depths[-1]:
                    class_stack.pop()
                    open_depths.pop()
                brace_depth -= 1

    return class_stack


def get_sig_line(lines: list, target_idx: int) -> str:
    """
    Return the line that holds the actual method signature.
    When @NullUnmarked appears on a standalone line (e.g. @NullUnmarked @Override
    with no '(' on the same line), the signature is on the following line.
    """
    line = lines[target_idx]
    if '(' not in line and target_idx + 1 < len(lines):
        return lines[target_idx + 1]
    return line


def split_params(params_str: str) -> list:
    """Split a parameter string by top-level commas (respects <...> depth)."""
    params, buf, depth = [], [], 0
    for ch in params_str:
        if ch == '<':
            depth += 1; buf.append(ch)
        elif ch == '>':
            depth -= 1; buf.append(ch)
        elif ch == ',' and depth == 0:
            params.append(''.join(buf).strip()); buf = []
        else:
            buf.append(ch)
    if buf:
        params.append(''.join(buf).strip())
    return params


_KEYWORDS = frozenset({
    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'return',
    'try', 'catch', 'finally', 'throw', 'new', 'class', 'interface',
    'enum', 'synchronized', 'instanceof', 'super', 'this', 'void',
    'public', 'private', 'protected', 'static', 'final', 'abstract',
})


def parse_method_sig(sig_line: str):
    """
    Extract (method_name, [param_types]) from a Java method declaration.
    Strips annotations (@Nullable etc.), modifiers, return type, param names.
    Returns (None, []) if no method signature is found.
    """
    m = re.search(r'(\w+)\s*\(([^)]*)\)', sig_line)
    if not m:
        return None, []

    method_name = m.group(1)
    if method_name in _KEYWORDS:
        return None, []

    raw_params = m.group(2).strip()
    if not raw_params:
        return method_name, []

    param_types = []
    for param in split_params(raw_params):
        param = re.sub(r'@\w+\s*', '', param).strip()   # drop annotations
        param = re.sub(r'\bfinal\s+', '', param).strip() # drop 'final'
        tokens = param.split()
        if len(tokens) >= 2:
            param_types.append(' '.join(tokens[:-1]))    # drop variable name
        elif tokens:
            param_types.append(tokens[0])

    return method_name, param_types


# ── Location parsing ───────────────────────────────────────────────────────────

def parse_locations(txt_file: pathlib.Path) -> list:
    """
    Parse nullunmarked-locations.txt.
    Each line: /abs/path/To/File.java:linenum:    @NullUnmarked ...
    Returns list of (file_path, line_num).
    """
    entries = []
    for raw in txt_file.read_text(encoding='utf-8').splitlines():
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split(':', 2)
        if len(parts) < 2:
            continue
        fp = pathlib.Path(parts[0])
        try:
            ln = int(parts[1])
        except ValueError:
            continue
        entries.append((fp, ln))
    return entries


# ── Specimin argument derivation ───────────────────────────────────────────────

def derive_args(file_path: pathlib.Path, line_num: int):
    """
    Derive (rel_file, target_method, method_name) for a single @NullUnmarked
    location by parsing the Java source file.
    """
    lines      = file_path.read_text(encoding='utf-8').splitlines()
    target_idx = line_num - 1

    package     = get_package(lines)
    class_stack = get_class_stack_at(lines, target_idx)
    if not class_stack:
        raise ValueError(f"No enclosing class found at {file_path.name}:{line_num}")

    fqcn     = (package + '.' if package else '') + '.'.join(class_stack)
    sig_line = get_sig_line(lines, target_idx)

    method_name, param_types = parse_method_sig(sig_line)
    if not method_name:
        raise ValueError(
            f"Could not parse method signature at {file_path.name}:{line_num}"
            f" — line: {sig_line.strip()!r}"
        )

    target_method = f"{fqcn}#{method_name}({', '.join(param_types)})"
    rel_file      = file_path.relative_to(EVENTBUS_SRC_ROOT)
    return rel_file, target_method, method_name


# ── Specimin runner ────────────────────────────────────────────────────────────

def run_specimin(
    rel_file:      pathlib.Path,
    target_method: str,
    method_name:   str,
    index:         int,
    dry_run:       bool = False,
) -> int:
    output_dir = SPECIMIN_OUT / f"{index:02d}_{method_name}"
    output_dir.mkdir(parents=True, exist_ok=True)

    args_str = (
        f'--root "{EVENTBUS_SRC_ROOT}" '
        f'--targetFile "{rel_file}" '
        f'--targetMethod "{target_method}" '
        f'--outputDirectory "{output_dir}" '
        f'--jarPath "{JAR_PATH}" '
        f'--modularityModel nullaway'
    )
    cmd = [str(GRADLEW), "run", f"--args={args_str}"]

    print(f"\n{'─' * 60}")
    print(f"[{index:02d}] {target_method}")
    print(f"      out → {output_dir.name}")
    print(f"      cmd : {' '.join(cmd)}")

    if dry_run:
        print("      [dry-run — skipped]")
        return 0

    result = subprocess.run(cmd, cwd=str(SPECIMIN_DIR))
    status = "[OK]" if result.returncode == 0 else f"[FAILED — exit {result.returncode}]"
    print(f"      {status}")
    return result.returncode


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    for path, label in [
        (NULLUNMARKED_FILE, "nullunmarked-locations.txt"),
        (EVENTBUS_SRC_ROOT, "EventBus src root"),
        (SPECIMIN_DIR,      "Specimin directory"),
        (GRADLEW,           "gradlew"),
        (JAR_PATH,          "jar dependency directory"),
    ]:
        if not path.exists():
            print(f"ERROR: {label} not found:\n  {path}")
            sys.exit(1)

    SPECIMIN_OUT.mkdir(parents=True, exist_ok=True)

    entries = parse_locations(NULLUNMARKED_FILE)
    print(f"Found {len(entries)} @NullUnmarked location(s) in {NULLUNMARKED_FILE.name}")
    if dry_run:
        print("(dry-run mode — Specimin will not be executed)\n")

    successes, failures = 0, []

    for i, (fp, ln) in enumerate(entries, start=1):
        print(f"\n[{i:02d}] Parsing {fp.name}:{ln} ...")
        try:
            rel_file, target_method, method_name = derive_args(fp, ln)
            print(f"      class  : {target_method.split('#')[0]}")
            print(f"      method : {target_method.split('#')[1]}")
        except ValueError as e:
            print(f"      [SKIP] {e}")
            failures.append((i, str(e)))
            continue

        rc = run_specimin(rel_file, target_method, method_name, i, dry_run=dry_run)
        if rc == 0:
            successes += 1
        else:
            failures.append((i, target_method))

    print(f"\n{'═' * 60}")
    print(f"Summary: {successes}/{len(entries)} succeeded.")
    if failures:
        print("Failed / skipped:")
        for idx, info in failures:
            print(f"  [{idx:02d}] {info}")

    sys.exit(0 if not failures else 1)


if __name__ == "__main__":
    main()
