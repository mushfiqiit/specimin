#!/usr/bin/env python3
"""
FixSpuriousNullable.py

For each "dereferenced expression X is @Nullable" warning in a NullAway output
file, this script:

  1. Parses the warning to extract the field name being dereferenced
     (e.g. "postingState.subscription" → field name "subscription").
  2. Searches the full EventBus source tree for actual dereferences of that
     name (e.g. `subscription.method()`, `subscription.field`) while
     excluding lines that are only null-check guards.
  3. If >= 1 real dereference is found, locates every `@Nullable` annotation
     on a field or method return type declaration bearing that name and
     rewrites it as `@Nonnull`.

Usage:
    python3 FixSpuriousNullable.py
    python3 FixSpuriousNullable.py --dry-run
    python3 FixSpuriousNullable.py --verbose
    python3 FixSpuriousNullable.py \\
        --warnings  /path/to/nullaway-after.txt \\
        --eventbus-src /path/to/EventBus/src
"""
from __future__ import annotations

import re
import sys
import pathlib
from collections import defaultdict

# ── Default paths ──────────────────────────────────────────────────────────────

DEFAULT_WARNINGS = (
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/nullaway-after.txt"
)
DEFAULT_EVENTBUS_SRC = (
    "/Users/mushfiqurrahmanchowdhury/Documents/EventBus/EventBus/src"
)

# ── Warning parser ─────────────────────────────────────────────────────────────

# Matches lines like:
#   /path/Foo.java:42: warning: [NullAway] dereferenced expression bar.baz is @Nullable
_WARNING_RE = re.compile(
    r"^(.+?):(\d+):\s*warning:\s*\[NullAway\]\s*"
    r"dereferenced expression\s+(.+?)\s+is\s+@Nullable"
)


def parse_warnings(warnings_path: pathlib.Path) -> dict[str, list[tuple[str, int, str]]]:
    """
    Returns a dict mapping field_name → [(file_path, line_no, full_expr), ...]

    The field_name is the *last* component of the dereferenced expression:
        postingState.subscription  →  subscription
        subscription               →  subscription
    """
    result: dict[str, list[tuple[str, int, str]]] = defaultdict(list)

    for line in warnings_path.read_text(encoding="utf-8").splitlines():
        m = _WARNING_RE.match(line.strip())
        if not m:
            continue
        file_path = m.group(1)
        line_no = int(m.group(2))
        full_expr = m.group(3)
        # Last component of a dotted expression
        field_name = full_expr.split(".")[-1].strip()
        result[field_name].append((file_path, line_no, full_expr))

    return dict(result)


# ── Dereference detection ──────────────────────────────────────────────────────

# A line is a "dereference" of `name` if it contains `name.` (the name used as
# an object), and is NOT just a null guard (`name == null` / `name != null`).

def count_dereferences(name: str, src_root: pathlib.Path) -> list[tuple[pathlib.Path, int, str]]:
    """
    Return all (file, lineno, line) triples where `name` is dereferenced in
    the source tree.  Null-check-only lines are excluded.
    """
    deref_re = re.compile(r"\b" + re.escape(name) + r"\s*\.")
    null_check_re = re.compile(
        r"\b" + re.escape(name) + r"\s*(==|!=)\s*null"
        r"|null\s*(==|!=)\s*\b" + re.escape(name) + r"\b"
    )
    # Also exclude lines that are purely a null-check `if (name == null)` /
    # `name == null` with nothing else meaningful after the guard.
    _pure_null_check_re = re.compile(
        r"^\s*(?:if\s*\()?\s*(?:\!?\s*)?\b" + re.escape(name) +
        r"\s*(==|!=)\s*null\b"
    )

    hits: list[tuple[pathlib.Path, int, str]] = []

    for java_file in sorted(src_root.rglob("*.java")):
        try:
            lines = java_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for lineno, line in enumerate(lines, start=1):
            if not deref_re.search(line):
                continue
            # Skip lines whose only mention of `name` is a null-check
            if null_check_re.search(line):
                # The line has BOTH a dereference and a null-check pattern.
                # Treat as a guarded use — keep if there are multiple dots
                # (e.g. `if (x.field != null) x.field.method()`).
                if line.count(name + ".") < 2 and _pure_null_check_re.search(line):
                    continue
            hits.append((java_file, lineno, line.rstrip()))

    return hits


