#!/usr/bin/env python3
"""
RunSpeciminAll.py

Reads nullaway-warnings.txt, derives a Specimin target for each NullAway
warning, and runs Specimin to produce a reduced program per unique target
under SPECIMIN_OUT.

A warning line looks like:
    /abs/path/To/File.java:LINE: error: [NullAway] <message>

For each warning the script finds the *enclosing member* of the warning line
by scanning the brace structure of the Java file:
  * inside a method/constructor body  -> --targetMethod "pkg.Class#name(Type1, Type2)"
  * on a field declaration            -> --targetField  "pkg.Class#fieldName"
  * inside a static/instance initializer block
                                      -> --targetField for the field being assigned

Usage:
    python3 RunSpeciminAll.py            # run all
    python3 RunSpeciminAll.py --dry-run  # only derive + print targets, don't run Specimin

Paths below can be overridden with environment variables of the same name
(useful for testing); by default they point at the usual locations.
"""
from __future__ import annotations

import os
import re
import sys
import shlex
import pathlib
import subprocess

# ── Paths ──────────────────────────────────────────────────────────────────────
def _path(env_name: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(env_name, default)).expanduser()

NULLAWAY_WARNINGS_FILE = _path(
    "NULLAWAY_WARNINGS_FILE",
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/nullaway-warnings.txt",
)
WARNING_METHODS_FILE = _path(
    "WARNING_METHODS_FILE",
    str(pathlib.Path(
        os.environ.get(
            "NULLAWAY_WARNINGS_FILE",
            "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/nullaway-warnings.txt",
        )
    ).parent / "warningMethods.txt"),
)
EVENTBUS_SRC_ROOT = _path(
    "EVENTBUS_SRC_ROOT",
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src",
)
SPECIMIN_DIR = _path(
    "SPECIMIN_DIR",
    "/Users/mushfiqurrahmanchowdhury/Documents/specimin",
)
SPECIMIN_OUT = _path(
    "SPECIMIN_OUT",
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/specimin-out",
)
JAR_PATH = _path("JAR_PATH", str(pathlib.Path.home() / "eventbus-deps"))
GRADLEW  = SPECIMIN_DIR / "gradlew"

# ── Java source helpers ────────────────────────────────────────────────────────

def strip_strings_and_line_comment(line: str) -> str:
    """Remove // comments and the contents of string/char literals from a line."""
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


def clean_lines(lines: list) -> list:
    """Return lines with block comments, // comments and literals removed."""
    cleaned = []
    in_block = False
    for raw in lines:
        line = raw
        out = []
        i = 0
        while i < len(line):
            if in_block:
                end = line.find('*/', i)
                if end == -1:
                    i = len(line)
                else:
                    in_block = False
                    i = end + 2
            else:
                start = line.find('/*', i)
                if start == -1:
                    out.append(line[i:])
                    i = len(line)
                else:
                    out.append(line[i:start])
                    in_block = True
                    i = start + 2
        cleaned.append(strip_strings_and_line_comment(''.join(out)))
    return cleaned


def get_package(lines: list) -> str:
    for line in lines:
        m = re.match(r'\s*package\s+([\w.]+)\s*;', line)
        if m:
            return m.group(1)
    return ""


def get_class_stack_at(cleaned: list, target_idx: int) -> list:
    """
    Walk cleaned lines up to target_idx tracking brace depth and
    class/interface/enum declarations. Returns the enclosing type names.
    """
    class_stack, open_depths = [], []
    brace_depth, pending_class = 0, None

    for i, line in enumerate(cleaned):
        if i > target_idx:
            break
        m = re.search(r'\b(?:class|interface|enum)\s+(\w+)', line)
        if m:
            pending_class = m.group(1)
        for ch in line:
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


def strip_annotations(text: str) -> str:
    """Remove @Annotation and @Annotation(...) tokens (literals already stripped)."""
    return re.sub(r'@\w+(\s*\([^)]*\))?', ' ', text)


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
    'enum', 'synchronized', 'instanceof', 'super', 'this', 'assert',
})


def parse_method_sig(decl_text: str):
    """
    Extract (method_name, [param_types]) from a method/constructor declaration.
    decl_text must already have comments/literals stripped; annotations are
    removed here. Returns (None, []) if it doesn't look like a declaration.
    """
    text = strip_annotations(decl_text)
    m = re.search(r'(\w+)\s*\(([^)]*)\)', text)
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
        param = re.sub(r'\bfinal\s+', '', param).strip()
        tokens = param.split()
        if len(tokens) >= 2:
            param_types.append(' '.join(tokens[:-1]))
        elif tokens:
            param_types.append(tokens[0])
    return method_name, param_types


_TOPLEVEL_EQ = re.compile(r'(?<![=!<>+\-*/%&|^])=(?!=)')


def parse_field_name(decl_text: str):
    """Extract the field name from a (cleaned) field declaration."""
    text = strip_annotations(decl_text)
    eq = _TOPLEVEL_EQ.search(text)
    if eq:
        text = text[:eq.start()]
    text = text.rstrip().rstrip(';').rstrip()
    text = re.sub(r'(\[\s*\])+$', '', text).rstrip()
    m = re.search(r'(\w+)$', text)
    return m.group(1) if m else None


