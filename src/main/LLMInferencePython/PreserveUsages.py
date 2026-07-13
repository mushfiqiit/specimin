#!/usr/bin/env python3
"""
PreserveUsages.py

Reverse-reference / usage-preservation layer for the Specimin slicing step.

Background
----------
Specimin keeps the *whole body* of every target member, but for every other
member it keeps only the declaration and stubs the body out
(``throw new Error();``).  So if a field ``x`` is *declared* in the target
class but *dereferenced* (e.g. ``x.y = 0;``) inside some other, non-target
method, that dereference disappears from the slice.  Downstream the LLM then
sees ``x`` with no evidence that it is ever dereferenced and wrongly infers
``@Nullable``.

This module finds the members that dereference a field of the target class and
returns them as *extra* Specimin targets, so their bodies — and therefore the
dereference sites — are preserved in the slice.  Because Specimin accepts many
``--targetMethod`` / ``--targetField`` values in a single run (all relative to
one ``--root``, all written into one output directory), the extra targets are
folded into the same invocation and produce one combined slice.

A "dereference" is a use of the field as a receiver (``x.foo()``, ``x.y``) or an
array access (``x[i]``); these are exactly the uses that prove the field is
non-null at that point.  Plain writes such as ``x = e;`` are intentionally
*not* counted: they say nothing about ``x`` being dereferenced.

Precision note
--------------
Like the rest of this pipeline, references are located with lightweight,
brace-and-regex Java scanning rather than a full symbol solver.  To keep
false positives low the default scope is ``class`` (only the file where the
field is declared), and only unqualified (``x.``) or ``this.x.`` dereferences
are matched — ``other.x.`` is skipped because in another class it is unlikely
to be the same field.  Scope ``repo`` widens the search to every source file
under the root at the cost of precision.  For exact, solver-based resolution
the same idea can be moved into Specimin itself (see DEVELOPERS notes), but the
member-target strings produced here use the same format Specimin matches
against, so they slot straight into the existing pipeline.

Standalone usage (handy for inspection / debugging):
    EVENTBUS_SRC_ROOT=/path/to/src \
        python3 PreserveUsages.py 'pkg.Class#method(Type1, Type2)' [--scope class|repo]
"""
from __future__ import annotations

import os
import re
import sys
import pathlib

# Reuse the Java-scanning helpers already defined for the pipeline. This module
# has no side effects and creates no import cycle.
from JavaSourceScanning import (
    clean_lines,
    get_package,
    get_class_stack_at,
    index_members,
    innermost,
    parse_method_sig,
)


def _src_root() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get(
            "EVENTBUS_SRC_ROOT",
            "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src",
        )
    ).expanduser()


def fqcn_to_rel_file(fqcn: str) -> pathlib.Path:
    """Map a fully-qualified class name to its relative .java file path.

    The first uppercase-initial, dot-separated token is the outer class (Java
    convention); nested classes share the outer class's file.  Kept local to
    avoid an import cycle with RunSpeciminAll.
    """
    parts = fqcn.split('.')
    for i, part in enumerate(parts):
        if part and part[0].isupper():
            pkg_path = '/'.join(parts[:i])
            outer_class = parts[i]
            return pathlib.Path(pkg_path) / f"{outer_class}.java"
    return pathlib.Path(fqcn.replace('.', '/') + '.java')


def _deref_pattern(field_name: str) -> re.Pattern:
    """A regex matching a dereference of ``field_name``.

    Matches ``field.``, ``field[``, ``this.field.`` and ``this.field[`` but not
    ``other.field.`` (the negative lookbehind rejects a preceding ``.`` or word
    char), since a qualified access in a different class is unlikely to be the
    same field.
    """
    return re.compile(
        r'(?<![.\w$])(?:this\s*\.\s*)?' + re.escape(field_name) + r'\s*[.\[]'
    )