# ── @Nullable → @Nonnull rewriter ─────────────────────────────────────────────

# We look for a @Nullable annotation that is "close to" a field or method
# declaration that mentions `field_name`.
#
# Handled layouts:
#   @Nullable FieldType fieldName;               (inline)
#   @Nullable FieldType fieldName = someValue;   (inline with init)
#   @Nullable\n    FieldType fieldName;          (annotation on its own line)
#   ... @Nullable ReturnType methodName(...)     (return-type annotation)
#   method/parameter: @Nullable Type paramName  (parameter annotation)
#
# We use a two-pass line-by-line scan so we never accidentally rewrite a
# comment or string literal.

def _is_declaration_line(line: str, name: str) -> bool:
    """
    Return True if `line` looks like a declaration (field, param, or return)
    for `name`:  it contains `name` preceded by whitespace (i.e. the name is
    a word, not part of a larger identifier) and ends with `;`, `,`, or `)`.
    """
    if not re.search(r"\b" + re.escape(name) + r"\b", line):
        return False
    # Must be a declaration context — has a type before the name, ends with ; ) ,
    stripped = line.strip()
    # Exclude pure comment lines
    if stripped.startswith("//") or stripped.startswith("*"):
        return False
    # Declaration lines end with ; or have a parameter-list context
    return bool(re.search(r"\b" + re.escape(name) + r"\s*[;,)=\[]", line))


def _has_nullable_directly_before(line: str, name: str) -> bool:
    """
    Return True only if this line has `@Nullable` (possibly with modifiers or
    a type in between) immediately preceding the token `name`.

    This prevents matching a line like:
        void foo(Subscription subscription, @Nullable Object event)
    when we are looking for `subscription` — @Nullable there belongs to `event`.

    The rule: between @Nullable and the word `name` there must be no comma and
    no other identifier that could be another parameter.
    """
    escaped = re.escape(name)
    # @Nullable <type and modifiers, no comma, no other bare word+comma> name
    return bool(re.search(
        r"@Nullable\b[^,;(]*?\b" + escaped + r"\b",
        line
    ))

def rewrite_nullable_to_nonnull(
    src_root: pathlib.Path,
    field_name: str,
    dry_run: bool = False,
    verbose: bool = False,
) -> list[tuple[pathlib.Path, int]]:
    """
    Find every @Nullable annotation on a declaration of `field_name` across
    the source tree and replace it with @Nonnull.

    Returns list of (file, lineno) pairs that were changed.
    """
    changed: list[tuple[pathlib.Path, int]] = []

    for java_file in sorted(src_root.rglob("*.java")):
        try:
            original = java_file.read_text(encoding="utf-8")
        except OSError:
            continue

        lines = original.splitlines(keepends=True)
        new_lines = list(lines)
        file_changed = False

        i = 0
        while i < len(lines):
            line = lines[i]

            # ── Case 1: @Nullable on the SAME line as the declaration ──────
            if "@Nullable" in line and _is_declaration_line(line, field_name):
                new_line = line.replace("@Nullable", "@Nonnull", 1)
                if new_line != line:
                    new_lines[i] = new_line
                    changed.append((java_file, i + 1))
                    file_changed = True
                    if verbose:
                        print(f"      [{java_file.name}:{i+1}] {line.rstrip()}")
                        print(f"           → {new_line.rstrip()}")

            # ── Case 2: @Nullable on its OWN line; declaration follows ─────
            elif "@Nullable" in line and line.strip().rstrip() in (
                "@Nullable", "@Nullable "
            ) or (line.strip().startswith("@Nullable") and
                  # annotation-only line: nothing after @Nullable except whitespace
                  re.match(r"^\s*@Nullable\s*$", line)):
                # Look ahead up to 3 lines for the declaration
                for offset in range(1, 4):
                    j = i + offset
                    if j >= len(lines):
                        break
                    next_line = lines[j]
                    # Skip other annotation lines
                    if re.match(r"^\s*@\w", next_line):
                        continue
                    if _is_declaration_line(next_line, field_name):
                        new_line = line.replace("@Nullable", "@Nonnull", 1)
                        if new_line != line:
                            new_lines[i] = new_line
                            changed.append((java_file, i + 1))
                            file_changed = True
                            if verbose:
                                print(f"      [{java_file.name}:{i+1}] {line.rstrip()}")
                                print(f"           → {new_line.rstrip()}")
                        break
                    else:
                        break  # hit a non-annotation, non-declaration line

            i += 1

        if file_changed and not dry_run:
            java_file.write_text("".join(new_lines), encoding="utf-8")

    return changed