def index_members(cleaned: list):
    """
    Single forward pass over the cleaned lines of a Java file.
    Returns (method_spans, field_spans, init_spans):
      method_spans: list of (start, end, decl_text)
      field_spans:  list of (start, end, field_name)
      init_spans:   list of (start, end)   # static/instance initializer blocks
    Members declared inside anonymous classes are attributed to the enclosing
    field/method span, which is what Specimin needs anyway.
    """
    method_spans, field_spans, init_spans = [], [], []

    depth = 0
    class_body_depths = []   # depths of currently-open class bodies
    brace_kinds = []         # what each currently-open '{' belongs to
    member = None            # (kind, start_idx, payload) for the open member
    pending, pending_start = '', None

    def at_member_level() -> bool:
        if member is not None:
            return False
        expected = class_body_depths[-1] if class_body_depths else 0
        return depth == expected

    for i, line in enumerate(cleaned):
        if at_member_level() and line.strip():
            if not pending:
                pending_start = i
            pending += ' ' + line.strip()

        for ch in line:
            if ch == '{':
                if at_member_level():
                    decl = strip_annotations(pending[:pending.rfind('{')] if '{' in pending else pending)
                    eq = _TOPLEVEL_EQ.search(decl)
                    paren = decl.find('(')
                    if re.search(r'\b(?:class|interface|enum)\s+\w+', decl):
                        kind = 'class'
                    elif eq and (paren == -1 or eq.start() < paren):
                        kind = 'fieldinit'
                    else:
                        name, _ = parse_method_sig(decl)
                        kind = 'method' if name else 'init'

                    if kind == 'class':
                        class_body_depths.append(depth + 1)
                        brace_kinds.append('class')
                    elif kind == 'method':
                        member = ('method', pending_start, pending)
                        brace_kinds.append('member')
                    elif kind == 'fieldinit':
                        member = ('field', pending_start, parse_field_name(pending))
                        brace_kinds.append('member')
                    else:
                        member = ('init', pending_start, None)
                        brace_kinds.append('member')
                    pending, pending_start = '', None
                else:
                    brace_kinds.append('plain')
                depth += 1
            elif ch == '}':
                depth -= 1
                kind = brace_kinds.pop() if brace_kinds else 'plain'
                if kind == 'class':
                    class_body_depths.pop()
                    pending, pending_start = '', None
                elif kind == 'member':
                    mkind, mstart, payload = member
                    if mkind == 'method':
                        method_spans.append((mstart, i, payload))
                    elif mkind == 'field':
                        field_spans.append((mstart, i, payload))
                    else:
                        init_spans.append((mstart, i))
                    member = None
            elif ch == ';' and at_member_level() and pending.strip(';').strip():
                decl = strip_annotations(pending)
                eq = _TOPLEVEL_EQ.search(decl)
                paren = decl.find('(')
                if eq and (paren == -1 or eq.start() < paren):
                    name = parse_field_name(pending)
                elif paren == -1:
                    name = parse_field_name(pending)
                else:
                    # abstract/interface method declaration without a body
                    mname, _ = parse_method_sig(pending)
                    if mname:
                        method_spans.append((pending_start, i, pending))
                    name = None
                if name:
                    field_spans.append((pending_start, i, name))
                pending, pending_start = '', None

    return method_spans, field_spans, init_spans


def innermost(spans, target_idx):
    """Return the payload-bearing span containing target_idx with the largest start."""
    best = None
    for span in spans:
        if span[0] <= target_idx <= span[1]:
            if best is None or span[0] > best[0]:
                best = span
    return best


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
    # fallback — shouldn't happen for well-formed Java names
    return pathlib.Path(fqcn.replace('.', '/') + '.java')


def parse_warning_methods(txt_file: pathlib.Path) -> list:
    """
    Read warningMethods.txt.  Each non-empty line is already a fully-qualified
    Specimin target:
        org.greenrobot.eventbus.EventBus#subscribe(Object, SubscriberMethod)

    Returns a list of (rel_file, target, short_name), deduplicated.
    """
    entries, seen = [], set()
    for raw in txt_file.read_text(encoding='utf-8').splitlines():
        target = raw.strip()
        if not target or target.startswith('#'):
            continue
        if target in seen:
            continue
        seen.add(target)

        hash_idx = target.find('#')
        if hash_idx == -1:
            print(f"  [SKIP] malformed line (no '#'): {target!r}")
            continue

        fqcn = target[:hash_idx]
        member = target[hash_idx + 1:]
        paren = member.find('(')
        short_name = member[:paren] if paren != -1 else member

        rel_file = fqcn_to_rel_file(fqcn)
        entries.append((rel_file, target, short_name))
    return entries


# ── Specimin runner ────────────────────────────────────────────────────────────

def run_specimin(kind, rel_file, target, short_name, index, dry_run=False) -> int:
    output_dir = SPECIMIN_OUT / f"{index:02d}_{short_name}"
    target_flag = "--targetMethod" if kind == 'method' else "--targetField"

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
    print(f"      out → {output_dir.name}")
    print(f"      cmd : {' '.join(cmd)}")

    if dry_run:
        print("      [dry-run — skipped]")
        return 0

    output_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(cmd, cwd=str(SPECIMIN_DIR))
    status = "[OK]" if result.returncode == 0 else f"[FAILED — exit {result.returncode}]"
    print(f"      {status}")
    return result.returncode


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    dry_run = "--dry-run" in sys.argv

    required = [
        (WARNING_METHODS_FILE, "warningMethods.txt"),
        (EVENTBUS_SRC_ROOT,    "EventBus src root"),
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
    print(f"Found {len(entries)} unique method target(s) in {WARNING_METHODS_FILE.name}")
    if dry_run:
        print("(dry-run mode — Specimin will not be executed)")

    successes, failures = 0, []

    for i, (rel_file, target, short_name) in enumerate(entries, start=1):
        print(f"\n[{i:02d}] {target}")
        rc = run_specimin('method', rel_file, target, short_name, i, dry_run=dry_run)
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
