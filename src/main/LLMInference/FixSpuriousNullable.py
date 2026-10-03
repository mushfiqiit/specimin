#!/usr/bin/env python3
"""
FixSpuriousNullable.py

Post-merge clean-up for the JUnit 4 pipeline: for every
"dereferenced expression X is @Nullable" warning in a NullAway warnings file
(e.g. the one GenerateNullAwayWarnings.sh writes after the LLM annotations
were merged), decide whether the @Nullable that causes it is SPURIOUS, and if
so rewrite it to @Nonnull in the original source.

Adapted from the LLMInference branch's PostAnalysis/FixSpuriousNullable.py
(EventBus). That version flipped every @Nullable declaration named like the
dereferenced expression as soon as the name was dereferenced anywhere. On
JUnit that re-creates the oscillation seen between merge rounds: a method that
really does `return null` gets @Nonnull, and NullAway moves the warning to its
`return null`. This version:

  1. Resolves the dereferenced expression to ONE declaration: the method a
     call resolves to (receiver type and overloads considered), the field, or
     the parameter of the enclosing method. Local variables are reported and
     left alone (they have no declaration annotation to change).
  2. Only considers declarations that currently carry @Nullable.
  3. Keeps @Nullable when the original source shows the declaration really
     can be null ("evidence"), and flips it only when there is none:
       method  some `return E;` in its body where E can be null
       field   some assignment / initializer `f = E` where E can be null
       param   some call site passes an argument that can be null
     "E can be null" means: the literal null, a ternary with a null branch, a
     @Nullable parameter/field, a local assigned from such an expression, a
     call to a project method declared @Nullable, or a JDK call that returns
     null by contract (Map.get, getAnnotation, getCause, System.getProperty,
     ...). Without evidence, the @Nullable is spurious.
  4. Optionally (--verify) re-runs NullAway after each flip and keeps the flip
     only if the total number of warnings goes DOWN; otherwise it is reverted.
     This is the ground truth; the evidence search above is a heuristic that
     decides which flips are worth trying.

Usage:
    python3 FixSpuriousNullable.py --dry-run          # report only
    python3 FixSpuriousNullable.py                    # flip the spurious ones
    python3 FixSpuriousNullable.py --verify           # flip one at a time, keep only if NullAway improves
    python3 FixSpuriousNullable.py --verbose --report fsn.tsv

Options:
    --warnings FILE   NullAway warnings file
                      (default: ~/Documents/junit4/nullaway-warnings_after.txt)
    --src-root DIR    original source root (default: ~/Documents/junit4/src/main/java)
    --dry-run         change nothing
    --verify          verify each flip with a NullAway run (needs JDK 21 and
                      GenerateNullAwayWarnings.sh; takes one build per flip)
    --verify-cmd CMD  command that runs NullAway and leaves nullaway-warnings*.txt
                      and nullaway-report*.txt in $OUT_DIR (default:
                      bash ../SpeciminPerformanceEvaluation/GenerateNullAwayWarnings.sh)
    --report FILE     also write every decision as a tab-separated file
    --verbose         print the evidence for each decision

Run it in the junit4 checkout's git state you want to change, and inspect the
result with `git diff` (it edits the original sources in place).
"""
from __future__ import annotations

import os
import re
import sys
import shlex
import shutil
import pathlib
import tempfile
import subprocess
from collections import OrderedDict

from ExtractUsageContext import (
    JavaFile,
    Member,
    Repo,
    blank_strings,
    calls_member,
    enclosing_call,
    enclosing_method,
    receiver_class,
    resolve_field,
    resolve_method,
    statement_at,
    trailing_member,
)

JUNIT_DIR = pathlib.Path("~/Documents/junit4").expanduser()
DEFAULT_WARNINGS = JUNIT_DIR / "nullaway-warnings_after.txt"
DEFAULT_SRC_ROOT = JUNIT_DIR / "src" / "main" / "java"
DEFAULT_VERIFY_CMD = "bash " + shlex.quote(str(
    pathlib.Path(__file__).resolve().parent.parent
    / "SpeciminPerformanceEvaluation" / "GenerateNullAwayWarnings.sh"))

