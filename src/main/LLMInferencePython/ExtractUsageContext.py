#!/usr/bin/env python3
"""
ExtractUsageContext.py

Post-slice usage extractor for the LLM-inference pipeline.

Specimin produces a *minimal* slice: the target member's body plus the
declarations it needs. The sites that actually constrain a field's type — its
dereferences, null-guards, and assignments in *other* methods — are not in the
slice (their bodies were stubbed out). Rather than bloat the compilable slice by
pulling those whole methods in, this module extracts just the relevant *lines*
from the original source and hands them to the LLM as read-only context.

The fields whose usages matter are exactly the fields that appear *in the slice*
(those are what the LLM will annotate). So, given the generated slice folder,
this module:
  1. scans the slice for every field declaration it contains (e.g. a field of a
     helper class such as FindState.clazz, not just the target class's fields),
  2. for each such field, collects the lines in the ORIGINAL source (in the
     field's declaring file by default) that reference it — dereferences,
     null-guards, assignments — with a little surrounding context, deduplicated
     and capped.

The result is rendered as a compact text block that RunLLMInferenceAll.py splices
into the prompt. It is *context only* — never compiled, never annotated.

Like the rest of this pipeline, references are located with lightweight
brace/regex Java scanning (reusing the helpers in JavaSourceScanning), not a
full symbol solver. The default scope is each field's declaring file (high
precision); ``repo`` widens to every source file (more recall, more noise).

Standalone:
    EVENTBUS_SRC_ROOT=/path/to/src python3 ExtractUsageContext.py \\
        --slice /path/to/specimin-out/01_Foo [--scope class|repo] [--lines N]
    # or, without a slice, fall back to the fields of a target's class:
    EVENTBUS_SRC_ROOT=/path/to/src python3 ExtractUsageContext.py \\
        'pkg.Class#method(T1, T2)' [--scope class|repo] [--lines N]
"""
from __future__ import annotations

import os
import re
import sys
import pathlib

from JavaSourceScanning import (
    clean_lines,
    get_package,
    get_class_stack_at,
    index_members,
    innermost,
    parse_method_sig,
)

# Hard cap on emitted context lines, to bound prompt tokens.
DEFAULT_MAX_LINES = 120


def _src_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "EVENTBUS_SRC_ROOT",
            "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src",
        )
    ).expanduser()


def fqcn_to_rel_file(fqcn: str) -> pathlib.Path:
    """Map a fully-qualified class name to its relative .java file path."""
    parts = fqcn.split('.')
    for i, part in enumerate(parts):
        if part and part[0].isupper():
            return pathlib.Path('/'.join(parts[:i])) / f"{parts[i]}.java"
    return pathlib.Path(fqcn.replace('.', '/') + '.java')


# ── Selecting which fields to show usages for ───────────────────────────────────

def _fields_in_file(abs_file):
    """All (declaring_class_fqcn, field_name) declared in one Java file."""
    try:
        lines = abs_file.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    cleaned = clean_lines(lines)
    package = get_package(lines)
    _methods, field_spans, _inits = index_members(cleaned)
    out = []
    for start, _end, name in field_spans:
        if not name:
            continue
        stack = get_class_stack_at(cleaned, start)
        if not stack:
            continue
        fqcn = (package + '.' if package else '') + '.'.join(stack)
        pair = (fqcn, name)
        if pair not in out:
            out.append(pair)
    return out


def fields_from_slice(slice_dir):
    """Every (declaring_class_fqcn, field_name) declared anywhere in the slice."""
    slice_dir = pathlib.Path(slice_dir)
    out = []
    for jf in sorted(slice_dir.rglob("*.java")):
        for pair in _fields_in_file(jf):
            if pair not in out:
                out.append(pair)
    return out


def fields_from_target(src_root, target):
    """Fallback: the fields of the target's class that the target touches.

    Used when no slice folder is available (standalone inspection).
    """
    fqcn = target.split('#', 1)[0]
    rel_file = fqcn_to_rel_file(fqcn)
    decl_abs = pathlib.Path(src_root) / rel_file
    if not decl_abs.exists():
        return []
    lines = decl_abs.read_text(encoding='utf-8').splitlines()
    cleaned = clean_lines(lines)
    package = get_package(lines)
    class_fields = [n for (c, n) in _fields_in_file(decl_abs) if c == fqcn]
    if not class_fields:
        return []
    member = target.split('#', 1)[1] if '#' in target else ''
    if member and '(' not in member:
        return [(fqcn, member)] if member in class_fields else []
    used = _fields_used_in_member(cleaned, package, fqcn, target, class_fields)
    return [(fqcn, n) for n in used]


def _member_span_for(cleaned, package, target_class_fqcn, primary_target):
    member = primary_target.split('#', 1)[1] if '#' in primary_target else primary_target
    paren = member.find('(')
    want_name = member[:paren] if paren != -1 else member
    want_sig = re.sub(r'\s', '', member) if paren != -1 else None
    method_spans, field_spans, _inits = index_members(cleaned)
    for start, end, decl in method_spans:
        stack = get_class_stack_at(cleaned, start)
        if not stack:
            continue
        fqcn = (package + '.' if package else '') + '.'.join(stack)
        name, params = parse_method_sig(decl)
        if name is None or fqcn != target_class_fqcn:
            continue
        sig = re.sub(r'\s', '', f"{name}({', '.join(params)})")
        if want_sig is not None and sig == want_sig:
            return start, end
        if want_sig is None and name == want_name:
            return start, end
    return None


