#!/usr/bin/env python3
"""
ExtractWarningMethods.py

Reads warning-location lines from nullaway-warnings.txt AND
index-checker-warnings.log, finds the enclosing method or constructor for
each warning, and writes a deduplicated list to warningMethods.txt (one
fully-qualified Specimin-style target per line).

  com.google.gson.Gson#toJson(Object, Type, JsonWriter)
  com.google.gson.internal.LinkedTreeMap#size()
  ...

Both tools emit the same underlying javac diagnostic shape
('<file>:<line>: error|warning: [Tag] message'), so a single location matcher
handles both; each input file is optional, but at least one must exist.
A method flagged by both NullAway and the Index Checker is only listed once.

Field-level warnings (where the warning is on a bare field declaration rather
than inside a method body) are skipped because they have no callable signature.

Usage:
    python3 ExtractWarningMethods.py

Paths can be overridden via environment variables:
    NULLAWAY_WARNINGS_FILE       path to nullaway-warnings.txt
    INDEX_CHECKER_WARNINGS_FILE  path to index-checker-warnings.log
    WARNING_METHODS_FILE         output file (default: next to NULLAWAY_WARNINGS_FILE)
"""
from __future__ import annotations

import os
import re
import sys
import pathlib

# ── Paths ──────────────────────────────────────────────────────────────────────
def _path(env_name: str, default: str) -> pathlib.Path:
    return pathlib.Path(os.environ.get(env_name, default)).expanduser()

NULLAWAY_WARNINGS_FILE = _path(
    "NULLAWAY_WARNINGS_FILE",
    "~/Documents/gson/nullaway-warnings.txt",
)
INDEX_CHECKER_WARNINGS_FILE = _path(
    "INDEX_CHECKER_WARNINGS_FILE",
    "~/Documents/gson/index-checker-warnings.log",
)
WARNING_METHODS_FILE = _path(
    "WARNING_METHODS_FILE",
    str(pathlib.Path(
        os.environ.get("NULLAWAY_WARNINGS_FILE", "~/Documents/gson/nullaway-warnings.txt")
    ).expanduser().parent / "warningMethods.txt"),
)

# Sources to read, in the order their locations get processed. Each is
# (path, label); a missing file is skipped rather than treated as an error,
# so this also works with just one of the two warning files present.
WARNING_SOURCES = [
    (NULLAWAY_WARNINGS_FILE, "NullAway"),
    (INDEX_CHECKER_WARNINGS_FILE, "Index Checker"),
]

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
# Matches the javac diagnostic location line both NullAway and the Checker
# Framework's Index Checker emit, e.g.:
#   /path/File.java:60: error: [argument] incompatible argument for parameter...
#   /path/File.java:42: warning: [NullAway] dereferenced expression ... is @Nullable
# Continuation lines (source snippet, '^' caret, 'found/required:') don't start
# with '<path>:<line>:' and are naturally skipped.
_LOCATION_RE = re.compile(r'^(.+?):(\d+):\s*(?:error|warning):\s*\[[^\]]+\]')


def parse_locations(txt_file: pathlib.Path) -> list:
    entries, seen = [], set()
    for raw in txt_file.read_text(encoding='utf-8').splitlines():
        m = _LOCATION_RE.match(raw.strip())
        if not m:
            continue
        fp = pathlib.Path(m.group(1))
        ln = int(m.group(2))
        if (fp, ln) not in seen:
            seen.add((fp, ln))
            entries.append((fp, ln))
    return entries


# ── Method extraction ──────────────────────────────────────────────────────────

def extract_method_target(file_path: pathlib.Path, line_num: int):
    """
    Returns a Specimin-style 'pkg.Class#name(Type1, Type2)' string if the
    warning is inside a method/constructor body, or None if it is on a bare
    field or unrecognised location.
    """
    lines      = file_path.read_text(encoding='utf-8').splitlines()
    cleaned    = clean_lines(lines)
    target_idx = line_num - 1

    package     = get_package(lines)
    class_stack = get_class_stack_at(cleaned, target_idx)
    if not class_stack:
        return None
    fqcn = (package + '.' if package else '') + '.'.join(class_stack)

    method_spans, _field_spans, _init_spans = index_members(cleaned)

    span = innermost(method_spans, target_idx)
    if span is None:
        return None

    name, params = parse_method_sig(span[2])
    if not name:
        return None

    return f"{fqcn}#{name}({', '.join(params)})"


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    available_paths = {p for p, _ in WARNING_SOURCES if p.exists()}
    if not available_paths:
        print("ERROR: none of the warning files were found:")
        for p, label in WARNING_SOURCES:
            print(f"  [{label}] {p}")
        sys.exit(1)

    seen_locations, entries = set(), []
    for path, label in WARNING_SOURCES:
        if path not in available_paths:
            print(f"Skipping {label}: {path} not found.")
            continue
        locs = parse_locations(path)
        print(f"Found {len(locs)} {label} warning location(s) in {path}.")
        for fp, ln in locs:
            if (fp, ln) not in seen_locations:
                seen_locations.add((fp, ln))
                entries.append((fp, ln))

    seen, methods = set(), []
    skipped = 0

    for fp, ln in entries:
        try:
            target = extract_method_target(fp, ln)
        except (OSError, ValueError) as e:
            print(f"  [SKIP] {fp.name}:{ln} — {e}")
            skipped += 1
            continue

        if target is None:
            print(f"  [SKIP] {fp.name}:{ln} — not inside a method/constructor")
            skipped += 1
            continue

        if target not in seen:
            seen.add(target)
            methods.append(target)
            print(f"  + {target}")
        else:
            print(f"  (dup) {fp.name}:{ln} — already listed")

    WARNING_METHODS_FILE.parent.mkdir(parents=True, exist_ok=True)
    WARNING_METHODS_FILE.write_text('\n'.join(methods) + ('\n' if methods else ''),
                                    encoding='utf-8')

    print(f"\nWrote {len(methods)} unique method target(s) to {WARNING_METHODS_FILE}")
    if skipped:
        print(f"Skipped {skipped} warning(s) (field declarations or parse errors).")


if __name__ == "__main__":
    main()