_DEREF_RE = re.compile(
    r"^(.+?\.java):(\d+):\s*(?:warning|error):\s*\[NullAway\]\s*"
    r"dereferenced expression\s+(.+?)\s+is\s+@Nullable"
)
_NULLABLE_RE = re.compile(r"@(?:javax\.annotation\.)?Nullable\b")
_ANY_NULLABLE_RE = re.compile(r"@(?:[\w.]+\.)?Nullable\b")

# JDK methods that return null by contract (receiver outside the project).
_JDK_NULLABLE_CALLS = frozenset({
    "get", "remove", "put", "getOrDefault",            # Map
    "poll", "peek", "pollFirst", "pollLast", "peekFirst", "peekLast",
    "getAnnotation", "getDeclaredAnnotation", "getEnclosingClass",
    "getDeclaringClass", "getSuperclass", "getComponentType",
    "getCause", "getMessage", "getLocalizedMessage",
    "getProperty", "getenv", "getResource", "getResourceAsStream",
    "getParent", "getParentFile", "readLine", "listFiles", "list",
})


# ── Declarations ───────────────────────────────────────────────────────────────

class Decl:
    """One annotatable declaration: a method's return, a field, or a
    method/constructor parameter."""

    def __init__(self, kind, jf: JavaFile, d, param_index=None):
        self.kind, self.jf, self.d, self.param_index = kind, jf, d, param_index

    @property
    def name(self) -> str:
        if self.kind == "param":
            return self.d[5][self.param_index]
        return self.d[0]

    def key(self):
        return self.kind, self.jf.rel, self.d[2], self.param_index

    def label(self) -> str:
        owner = (self.d[1] or "?").split(".")[-1]
        if self.kind == "field":
            return f"field {owner}.{self.d[0]}"
        sig = f"{owner}.{self.d[0]}({', '.join(self.d[4])})"
        if self.kind == "param":
            return f"parameter '{self.name}' of {sig}"
        return f"return of {sig}"

    def header_lines(self) -> range:
        start, end = self.d[2], self.d[3]
        if self.kind == "field":
            return range(start, end + 1)
        return range(start, self.jf.header_end(start, end) + 1)

    def has_nullable(self) -> bool:
        return self.annotation_position() is not None

    def annotation_position(self):
        """(line index, column) of this declaration's @Nullable, or None."""
        lines = self.jf.lines
        rows = list(self.header_lines())
        text = "\n".join(lines[i] for i in rows)
        offsets, pos = [], 0
        for i in rows:
            offsets.append((pos, i))
            pos += len(lines[i]) + 1

        def to_line_col(p):
            for off, i in reversed(offsets):
                if p >= off:
                    return i, p - off
            return rows[0], p

        name_m = None
        if self.kind == "field":
            name_m = re.search(r"\b" + re.escape(self.name) + r"\b\s*(=|;|,|\[)", text)
            limit = name_m.start() if name_m else len(text)
            region = (0, limit)
        else:
            call = re.search(r"\b" + re.escape(self.d[0]) + r"\s*\(", text)
            if not call:
                return None
            if self.kind == "method":
                region = (0, call.start())
            else:
                params_start = call.end()
                spans, depth, start = [], 0, params_start
                for j in range(params_start, len(text)):
                    c = text[j]
                    if c in "(<[":
                        depth += 1
                    elif c in ")>]":
                        if depth == 0:
                            spans.append((start, j))
                            break
                        depth -= 1
                    elif c == "," and depth == 0:
                        spans.append((start, j))
                        start = j + 1
                if self.param_index >= len(spans):
                    return None
                region = spans[self.param_index]
        for m in _NULLABLE_RE.finditer(text, region[0], region[1]):
            line_idx, col = to_line_col(m.start())
            # skip occurrences inside comments
            if "Nullable" in self.jf.cleaned[line_idx]:
                return line_idx, col
        return None


