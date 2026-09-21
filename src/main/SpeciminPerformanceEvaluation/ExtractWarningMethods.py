#!/usr/bin/env python3
"""
ExtractWarningMethods.py

Reads warning-location lines from nullaway-warnings.txt, finds the enclosing
method or constructor for each warning, and writes ONE entry per warning to
warningMethods.jsonl (in the same order as the input file):

    {"target": "org.greenrobot.eventbus.EventBus#post(Object)",
     "warning": "/path/EventBus.java:204: warning: [NullAway] ...",
     "file": "/path/EventBus.java", "line": 204}

Unlike LLMInferencePython/ExtractWarningMethods.py, this script does NOT
deduplicate by target method: if two warnings are reported inside the same
method, that method is written twice (once per warning), each paired with
its own exact warning line, so RunSpeciminAll.py can later produce one slice
per *warning* (not one slice per unique method) and drop a copy of the
originating warning into that slice's folder.

Field-level warnings (the warning is on a bare field declaration, e.g. an
uninitialized @NonNull field) are targeted with Specimin's --targetField
instead of --targetMethod. Only warnings that fall inside an anonymous
static/instance initializer block (`static { ... }`) are skipped: Specimin
unconditionally prunes those blocks (see PrunerVisitor#visit(Initializer
Declaration)), so there is no target that would keep such a warning
reproducible in a slice.

Usage:
    python3 ExtractWarningMethods.py

Paths can be overridden via environment variables:
    NULLAWAY_WARNINGS_FILE   path to nullaway-warnings.txt
    WARNING_METHODS_FILE     output file (default: next to NULLAWAY_WARNINGS_FILE)
"""
from __future__ import annotations

import os
import re
import sys
import json
import pathlib

# ── Paths ──────────────────────────────────────────────────────────────────────
def _path(env_name: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(env_name, default)).expanduser()


NULLAWAY_WARNINGS_FILE = _path(
    "NULLAWAY_WARNINGS_FILE",
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/nullaway-warnings.txt",
)
WARNING_METHODS_FILE = _path(
    "WARNING_METHODS_FILE",
    str(NULLAWAY_WARNINGS_FILE.parent / "warningMethods.jsonl"),
)

# ── Java source helpers (shared with RunSpeciminAll.py) ────────────────────────

def strip_strings_and_line_comment(line: str) -> str:
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
    return re.sub(r'@\w+(\s*\([^)]*\))?', ' ', text)


def split_params(params_str: str) -> list:
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
    text = strip_annotations(decl_text)
    eq = _TOPLEVEL_EQ.search(text)
    if eq:
        text = text[:eq.start()]
    text = text.rstrip().rstrip(';').rstrip()
    text = re.sub(r'(\[\s*\])+$', '', text).rstrip()
    m = re.search(r'(\w+)$', text)
    return m.group(1) if m else None


def index_members(cleaned: list):
    method_spans, field_spans, init_spans = [], [], []
    depth = 0
    class_body_depths = []
    brace_kinds = []
    member = None
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
                    mname, _ = parse_method_sig(pending)
                    if mname:
                        method_spans.append((pending_start, i, pending))
                    name = None
                if name:
                    field_spans.append((pending_start, i, name))
                pending, pending_start = '', None

    return method_spans, field_spans, init_spans


def innermost(spans, target_idx):
    best = None
    for span in spans:
        if span[0] <= target_idx <= span[1]:
            if best is None or span[0] > best[0]:
                best = span
    return best


# ── Location parsing ───────────────────────────────────────────────────────────
# Matches the javac diagnostic location line NullAway emits, e.g.:
#   /path/File.java:42: warning: [NullAway] dereferenced expression ... is @Nullable
# Continuation lines (source snippet, '^' caret, 'see ...') don't start with
# '<path>:<line>:' and are naturally skipped -- run-nullaway.sh's `grep -E
# '\[NullAway\]'` already reduces each warning to exactly this one line, so
# each matched line here IS the complete, exact warning message.
_LOCATION_RE = re.compile(r'^(.+?):(\d+):\s*(?:error|warning):\s*\[[^\]]+\]')