def _member_target_at(package, cleaned, idx, method_spans, field_spans):
    """Return (kind, target) for the member enclosing line ``idx``.

    ``kind`` is 'method' or 'field'; ``target`` is a Specimin-style
    'pkg.Class#name(...)' (method/constructor) or 'pkg.Class#field' string.
    Returns None when the line is not inside a nameable member (e.g. a static
    initializer block, which Specimin cannot target).
    """
    class_stack = get_class_stack_at(cleaned, idx)
    if not class_stack:
        return None
    fqcn = (package + '.' if package else '') + '.'.join(class_stack)

    mspan = innermost(method_spans, idx)
    fspan = innermost(field_spans, idx)

    # Prefer the deeper of the two: a reference inside a method that itself sits
    # inside a field initializer should be attributed to the method.
    if mspan is not None and (fspan is None or mspan[0] >= fspan[0]):
        name, params = parse_method_sig(mspan[2])
        if name:
            return 'method', f"{fqcn}#{name}({', '.join(params)})"
    if fspan is not None and fspan[2]:
        return 'field', f"{fqcn}#{fspan[2]}"
    return None


def _fields_of_class(cleaned, package, target_class_fqcn):
    """Names of the fields declared directly in ``target_class_fqcn``."""
    _methods, field_spans, _inits = index_members(cleaned)
    names = []
    for start, _end, name in field_spans:
        if not name:
            continue
        stack = get_class_stack_at(cleaned, start)
        if not stack:
            continue
        fqcn = (package + '.' if package else '') + '.'.join(stack)
        if fqcn == target_class_fqcn and name not in names:
            names.append(name)
    return names


def _member_span_for(cleaned, package, target_class_fqcn, primary_target):
    """Find the (start, end) span of the member named by ``primary_target``.

    ``primary_target`` is 'pkg.Class#name(params)' (method/constructor) or
    'pkg.Class#field'.  Returns None if it cannot be located.
    """
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

    if want_sig is None:
        for start, end, fname in field_spans:
            if fname != want_name:
                continue
            stack = get_class_stack_at(cleaned, start)
            fqcn = (package + '.' if package else '') + '.'.join(stack)
            if fqcn == target_class_fqcn:
                return start, end
    return None


def _fields_used_in_member(cleaned, package, target_class_fqcn, primary_target,
                           class_field_names):
    """Subset of ``class_field_names`` referenced inside the primary target.

    These are the fields whose nullability the slice's LLM step will actually
    reason about, so they are the ones whose dereferences are worth preserving.
    Falls back to all class fields if the member cannot be located.
    """
    span = _member_span_for(cleaned, package, target_class_fqcn, primary_target)
    if span is None:
        return list(class_field_names)
    start, end = span
    used = []
    # Count both unqualified (`f`) and `this.f` accesses, but not `other.f`
    # (which in this class is unlikely to be the same field).
    word_patterns = [
        (f, re.compile(r'(?<![.\w$])(?:this\s*\.\s*)?' + re.escape(f) + r'\b'))
        for f in class_field_names
    ]
    for idx in range(start, min(end, len(cleaned) - 1) + 1):
        line = cleaned[idx]
        if not line.strip():
            continue
        for f, pat in word_patterns:
            if f not in used and pat.search(line):
                used.append(f)
    return used


def _scan_file(rel_file, abs_file, field_names, target_class_fqcn,
               primary_target, results, seen):
    """Append (kind, rel_file, target) usage targets found in one file."""
    try:
        lines = abs_file.read_text(encoding='utf-8').splitlines()
    except OSError:
        return
    cleaned = clean_lines(lines)
    package = get_package(lines)
    method_spans, field_spans, _inits = index_members(cleaned)

    patterns = [(fname, _deref_pattern(fname)) for fname in field_names]

    for idx, line in enumerate(cleaned):
        if not line.strip():
            continue
        for fname, pat in patterns:
            if not pat.search(line):
                continue
            mt = _member_target_at(package, cleaned, idx, method_spans, field_spans)
            if mt is None:
                break
            kind, target = mt
            # Skip the run's own target and the field's own initializer.
            if target == primary_target:
                break
            if target == f"{target_class_fqcn}#{fname}":
                break
            key = (kind, str(rel_file), target)
            if key not in seen:
                seen.add(key)
                results.append(key)
            break  # one dereference per line is enough to preserve the member