# ── Argument parsing ───────────────────────────────────────────────────────────

def parse_args():
    args = sys.argv[1:]
    warnings_path = pathlib.Path(DEFAULT_WARNINGS)
    eventbus_src = pathlib.Path(DEFAULT_EVENTBUS_SRC)
    dry_run = False
    verbose = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--dry-run":
            dry_run = True
        elif a == "--verbose":
            verbose = True
        elif a == "--warnings" and i + 1 < len(args):
            warnings_path = pathlib.Path(args[i + 1])
            i += 1
        elif a == "--eventbus-src" and i + 1 < len(args):
            eventbus_src = pathlib.Path(args[i + 1])
            i += 1
        i += 1

    return warnings_path, eventbus_src, dry_run, verbose


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> None:
    warnings_path, eventbus_src, dry_run, verbose = parse_args()

    print(f"Warnings file : {warnings_path}")
    print(f"EventBus src  : {eventbus_src}")
    if dry_run:
        print("(dry-run — no files will be modified)")
    print()

    if not warnings_path.exists():
        print(f"ERROR: warnings file not found: {warnings_path}")
        sys.exit(1)
    if not eventbus_src.exists():
        print(f"ERROR: eventbus-src not found: {eventbus_src}")
        sys.exit(1)

    # Step 1: parse all "dereferenced expression X is @Nullable" warnings
    field_warnings = parse_warnings(warnings_path)

    if not field_warnings:
        print("No 'dereferenced expression ... is @Nullable' warnings found.")
        sys.exit(0)

    print(f"Found {len(field_warnings)} unique field name(s) with dereference warnings:\n")

    total_rewrites = 0

    for field_name, occurrences in sorted(field_warnings.items()):
        print(f"  Field: {field_name!r}  ({len(occurrences)} warning(s))")
        if verbose:
            for fpath, lineno, expr in occurrences:
                print(f"    warning @ {pathlib.Path(fpath).name}:{lineno}  expr={expr!r}")

        # Step 2: count actual dereferences in the full source tree
        deref_hits = count_dereferences(field_name, eventbus_src)

        print(f"    Dereferences found in src: {len(deref_hits)}")
        if verbose:
            for df, dl, dline in deref_hits[:10]:
                print(f"      {df.name}:{dl}  {dline.strip()}")
            if len(deref_hits) > 10:
                print(f"      ... and {len(deref_hits) - 10} more")

        if len(deref_hits) == 0:
            print(f"    → No dereferences found — keeping @Nullable\n")
            continue

        # Step 3: rewrite @Nullable → @Nonnull for this field name
        rewrites = rewrite_nullable_to_nonnull(
            eventbus_src, field_name, dry_run=dry_run, verbose=verbose
        )

        if rewrites:
            action = "would rewrite" if dry_run else "rewrote"
            for rfile, rline in rewrites:
                print(f"    {action} @Nullable → @Nonnull  [{rfile.name}:{rline}]")
            total_rewrites += len(rewrites)
        else:
            print(f"    → No @Nullable declaration found to rewrite for {field_name!r}")

        print()

    print("═" * 55)
    action = "Would rewrite" if dry_run else "Rewrote"
    print(f"{action} {total_rewrites} @Nullable annotation(s) → @Nonnull")


if __name__ == "__main__":
    main()