def _fields_used_in_member(cleaned, package, target_class_fqcn, primary_target, class_fields):
    span = _member_span_for(cleaned, package, target_class_fqcn, primary_target)
    if span is None:
        return list(class_fields)
    start, end = span
    used = []
    patterns = [(f, re.compile(r'\b' + re.escape(f) + r'\b')) for f in class_fields]
    for idx in range(start, min(end, len(cleaned) - 1) + 1):
        line = cleaned[idx]
        if not line.strip():
            continue
        for f, pat in patterns:
            if f not in used and pat.search(line):
                used.append(f)
    return used


# ── Usage scanning ──────────────────────────────────────────────────────────────

def _usage_pattern(field_name):
    """Matches the field name as a whole word, qualified (``r.f``) or not (``f``)."""
    return re.compile(r'\b' + re.escape(field_name) + r'\b')


def _scan_file(abs_file, rel_file, names, context_lines, skip_decls):
    """Return [blocks] of (lineno, text) where any of ``names`` is used."""
    try:
        lines = abs_file.read_text(encoding='utf-8').splitlines()
    except OSError:
        return []
    cleaned = clean_lines(lines)

    skip = set()
    if skip_decls:
        _m, field_spans, _i = index_members(cleaned)
        for start, end, name in field_spans:
            if name in names:
                skip.update(range(start, end + 1))

    patterns = [_usage_pattern(n) for n in names]
    hits = [
        idx for idx, line in enumerate(cleaned)
        if idx not in skip and line.strip() and any(p.search(line) for p in patterns)
    ]
    if not hits:
        return []

    ranges = []
    for idx in hits:
        lo = max(0, idx - context_lines)
        hi = min(len(lines) - 1, idx + context_lines)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    return [[(n + 1, lines[n]) for n in range(lo, hi + 1)] for lo, hi in ranges]


def collect_usages(src_root, fields_with_class, scope="class",
                   context_lines=1, max_lines=DEFAULT_MAX_LINES):
    """Collect usage snippets for the given (fqcn, field) pairs.

    Returns (sorted field-name list, ordered [(rel_file, [blocks])]).
    """
    src_root = pathlib.Path(src_root)
    if not fields_with_class:
        return [], []

    # Group field names by their declaring file.
    by_file = {}
    for fqcn, name in fields_with_class:
        rel = str(fqcn_to_rel_file(fqcn))
        by_file.setdefault(rel, set()).add(name)

    all_names = sorted({n for _f, n in fields_with_class})
    per_file = []
    total = 0

    if scope == "repo":
        # Search every source file for the union of all field names.
        for abs_file in sorted(src_root.rglob("*.java")):
            try:
                rel = str(abs_file.relative_to(src_root))
            except ValueError:
                continue
            skip = rel in by_file
            blocks = _scan_file(abs_file, rel, set(all_names), context_lines, skip)
            if blocks:
                per_file.append((rel, blocks))
                total += sum(len(b) for b in blocks)
                if total >= max_lines:
                    break
    else:
        # Class scope: search each field's declaring file for its own names.
        for rel, names in sorted(by_file.items()):
            abs_file = src_root / rel
            if not abs_file.exists():
                continue
            blocks = _scan_file(abs_file, rel, names, context_lines, True)
            if blocks:
                per_file.append((rel, blocks))
                total += sum(len(b) for b in blocks)
                if total >= max_lines:
                    break

    return all_names, per_file


def render(fields, per_file):
    """Render collected usages as a compact, clearly-labelled prompt block."""
    if not per_file:
        return ""
    out = [
        "The following are read-only excerpts from the ORIGINAL program showing how the",
        "relevant field(s) are used, guarded, and assigned outside this slice. Use them as",
        "evidence for nullability/type inference. Do NOT annotate or modify these excerpts.",
        f"Fields of interest: {', '.join(fields)}",
    ]
    for rel, blocks in per_file:
        out.append(f"\n# {rel}")
        for block in blocks:
            for lineno, text in block:
                out.append(f"  {lineno:>5}: {text}")
            out.append("  ---")
    return "\n".join(out).rstrip()


def build_usage_context_text(src_root, target=None, slice_dir=None, scope="class",
                             context_lines=1, max_lines=DEFAULT_MAX_LINES):
    """Collect + render usage context. Prefers fields from the slice; falls back
    to the target's class fields. Returns '' if nothing found."""
    fields_with_class = []
    if slice_dir is not None:
        fields_with_class = fields_from_slice(slice_dir)
    if not fields_with_class and target is not None:
        fields_with_class = fields_from_target(src_root, target)
    fields, per_file = collect_usages(
        src_root, fields_with_class, scope, context_lines, max_lines
    )
    return render(fields, per_file)


# ── Standalone entry point ──────────────────────────────────────────────────────

def _opt(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main() -> None:
    scope = _opt("--scope", "class")
    lines = int(_opt("--lines", "1"))
    slice_dir = _opt("--slice", None)
    consumed = {"--scope", "--lines", "--slice", scope, str(lines)}
    if slice_dir is not None:
        consumed.add(slice_dir)
    positionals = [a for a in sys.argv[1:] if a not in consumed]
    target = positionals[0] if positionals else None

    if slice_dir is None and target is None:
        print("usage: ExtractUsageContext.py (--slice DIR | 'pkg.Class#member(...)') "
              "[--scope class|repo] [--lines N]")
        sys.exit(2)

    text = build_usage_context_text(
        _src_root(), target=target, slice_dir=slice_dir, scope=scope, context_lines=lines
    )
    print(text if text else "(no field usages found)")


if __name__ == "__main__":
    main()