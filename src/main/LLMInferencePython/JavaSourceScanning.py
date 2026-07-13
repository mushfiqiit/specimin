#!/usr/bin/env python3
"""
JavaSourceScanning.py

Lightweight brace/regex Java source scanning helpers, shared by
PreserveUsages.py and ExtractUsageContext.py to locate field/method
declarations and their enclosing class at a given source line.

Not a real parser: it strips comments/strings and tracks brace depth rather
than building an AST. This module previously lived inside
ExtractWarningMethods.py; that script has been replaced by the AST-based
(JavaParser) `extractWarningMethods` Gradle task in the Specimin Java project
(org.checkerframework.specimin.warningmethods.WarningMethodExtractor), which
does not need these helpers, but PreserveUsages.py and ExtractUsageContext.py
still do, so the helpers were kept here rather than deleted.
"""
from __future__ import annotations

import re


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