def find_usage_targets(src_root, target_class_fqcn, target_rel_file,
                       primary_target, scope="class", fields="member"):
    """Find members that dereference a field of ``target_class_fqcn``.

    Parameters
    ----------
    src_root : pathlib.Path
        Java source root (the Specimin ``--root``).
    target_class_fqcn : str
        Fully-qualified name of the class being sliced.
    target_rel_file : pathlib.Path
        Path of the target class file, relative to ``src_root``.
    primary_target : str
        The run's own Specimin target, excluded from the result.
    scope : str
        ``class`` (default) scans only the declaring file for references; ``repo``
        scans every ``.java`` file under ``src_root`` (lower precision).
    fields : str
        Which fields to preserve usages of. ``member`` (default) restricts to the
        fields referenced inside ``primary_target`` — the ones the slice's LLM
        step actually reasons about, keeping the slice small. ``class`` widens to
        every field declared in the target class.

    Returns
    -------
    list[tuple[str, pathlib.Path, str]]
        (kind, rel_file, target) triples, deduplicated and order-preserving.
        ``rel_file`` must be passed to Specimin as an extra ``--targetFile`` so
        the target can be located.
    """
    src_root = pathlib.Path(src_root)
    decl_abs = src_root / target_rel_file
    if not decl_abs.exists():
        return []

    decl_lines = decl_abs.read_text(encoding='utf-8').splitlines()
    decl_cleaned = clean_lines(decl_lines)
    decl_package = get_package(decl_lines)
    field_names = _fields_of_class(decl_cleaned, decl_package, target_class_fqcn)
    if not field_names:
        return []

    member = primary_target.split('#', 1)[1] if '#' in primary_target else ''
    is_field_target = member and '(' not in member
    if is_field_target:
        # The target is itself a field: preserve dereferences of exactly that field.
        field_names = [member] if member in field_names else []
        if not field_names:
            return []
    elif fields == "member":
        field_names = _fields_used_in_member(
            decl_cleaned, decl_package, target_class_fqcn, primary_target, field_names
        )
        if not field_names:
            return []

    results, seen = [], set()

    if scope == "repo":
        files = sorted(src_root.rglob("*.java"))
    else:
        files = [decl_abs]

    for abs_file in files:
        try:
            rel_file = abs_file.relative_to(src_root)
        except ValueError:
            continue
        _scan_file(rel_file, abs_file, field_names, target_class_fqcn,
                   primary_target, results, seen)

    return results


# ── Standalone entry point (inspection / debugging) ─────────────────────────────

def _opt_value(flag, default):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


def main() -> None:
    scope = _opt_value("--scope", "class")
    fields = _opt_value("--fields", "member")
    consumed = {"--scope", "--fields", scope, fields}
    positionals = [a for a in sys.argv[1:] if a not in consumed]
    if not positionals:
        print("usage: PreserveUsages.py 'pkg.Class#member(...)' "
              "[--scope class|repo] [--fields member|class]")
        sys.exit(2)

    primary_target = positionals[0]
    fqcn = primary_target.split('#', 1)[0]
    src_root = _src_root()
    rel_file = fqcn_to_rel_file(fqcn)

    extras = find_usage_targets(
        src_root, fqcn, rel_file, primary_target, scope=scope, fields=fields
    )
    print(f"Target: {primary_target}")
    print(f"Root:   {src_root}")
    print(f"Scope:  {scope}   Fields: {fields}")
    print(f"Found {len(extras)} extra usage target(s):")
    for kind, erel, target in extras:
        print(f"  ({kind}) {target}    [{erel}]")


if __name__ == "__main__":
    main()