def parse_locations(txt_file: pathlib.Path) -> list:
    """Returns a list of (file, line, exact_warning_text), IN FILE ORDER,
    with NO deduplication -- a repeated (file, line) or a second warning in
    the same method both stay in the list, one entry per line of the input
    file."""
    entries = []
    for raw in txt_file.read_text(encoding='utf-8').splitlines():
        line = raw.strip()
        m = _LOCATION_RE.match(line)
        if not m:
            continue
        fp = pathlib.Path(m.group(1))
        ln = int(m.group(2))
        entries.append((fp, ln, line))
    return entries


# ── Target extraction ──────────────────────────────────────────────────────────

def extract_target(file_path: pathlib.Path, line_num: int):
    """
    Returns (kind, target, reason):
      - ('method', 'pkg.Class#name(Type1, Type2)', None) if the warning is
        inside a method/constructor body.
      - ('field', 'pkg.Class#fieldName', None) if the warning is on a bare
        field declaration (no enclosing method).
      - (None, None, reason) if neither applies -- reason explains why (e.g.
        the warning is inside an anonymous static/instance initializer
        block, which Specimin always strips regardless of target).
    """
    lines      = file_path.read_text(encoding='utf-8').splitlines()
    cleaned    = clean_lines(lines)
    target_idx = line_num - 1

    package     = get_package(lines)
    class_stack = get_class_stack_at(cleaned, target_idx)
    if not class_stack:
        return None, None, "no enclosing class found"
    fqcn = (package + '.' if package else '') + '.'.join(class_stack)

    method_spans, field_spans, init_spans = index_members(cleaned)

    method_span = innermost(method_spans, target_idx)
    if method_span is not None:
        name, params = parse_method_sig(method_span[2])
        if name:
            return 'method', f"{fqcn}#{name}({', '.join(params)})", None

    field_span = innermost(field_spans, target_idx)
    if field_span is not None and field_span[2]:
        return 'field', f"{fqcn}#{field_span[2]}", None

    if innermost(init_spans, target_idx) is not None:
        return None, None, (
            "inside a static/instance initializer block "
            "(Specimin always removes these, regardless of target)"
        )

    return None, None, "not inside a method, field, or initializer block"


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    if not NULLAWAY_WARNINGS_FILE.exists():
        print(f"ERROR: NullAway warnings file not found:\n  {NULLAWAY_WARNINGS_FILE}")
        sys.exit(1)

    locations = parse_locations(NULLAWAY_WARNINGS_FILE)
    print(f"Found {len(locations)} warning location(s) in {NULLAWAY_WARNINGS_FILE}.")

    entries = []
    skipped = 0

    for fp, ln, warning_text in locations:
        try:
            kind, target, reason = extract_target(fp, ln)
        except (OSError, ValueError) as e:
            print(f"  [SKIP] {fp.name}:{ln} — {e}")
            skipped += 1
            continue

        if target is None:
            print(f"  [SKIP] {fp.name}:{ln} — {reason}")
            skipped += 1
            continue

        entries.append({
            "target": target,
            "kind": kind,
            "warning": warning_text,
            "file": str(fp),
            "line": ln,
        })
        print(f"  + [{kind}] {target}  ({fp.name}:{ln})")

    WARNING_METHODS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with WARNING_METHODS_FILE.open('w', encoding='utf-8') as f:
        for entry in entries:
            f.write(json.dumps(entry) + '\n')

    print(f"\nWrote {len(entries)} warning entry(ies) (duplicates kept) to {WARNING_METHODS_FILE}")
    if skipped:
        print(f"Skipped {skipped} warning(s) (see [SKIP] reasons above).")


if __name__ == "__main__":
    main()