def resolve_decl(repo: Repo, jf: JavaFile, idx: int, expr: str):
    """(Decl or None, note) for the declaration behind a dereferenced expression."""
    tm = trailing_member(expr)
    if tm is None:
        return None, "could not parse the expression"
    enc = enclosing_method(jf, idx)
    kind, name, receiver = tm
    if kind == "method":
        stmt = statement_at(jf, idx)
        call = None
        pos = blank_strings(stmt).find(name + "(")
        if pos >= 0:
            call = enclosing_call(stmt, stmt.find("(", pos) + 1)
        rcls = receiver_class(repo, jf, idx, enc, receiver)
        if rcls is None:
            return None, f"{name}() is a method of a type outside the project"
        decls = resolve_method(repo, jf, name, call[3] if call else None, enc=enc,
                               receiver_cls=rcls)
        if not decls:
            return None, f"no project declaration of {name}() found"
        if len(decls) > 1:
            return None, f"{name}() is ambiguous ({len(decls)} declarations)"
        f, d = decls[0]
        return Decl("method", f, d), ""
    # a plain name: parameter, local, or field
    if receiver in ("", "this") and enc is not None and name in enc[5]:
        return Decl("param", jf, enc, enc[5].index(name)), ""
    if receiver == "" and enc is not None:
        local = re.compile(r"[\w>\]]\s+" + re.escape(name) + r"\s*(=|;|:)")
        if any(local.search(jf.cleaned[i]) for i in range(enc[2] + 1, enc[3] + 1)):
            return None, f"'{name}' is a local variable (no declaration annotation)"
    decls = resolve_field(repo, jf, name)
    if len(decls) == 1:
        f, d = decls[0]
        fdecl = [x for x in f.fields if x[2] == d[2] and x[0] == d[0]]
        return Decl("field", f, fdecl[0]), ""
    if not decls:
        return None, f"no field '{name}' found"
    return None, f"field '{name}' is ambiguous ({len(decls)} declarations)"


# ── Is an expression nullable? ─────────────────────────────────────────────────

def _strip_parens(e: str) -> str:
    e = e.strip()
    while e.startswith("(") and e.endswith(")"):
        depth = 0
        for i, c in enumerate(e):
            depth += c == "("
            depth -= c == ")"
            if depth == 0 and i < len(e) - 1:
                return e
        e = e[1:-1].strip()
    return e


def decl_of_name(repo: Repo, jf: JavaFile, idx: int, name: str):
    enc = enclosing_method(jf, idx)
    if enc is not None and name in enc[5]:
        return Decl("param", jf, enc, enc[5].index(name))
    decls = [(f, d) for f, d in resolve_field(repo, jf, name)]
    if len(decls) == 1:
        f, d = decls[0]
        return Decl("field", f, d)
    return None


def nullable_expr(repo: Repo, jf: JavaFile, idx: int, expr: str, depth: int = 0):
    """A short reason string if expr (at line idx of jf) can evaluate to null,
    else None."""
    e = _strip_parens(blank_strings(expr))
    e = re.sub(r"^\(\s*[\w$.<>\[\]?, ]+\)\s*(?=[\w$(])", "", e).strip()  # cast
    if not e or depth > 3:
        return None
    if e == "null":
        return "null literal"
    if re.search(r"\?\s*null\s*:|:\s*null\s*$", e):
        return "conditional with a null branch"
    enc = enclosing_method(jf, idx)
    if re.fullmatch(r"(this\.)?[A-Za-z_$][\w$]*", e):
        name = e.split(".")[-1]
        if enc is not None and name not in enc[5]:
            # a local: nullable if some assignment to it is
            assign = re.compile(r"(?<![\w$.])" + re.escape(name) + r"\s*=(?!=)\s*(.+?);")
            for i in range(enc[2] + 1, min(idx, enc[3]) + 1):
                m = assign.search(blank_strings(jf.lines[i]))
                if m:
                    why = nullable_expr(repo, jf, i, m.group(1), depth + 1)
                    if why:
                        return f"local '{name}' assigned {why} (line {i + 1})"
        d = decl_of_name(repo, jf, idx, name)
        if d is not None and d.has_nullable():
            return f"{d.label()} is @Nullable"
        return None
    tm = trailing_member(e)
    if tm and tm[0] == "method":
        _kind, name, receiver = tm
        rcls = receiver_class(repo, jf, idx, enc, receiver)
        if rcls is None:
            if name in _JDK_NULLABLE_CALLS:
                return f"JDK {name}() may return null"
            return None
        call = None
        pos = e.rfind(name + "(")
        if pos >= 0:
            call = enclosing_call(e, e.find("(", pos) + 1)
        decls = resolve_method(repo, jf, name, call[3] if call else None, enc=enc,
                               receiver_cls=rcls)
        if decls and any(Decl("method", f, d).has_nullable() for f, d in decls):
            return f"{name}() is declared @Nullable"
        if not decls and name in _JDK_NULLABLE_CALLS:
            return f"{name}() may return null"
    elif tm and tm[0] == "name" and tm[2]:
        d = None
        decls = resolve_field(repo, jf, tm[1])
        if len(decls) == 1:
            d = Decl("field", decls[0][0], decls[0][1])
        if d is not None and d.has_nullable():
            return f"{d.label()} is @Nullable"
    return None


def evidence(repo: Repo, decl: Decl):
    """(file, 1-based line, text, reason) showing decl can really be null, or None."""
    jf, d = decl.jf, decl.d
    if decl.kind == "method":
        for i in range(d[2], d[3] + 1):
            for m in re.finditer(r"\breturn\b(.+?);", blank_strings(jf.lines[i])):
                why = nullable_expr(repo, jf, i, m.group(1))
                if why:
                    return jf.rel, i + 1, jf.lines[i].strip(), why
        return None
    if decl.kind == "field":
        assign = re.compile(r"(?<![\w$])(?:this\s*\.\s*)?" + re.escape(decl.name)
                            + r"\s*=(?!=)\s*(.+?)[;,]")
        owner = (d[1] or "").split(".")[-1]
        qualified = re.compile(r"\.\s*" + re.escape(decl.name) + r"\s*=(?!=)\s*(.+?);")
        for f in repo.files():
            if f is not jf and owner not in f.text:
                continue
            pat = assign if f is jf else qualified
            for i, line in enumerate(f.cleaned):
                if decl.name not in line:
                    continue
                for m in pat.finditer(blank_strings(f.lines[i])):
                    why = nullable_expr(repo, f, i, m.group(1))
                    if why:
                        return f.rel, i + 1, f.lines[i].strip(), why
        return None
    # parameter: some call site passes a nullable argument
    member = Member("method", d[0], [(jf, d)], "", is_ctor=pathlib.Path(jf.rel).stem == d[0]
                    and (d[1] or "").endswith("." + d[0]))
    if member.is_ctor:
        pat = re.compile(r"\bnew\s+" + re.escape(d[0]) + r"\s*(<[^>]*>)?\s*\(|\b(this|super)\s*\(")
    else:
        pat = re.compile(r"(?<![\w$])" + re.escape(d[0]) + r"\s*\(")
    for f in repo.files():
        if d[0] not in f.text:
            continue
        for i, line in enumerate(f.cleaned):
            for m in pat.finditer(line):
                if f is jf and d[2] <= i <= jf.header_end(d[2], d[3]):
                    continue  # the declaration itself
                if not calls_member(repo, member, f, i, m):
                    continue
                text = blank_strings(f.lines[i])
                paren = text.find("(", m.start())
                call = enclosing_call(text, paren + 1) if paren >= 0 else None
                if not call or call[3] is None or decl.param_index >= len(call[3]):
                    continue
                arg = call[3][decl.param_index]
                why = nullable_expr(repo, f, i, arg)
                if why:
                    return f.rel, i + 1, f.lines[i].strip(), f"argument '{arg}': {why}"
    return None


# ── Editing ────────────────────────────────────────────────────────────────────

def flip(decl: Decl) -> tuple[str, str]:
    """Rewrites decl's @Nullable to @Nonnull in its file and adds the import.
    Returns (path, original text) so the change can be reverted."""
    path = decl.jf.path
    original = path.read_text(encoding="utf-8")
    pos = decl.annotation_position()
    if pos is None:
        raise ValueError(f"no @Nullable found on {decl.label()}")
    line_idx, col = pos
    lines = original.split("\n")
    line = lines[line_idx]
    m = _NULLABLE_RE.match(line, col)
    replacement = "@javax.annotation.Nonnull" if "javax.annotation" in m.group(0) else "@Nonnull"
    lines[line_idx] = line[:m.start()] + replacement + line[m.end():]
    text = "\n".join(lines)
    if replacement == "@Nonnull" and not re.search(
            r"^import\s+javax\.annotation\.(Nonnull|\*)\s*;", text, re.M):
        imports = list(re.finditer(r"^import\s+[\w.*]+\s*;[^\n]*$", text, re.M))
        if imports:
            at = imports[-1].end()
            text = text[:at] + "\nimport javax.annotation.Nonnull;" + text[at:]
        else:
            pkg = re.search(r"^package\s+[\w.]+\s*;[^\n]*$", text, re.M)
            at = pkg.end() if pkg else 0
            text = text[:at] + "\n\nimport javax.annotation.Nonnull;" + text[at:]
    path.write_text(text, encoding="utf-8")
    return str(path), original


# ── Verification with NullAway ─────────────────────────────────────────────────

def run_nullaway(cmd: str, src_root: pathlib.Path, verbose: bool):
    """Number of NullAway warnings, or None if the build did not compile."""
    out_dir = pathlib.Path(tempfile.mkdtemp(prefix="fsn-nullaway-"))
    env = dict(os.environ, OUT_DIR=str(out_dir), JUNIT_SRC_ROOT=str(src_root))
    proc = subprocess.run(cmd, shell=True, env=env, capture_output=True, text=True)
    try:
        reports = sorted(out_dir.glob("nullaway-report*.txt"))
        warnings = sorted(out_dir.glob("nullaway-warnings*.txt"))
        if verbose:
            print("      " + "\n      ".join((proc.stdout + proc.stderr).strip().splitlines()[-3:]))
        if not warnings:
            return None
        report = reports[0].read_text(encoding="utf-8", errors="replace") if reports else ""
        if re.search(r"^\S+\.java:\d+: error:", report, re.M) or "BUILD FAILED" in report:
            return None
        return sum(1 for l in warnings[0].read_text(encoding="utf-8").splitlines()
                   if "[NullAway]" in l)
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


# ── Main ───────────────────────────────────────────────────────────────────────

def _opt(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        print(f"ERROR: {flag} needs a value")
        sys.exit(2)
    return default


def to_source_path(src_root: pathlib.Path, recorded: str) -> pathlib.Path | None:
    p = pathlib.Path(recorded)
    try:
        return src_root / p.relative_to(src_root)
    except ValueError:
        pass
    parts = p.parts
    if "java" in parts:
        rel = pathlib.Path(*parts[len(parts) - parts[::-1].index("java"):])
        return src_root / rel
    return None


def main() -> None:
    warnings = pathlib.Path(_opt("--warnings", str(DEFAULT_WARNINGS))).expanduser()
    src_root = pathlib.Path(_opt("--src-root", str(DEFAULT_SRC_ROOT))).expanduser().resolve()
    report_file = _opt("--report")
    verify_cmd = _opt("--verify-cmd", DEFAULT_VERIFY_CMD)
    dry_run = "--dry-run" in sys.argv
    verify = "--verify" in sys.argv
    verbose = "--verbose" in sys.argv

    print(f"Warnings file : {warnings}")
    print(f"Source root   : {src_root}")
    if dry_run:
        print("(dry-run — no files will be modified)")
    elif verify:
        print(f"(verify — each flip is checked with: {verify_cmd})")
    for p, what in ((warnings, "warnings file"), (src_root, "source root")):
        if not p.exists():
            print(f"ERROR: {what} not found: {p}")
            sys.exit(1)

    repo = Repo(src_root)
    rows = []                      # (status, warning, declaration, detail)
    candidates: "OrderedDict[tuple, list]" = OrderedDict()   # key -> [Decl, warnings]
    n_deref = 0
    for raw in warnings.read_text(encoding="utf-8").splitlines():
        m = _DEREF_RE.match(raw.strip())
        if not m:
            continue
        n_deref += 1
        path = to_source_path(src_root, m.group(1))
        where = f"{path.relative_to(src_root) if path else m.group(1)}:{m.group(2)}"
        expr = m.group(3)
        jf = repo.file(path) if path else None
        if jf is None:
            rows.append(("file not found", where, expr, ""))
            continue
        decl, note = resolve_decl(repo, jf, int(m.group(2)) - 1, expr)
        if decl is None:
            rows.append(("skipped", where, expr, note))
            continue
        if not decl.has_nullable():
            rows.append(("not @Nullable", where, decl.label(),
                         "the declaration has no @Nullable; the null comes from elsewhere"))
            continue
        candidates.setdefault(decl.key(), [decl, []])[1].append(f"{where} ({expr})")

    print(f"\n{n_deref} 'dereferenced expression ... is @Nullable' warning(s), "
          f"{len(candidates)} @Nullable declaration(s) behind them\n")

    to_flip = []
    for _key, (decl, ws) in candidates.items():
        ev = evidence(repo, decl)
        print(f"── {decl.label()}  [{decl.jf.rel}:{decl.d[2] + 1}]")
        for w in ws:
            print(f"     warning : {w}")
        if ev:
            f, line, text, why = ev
            print(f"     KEEP @Nullable — genuinely nullable: {why}")
            print(f"       {f}:{line}: {text[:110]}")
            for w in ws:
                rows.append(("kept (nullable)", w, decl.label(), f"{why} @ {f}:{line}"))
        else:
            print("     spurious @Nullable (no evidence it can be null) → @Nonnull")
            to_flip.append((decl, ws))

    flipped, reverted, failed = [], [], []
    if to_flip and not dry_run:
        current = None
        if verify:
            print("\nRunning NullAway for the baseline ...")
            current = run_nullaway(verify_cmd, src_root, verbose)
            if current is None:
                print("ERROR: the baseline NullAway run did not compile or produced no "
                      "warnings file. Check --verify-cmd / JAVA_HOME.")
                sys.exit(1)
            print(f"  baseline: {current} warning(s)")
        for decl, ws in to_flip:
            try:
                path, original = flip(decl)
            except (ValueError, OSError) as e:
                failed.append((decl, str(e)))
                continue
            if verify:
                print(f"  trying {decl.label()} ...")
                count = run_nullaway(verify_cmd, src_root, verbose)
                if count is not None and count < current:
                    print(f"    kept: {current} → {count} warning(s)")
                    current = count
                else:
                    pathlib.Path(path).write_text(original, encoding="utf-8")
                    print(f"    reverted: {current} → "
                          f"{'build failed' if count is None else count} warning(s)")
                    reverted.append((decl, ws, count))
                    repo = Repo(src_root)
                    continue
            flipped.append((decl, ws))
            repo = Repo(src_root)   # re-parse: the file changed

    for decl, ws in to_flip:
        if dry_run:
            status = "would flip"
        elif any(d is decl for d, _ in flipped):
            status = "flipped" + (" (verified)" if verify else "")
        elif any(d is decl for d, _w, _c in reverted):
            status = "reverted (NullAway not improved)"
        else:
            status = "flip failed"
        for w in ws:
            rows.append((status, w, decl.label(), "no evidence of null"))

    skipped = [r for r in rows if r[0] in ("skipped", "not @Nullable", "file not found")]
    if skipped:
        print("\nNot handled:")
        for _status, where, what, note in skipped:
            print(f"  {where}  {what}: {note}")

    if report_file:
        with open(report_file, "w", encoding="utf-8") as out:
            out.write("status\twarning\tdeclaration\tdetail\n")
            for r in rows:
                out.write("\t".join(r) + "\n")

    print(f"\n{'═' * 60}")
    print(f"Dereference warnings              : {n_deref}")
    print(f"@Nullable declarations behind them: {len(candidates)}")
    print(f"  kept (evidence of null)         : {len(candidates) - len(to_flip)}")
    print(f"  spurious → @Nonnull             : {len(to_flip)}"
          + (" (dry run)" if dry_run else ""))
    if verify and not dry_run:
        print(f"    kept after NullAway check     : {len(flipped)}")
        print(f"    reverted                      : {len(reverted)}")
    if failed:
        print(f"    failed                        : {len(failed)}")
        for decl, e in failed:
            print(f"      - {decl.label()}: {e}")
    print(f"Not handled (locals, non-project types, ...): "
          f"{sum(1 for r in rows if r[0] in ('skipped', 'not @Nullable', 'file not found'))}")
    if report_file:
        print(f"Report written to {report_file}")


if __name__ == "__main__":
    main()
