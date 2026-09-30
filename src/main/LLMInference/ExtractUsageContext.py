#!/usr/bin/env python3
"""
ExtractUsageContext.py

For every slice folder under SPECIMIN_OUT that has a root-warning.txt (written
by ExtractRootWarning.py), finds the field(s) and/or method(s) that warning is
about, collects how they are declared and used in the ORIGINAL, full source
tree (not the slice), and writes the excerpts to usage-context.txt in that
same slice folder. RunLLMInferenceAll.py splices usage-context.txt into the
slice's prompt as read-only evidence.

Adapted from the LLMInference branch's LLMInferencePython/ExtractUsageContext.py,
with three differences:
  1. Only the member(s) related to the slice's root warning are used, not every
     field the slice happens to contain.
  2. usage-context.txt is written into the slice folder whose root warning it
     is based on.
  3. Usages are searched in the original repository. The original location of
     the warning comes from the slice's warning.txt, and the source root from
     its root.txt (both written by SpeciminPerformanceEvaluation/RunSpeciminAll.py).

Which member(s) a warning is about is decided from the NullAway message and
the source line it points at:

  dereferenced expression E is @Nullable
      the method E calls last (e.g. `a.getFoo()` -> getFoo), or the field E
      names; if E is a parameter of the enclosing method, that method (its
      callers supply the value)
  returning @Nullable expression from method with @NonNull return type
      the enclosing method (how callers use the result), plus the returned
      field, if a field is returned
  passing @Nullable parameter 'X' where @NonNull is required
      the method/constructor receiving X (its other call sites, and how the
      parameter is used in its body), plus X itself if it is a field or call
  assigning @Nullable expression to @NonNull field
      the field on the left-hand side
  initializer method does not guarantee @NonNull field F (line N) ...,
  @NonNull [static] field F not initialized, read of @NonNull field F ...
      field F
  method returns @Nullable, but superclass method C.m(...) returns @NonNull
      method m (overrides and call sites)
  anything else (e.g. unboxing of a @Nullable value)
      the fields and methods declared in the same file that appear on the
      warning line

Lines inside the warning's own enclosing member are left out (that member is
already in the slice), except the member's declaration. Method call sites in
other files are only kept in files that mention the declaring class's simple
name, which keeps same-named methods of unrelated classes out. Like the rest
of the pipeline, this uses the lightweight brace/regex Java scanning in
SpeciminPerformanceEvaluation/ExtractWarningMethods.py, not a symbol solver.

Run this AFTER ExtractRootWarning.py and BEFORE RunLLMInferenceAll.py.

Usage:
    python3 ExtractUsageContext.py                  # all slices
    python3 ExtractUsageContext.py --dry-run        # report only, write nothing
    python3 ExtractUsageContext.py --slice 98_runnerForClass --print
    python3 ExtractUsageContext.py --src-root /path/to/junit4/src/main/java
    python3 ExtractUsageContext.py --context 2 --max-lines 150

Options:
    --slice NAME|DIR   process one slice folder only
    --print            also print each usage context to stdout
    --dry-run          do not write or remove any usage-context.txt
    --src-root DIR     original source root for every slice, instead of each
                       slice's root.txt (e.g. when the recorded path is from
                       another machine)
    --context N        lines of context around each usage (default 1)
    --max-lines N      cap on excerpt lines per slice (default 120)

SPECIMIN_OUT can be overridden with the environment variable of the same name.
A slice without a root warning, or with nothing to report, gets no
usage-context.txt (a stale one from an earlier run is removed).
"""
from __future__ import annotations

import os
import re
import sys
import pathlib

sys.path.insert(
    0, str(pathlib.Path(__file__).resolve().parent.parent / "SpeciminPerformanceEvaluation")
)
from ExtractWarningMethods import (  # noqa: E402
    clean_lines,
    get_class_stack_at,
    get_package,
    index_members,
    innermost,
    parse_method_sig,
    split_params,
    strip_annotations,
)

SPECIMIN_OUT = pathlib.Path(os.environ.get(
    "SPECIMIN_OUT",
    "/Users/mushfiqurrahmanchowdhury/Documents/junit4/speciminout",
)).expanduser()

USAGE_CONTEXT_NAME = "usage-context.txt"
ROOT_WARNING_NAME = "root-warning.txt"
WARNING_NAME = "warning.txt"
ROOT_NAME = "root.txt"

DEFAULT_CONTEXT_LINES = 1
DEFAULT_MAX_LINES = 120

_LOCATION_RE = re.compile(r'^(.+?\.java):(\d+):\s*(?:warning|error):\s*\[NullAway\]\s*(.*)$')
_IDENT = r'[A-Za-z_$][\w$]*'
_NOT_MEMBERS = frozenset({
    'if', 'else', 'for', 'while', 'do', 'switch', 'case', 'return', 'try', 'catch',
    'finally', 'throw', 'new', 'class', 'interface', 'enum', 'synchronized',
    'instanceof', 'super', 'this', 'assert', 'null', 'true', 'false', 'final',
    'static', 'public', 'private', 'protected', 'void', 'int', 'long', 'boolean',
    'char', 'byte', 'short', 'double', 'float',
})


# ── Source handling ────────────────────────────────────────────────────────────

def blank_strings(line: str) -> str:
    """The line with string/char literal contents replaced by spaces (same
    length, quotes kept), so parentheses inside literals don't confuse the
    call parsing below."""
    out, i, n = list(line), 0, len(line)
    while i < n:
        if line[i] in ('"', "'"):
            q, i = line[i], i + 1
            while i < n and line[i] != q:
                if line[i] == '\\' and i + 1 < n:
                    out[i] = out[i + 1] = ' '
                    i += 2
                    continue
                out[i] = ' '
                i += 1
        i += 1
    return ''.join(out)


class JavaFile:
    """One parsed source file: its lines, and its fields and methods."""

    def __init__(self, root: pathlib.Path, path: pathlib.Path):
        self.path = path
        self.rel = str(path.relative_to(root))
        self.lines = path.read_text(encoding='utf-8', errors='replace').splitlines()
        self.cleaned = clean_lines(self.lines)
        self.text = '\n'.join(self.cleaned)
        package = get_package(self.lines)
        method_spans, field_spans, _inits = index_members(self.cleaned)

        def fqcn_at(idx):
            stack = get_class_stack_at(self.cleaned, idx)
            return (package + '.' if package else '') + '.'.join(stack) if stack else None

        # (name, fqcn, start, end)
        self.fields = [(name, fqcn_at(s), s, e) for s, e, name in field_spans if name]
        # (name, fqcn, start, end, param_types, param_names)
        self.methods = []
        for s, e, decl in method_spans:
            name, types = parse_method_sig(decl)
            if name:
                self.methods.append((name, fqcn_at(s), s, e, types, param_names(decl)))
        self.method_spans = method_spans
        self.field_spans = field_spans

    def header_end(self, start: int, end: int) -> int:
        """Last line of a member's declaration header (up to its '{' or ';')."""
        for idx in range(start, end + 1):
            if '{' in self.cleaned[idx] or ';' in self.cleaned[idx]:
                return idx
        return start


def param_names(decl: str) -> list:
    text = strip_annotations(decl)
    m = re.search(r'\w+\s*\(([^)]*)\)', text)
    if not m or not m.group(1).strip():
        return []
    names = []
    for p in split_params(m.group(1)):
        tokens = re.sub(r'\bfinal\s+', '', p).replace('...', ' ').split()
        names.append(tokens[-1] if tokens else '')
    return names


class Repo:
    """All Java files under one original source root, parsed lazily once."""

    def __init__(self, root: pathlib.Path):
        self.root = root
        self._files = None

    def files(self) -> list:
        if self._files is None:
            self._files = []
            for p in sorted(self.root.rglob('*.java')):
                try:
                    self._files.append(JavaFile(self.root, p))
                except (OSError, ValueError):
                    pass
        return self._files

    def file(self, path: pathlib.Path):
        for f in self.files():
            if f.path == path:
                return f
        return None

    def field_decls(self, name: str) -> list:
        return [(f, d) for f in self.files() for d in f.fields if d[0] == name]

    def method_decls(self, name: str) -> list:
        return [(f, d) for f in self.files() for d in f.methods if d[0] == name]

    def _hierarchy(self) -> dict:
        """Simple class name -> simple names of its direct supertypes."""
        if not hasattr(self, '_supers'):
            self._supers = {}
            head = re.compile(r'\b(?:class|interface|enum)\s+(' + _IDENT + r')\s*(<[^{]*?>)?'
                              r'\s*((?:extends|implements)[^{]*)?\{')
            for f in self.files():
                for m in head.finditer(f.text):
                    names = []
                    rest = m.group(3) or ''
                    while re.search(r'<[^<>]*>', rest):
                        rest = re.sub(r'<[^<>]*>', '', rest)
                    for part in re.split(r'\b(?:extends|implements)\b|,', rest):
                        part = part.strip()
                        if part:
                            names.append(part.split('.')[-1])
                    self._supers.setdefault(m.group(1), set()).update(names)
        return self._supers

    def declares_type(self, simple_name: str) -> bool:
        return simple_name in self._hierarchy()

    def supertypes(self, simple_name: str) -> set:
        """The class itself and all its supertypes declared in the project."""
        out, todo = set(), [simple_name]
        while todo:
            n = todo.pop()
            if n not in out:
                out.add(n)
                todo += list(self._hierarchy().get(n, ()))
        return out


_repos: dict = {}


def repo_for(root: pathlib.Path) -> Repo:
    if root not in _repos:
        _repos[root] = Repo(root)
    return _repos[root]


# ── Locating the warning in the original source ────────────────────────────────

def read_first_line(path: pathlib.Path) -> str | None:
    if not path.exists():
        return None
    for line in path.read_text(encoding='utf-8').splitlines():
        if line.strip():
            return line.strip()
    return None


def original_location(folder: pathlib.Path, src_root_override: pathlib.Path | None):
    """(source root, original file, 1-based line, message) or raises ValueError."""
    warning = read_first_line(folder / WARNING_NAME)
    if warning is None:
        raise ValueError(f"no {WARNING_NAME}")
    m = _LOCATION_RE.match(warning)
    if not m:
        raise ValueError(f"{WARNING_NAME} is not a NullAway diagnostic")
    recorded_file, line, message = pathlib.Path(m.group(1)), int(m.group(2)), m.group(3)

    recorded_root = read_first_line(folder / ROOT_NAME)
    recorded_root = pathlib.Path(recorded_root) if recorded_root else None
    root = src_root_override or recorded_root
    if root is None:
        raise ValueError(f"no {ROOT_NAME}; pass --src-root")
    if not root.is_dir():
        raise ValueError(f"source root not found: {root} (pass --src-root)")

    original = recorded_file
    if src_root_override is not None or not original.exists():
        rel = None
        if recorded_root is not None:
            try:
                rel = recorded_file.relative_to(recorded_root)
            except ValueError:
                rel = None
        if rel is None:
            parts = recorded_file.parts
            if 'java' in parts:  # .../src/main/java/<package path>
                rel = pathlib.Path(*parts[len(parts) - parts[::-1].index('java'):])
        if rel is None:
            raise ValueError(f"cannot map {recorded_file} into {root}")
        original = root / rel
    if not original.exists():
        raise ValueError(f"original file not found: {original}")
    return root, original, line, message


def statement_at(jf: JavaFile, idx: int) -> str:
    """The warning line joined with following lines until the statement ends
    (string literals blanked), for parsing calls and assignments."""
    parts = []
    for i in range(idx, min(idx + 6, len(jf.lines))):
        raw = jf.lines[i]
        parts.append(blank_strings(re.sub(r'//.*$', '', raw) if '"' not in raw else raw))
        if ';' in jf.cleaned[i] or '{' in jf.cleaned[i]:
            break
    return ' '.join(p.strip() for p in parts)


def enclosing_method(jf: JavaFile, idx: int):
    best = None
    for d in jf.methods:
        if d[2] <= idx <= d[3] and (best is None or d[2] > best[2]):
            best = d
    return best


# ── Parsing the member(s) out of the warning ───────────────────────────────────

def enclosing_call(text: str, pos: int):
    """For the character at pos inside a call's argument list, returns
    (callee_name, is_constructor, arg_index, args, receiver) or None. args is
    the list of argument texts, or None if the call's closing ')' is not in
    text; receiver is the qualifier before the name ('' if none)."""
    depth, commas = 0, 0
    i = pos - 1
    while i >= 0:
        c = text[i]
        if c in ')]}':
            depth += 1
        elif c in '([{':
            if depth == 0:
                if c != '(':
                    return None
                break
            depth -= 1
        elif c == ',' and depth == 0:
            commas += 1
        i -= 1
    if i < 0:
        return None
    open_paren = i
    # Split the arguments (up to the matching ')', if it is in this text).
    depth, args, start, j = 0, [], open_paren + 1, open_paren + 1
    while j < len(text):
        c = text[j]
        if c in '([{':
            depth += 1
        elif c in ')]}':
            if depth == 0:
                break
            depth -= 1
        elif c == ',' and depth == 0:
            args.append(text[start:j].strip())
            start = j + 1
        j += 1
    if j < len(text):
        last = text[start:j].strip()
        if last or args:
            args.append(last)
    else:
        args = None
    before = text[:open_paren].rstrip()
    before = re.sub(r'<[^<>]*>$', '', before).rstrip()  # generic args: foo.<T>bar( / new X<T>(
    m = re.search(r'(new\s+)?((?:' + _IDENT + r'\s*\.\s*)*)(' + _IDENT + r')$', before)
    if not m:
        return None
    name = m.group(3)
    if m.group(1) or name in ('this', 'super'):
        return name, True, commas, args, ''
    if name in _NOT_MEMBERS:
        return None
    receiver = re.sub(r'\s', '', m.group(2)).rstrip('.')
    return name, False, commas, args, receiver


# ── Overload resolution by argument types ──────────────────────────────────────

_PRIMITIVES = frozenset({'int', 'long', 'boolean', 'char', 'byte', 'short', 'double', 'float'})


def erase(type_text: str) -> str:
    """Simple name without generics or package: java.util.List<T> -> List, int[] kept."""
    t = strip_annotations(type_text).replace('...', '[]')
    while re.search(r'<[^<>]*>', t):
        t = re.sub(r'<[^<>]*>', '', t)
    t = re.sub(r'\bfinal\s+', '', t).strip()
    return re.sub(r'^(?:[\w$]+\.)+', '', t).replace(' ', '')


def declared_type(jf: JavaFile, enc, name: str):
    """Declared type of an identifier: a parameter or local of the enclosing
    method, or a field of this file; None if unknown."""
    if enc is not None:
        if name in enc[5]:
            return enc[4][enc[5].index(name)]
        # locals, and catch / enhanced-for / lambda parameters: `Type name =`, `(Type name)`
        decl = re.compile(r'([\w$.<>\[\]?, |]+?)\s+' + re.escape(name) + r'\s*(=|;|:|\))')
        for i in range(enc[2] + 1, enc[3] + 1):
            m = decl.search(jf.cleaned[i])
            if m:
                tokens = re.split(r'[\s(|]+', m.group(1).replace('final ', ' ').strip())
                if tokens and tokens[-1] not in _NOT_MEMBERS - _PRIMITIVES:
                    return tokens[-1]
    for i, d in enumerate(jf.fields):
        if d[0] == name:
            text = strip_annotations(' '.join(jf.cleaned[d[2]:d[2] + 1]))
            m = re.search(r'([\w$.<>\[\]?]+)\s+' + re.escape(name) + r'\b', text)
            return m.group(1) if m else None
    return None


def arg_type(jf: JavaFile, enc, arg: str):
    a = arg.strip()
    if a == 'null':
        return 'null'
    if re.fullmatch(r'"[^"]*"', a):
        return 'String'
    if a in ('true', 'false'):
        return 'boolean'
    if re.fullmatch(r"'.*'", a):
        return 'char'
    if re.fullmatch(r'\d+[lL]', a):
        return 'long'
    if re.fullmatch(r'\d+', a):
        return 'int'
    if re.fullmatch(r'[\d.]+[fF]', a):
        return 'float'
    if re.fullmatch(r'\d*\.\d+[dD]?', a):
        return 'double'
    m = re.match(r'\(\s*([\w$.<>\[\]?, ]+?)\s*\)\s*[\w$(]', a)  # cast
    if m:
        return m.group(1)
    m = re.fullmatch(r'new\s+([\w$.]+)\s*(<[^()]*>)?\s*\(.*\)', a)     # new Type(...)
    if m:
        return m.group(1)
    m = re.fullmatch(r'([A-Z][\w$]*)\.valueOf\s*\(.*\)', a)          # Long.valueOf(x)
    if m:
        return m.group(1)
    if re.fullmatch(_IDENT, a):
        return declared_type(jf, enc, a)
    return None


def best_overloads(decls: list, jf: JavaFile, enc, args):
    """Keeps the declarations whose parameter types fit the call's arguments
    best; a null argument never goes to a primitive parameter."""
    if args is None:
        return decls
    types = [arg_type(jf, enc, a) for a in args]
    scored = []
    for f, d in decls:
        params = [erase(p) for p in d[4]]
        varargs = bool(d[4]) and '...' in d[4][-1]
        if len(params) != len(args) and not (varargs and len(args) >= len(params) - 1):
            continue
        score, ok = 0, True
        for i, t in enumerate(types):
            p = params[min(i, len(params) - 1)] if params else None
            if p is None or t is None:
                continue
            if varargs and i >= len(params) - 1 and p.endswith('[]') and erase(t) != p:
                p = p[:-2]
            if t == 'null':
                ok = ok and p not in _PRIMITIVES
            elif erase(t) == p:
                score += 2
            elif (erase(t) in _PRIMITIVES) != (p in _PRIMITIVES):
                score -= 1  # boxing/unboxing: allowed, but a worse fit
        if ok:
            scored.append((score, f, d))
    if not scored:
        return decls
    top = max(sc for sc, _f, _d in scored)
    return [(f, d) for sc, f, d in scored if sc == top]


def trailing_member(expr: str):
    """('method'|'name', identifier, receiver) for the last member an
    expression accesses: `a.c(x)` -> ('method', 'c', 'a'), `this.f` ->
    ('name', 'f', 'this'). receiver is '' when there is none."""
    expr = blank_strings(expr).strip()
    if expr.endswith(')'):
        depth, i = 0, len(expr) - 1
        while i >= 0:
            if expr[i] == ')':
                depth += 1
            elif expr[i] == '(':
                depth -= 1
                if depth == 0:
                    break
            i -= 1
        m = re.search(r'(?:([\w$.()]*?)\s*\.\s*)?(' + _IDENT + r')\s*$', expr[:max(i, 0)])
        return ('method', m.group(2), m.group(1) or '') if m else None
    m = re.search(r'(?:([\w$.()]*?)\s*\.\s*)?(' + _IDENT + r')\s*$', expr)
    if m and m.group(2) not in _NOT_MEMBERS:
        return ('name', m.group(2), m.group(1) or '')
    return None


def receiver_class(repo: Repo, jf: JavaFile, idx: int, enc, receiver: str):
    """Simple class name a call's receiver refers to, '' for no receiver or
    one whose type can't be read, and None for a type outside the project
    (e.g. a JDK Map), whose methods are not the project's."""
    r = receiver.strip()
    if not r:
        return ''
    if r in ('this', 'super'):
        return class_named(repo, jf, idx, r) or ''
    if re.fullmatch(_IDENT, r):
        t = declared_type(jf, enc, r)
        if t is None and r[0].isupper():
            t = r  # a static call such as Assert.fail(...)
        if t is None:
            return ''
        t = erase(t).rstrip('[]')
        return t if repo.declares_type(t) else None
    return ''


def restrict_to_receiver(repo: Repo, decls: list, cls):
    if cls is None:
        return []
    if not cls:
        return decls
    allowed = repo.supertypes(cls)
    return [(f, d) for f, d in decls if (d[1] or '').split('.')[-1] in allowed]


class Member:
    """A field or method the warning is about, with where it is declared."""

    def __init__(self, kind, name, decls, reason, param_index=None, is_ctor=False):
        self.kind, self.name, self.decls = kind, name, decls   # decls: [(JavaFile, decl)]
        self.reason, self.param_index, self.is_ctor = reason, param_index, is_ctor

    def label(self) -> str:
        where = ', '.join(sorted({d[1] or '?' for _f, d in self.decls}))
        if self.kind == 'field':
            return f"field {self.name} ({where})"
        sigs = sorted({f"{d[0]}({', '.join(d[4])})" for _f, d in self.decls})
        kind = 'constructor' if self.is_ctor else 'method'
        return f"{kind} {' / '.join(sigs)} ({where})"

    def key(self):
        return self.kind, self.name, tuple(sorted((f.rel, d[2]) for f, d in self.decls))


def resolve_field(repo: Repo, jf: JavaFile, name: str):
    same = [(jf, d) for d in jf.fields if d[0] == name]
    if same:
        return same
    decls = repo.field_decls(name)
    return decls if 0 < len(decls) <= 3 else []


def resolve_method(repo: Repo, jf: JavaFile, name: str, args=None, is_ctor=False, enc=None,
                   receiver_cls=''):
    if receiver_cls is None:
        return []  # a method of a type outside the project
    if receiver_cls:
        decls = restrict_to_receiver(repo, repo.method_decls(name), receiver_cls)
        decls = best_overloads(decls, jf, enc, args)
        return decls if len(decls) <= 5 else []
    same = [(jf, d) for d in jf.methods if d[0] == name]
    if same and not is_ctor:
        return best_overloads(same, jf, enc, args)
    decls = repo.method_decls(name)
    if is_ctor:  # a constructor is declared in the file of the class it constructs
        decls = [(f, d) for f, d in decls if pathlib.Path(f.rel).stem == name] or decls
    decls = best_overloads(decls, jf, enc, args)
    package = get_package(jf.lines)
    in_package = [(f, d) for f, d in decls if (d[1] or '').startswith(package + '.')]
    if package and in_package:
        decls = in_package  # same-named classes in other packages are less likely
    return decls if 0 < len(decls) <= 5 else best_overloads(same, jf, enc, args)


def class_named(repo: Repo, jf: JavaFile, idx: int, which: str):
    """Simple name of the enclosing class ('this') or its superclass ('super')."""
    stack = get_class_stack_at(jf.cleaned, idx)
    if not stack:
        return None
    if which == 'this':
        return stack[-1]
    m = re.search(r'\bclass\s+' + re.escape(stack[-1]) + r'\b[^{]*?\bextends\s+(' + _IDENT + r')',
                  jf.text)
    return m.group(1) if m else None


def name_member(repo, jf, idx, name, reason):
    """A plain identifier from the warning: a parameter of the enclosing method
    (-> that method, whose callers pass it), a local (-> nothing), or a field."""
    enc = enclosing_method(jf, idx)
    if enc is not None:
        if name in enc[5]:
            return [Member('method', enc[0], [(jf, enc)],
                           f"{reason}; '{name}' is parameter {enc[5].index(name)} of the "
                           "enclosing method, so its callers supply the value",
                           param_index=enc[5].index(name))]
        local = re.compile(r'[\w>\]]\s+' + re.escape(name) + r'\s*(=|;|:)')
        if any(local.search(jf.cleaned[i]) for i in range(enc[2] + 1, enc[3] + 1)):
            return []  # a local variable: everything about it is in the slice
    decls = resolve_field(repo, jf, name)
    return [Member('field', name, decls, reason)] if decls else []


def members_for_warning(repo: Repo, jf: JavaFile, line: int, message: str) -> list:
    idx = line - 1
    stmt = statement_at(jf, idx)
    enc = enclosing_method(jf, idx)
    out = []

    m = re.match(r'dereferenced expression (.+) is @Nullable', message)
    if m:
        tm = trailing_member(m.group(1))
        if tm and tm[0] == 'method':
            call = None
            pos = blank_strings(stmt).find(tm[1] + '(')
            if pos >= 0:
                close = stmt.find('(', pos)
                call = enclosing_call(stmt, close + 1)
            decls = resolve_method(repo, jf, tm[1], call[3] if call else None, enc=enc,
                                   receiver_cls=receiver_class(repo, jf, idx, enc, tm[2]))
            if decls:
                out.append(Member('method', tm[1], decls,
                                  "its @Nullable result is dereferenced at the warning line"))
        elif tm:
            out += name_member(repo, jf, idx, tm[1], "dereferenced at the warning line")
        return out

    if message.startswith('returning @Nullable expression'):
        if enc is not None:
            out.append(Member('method', enc[0], [(jf, enc)],
                              "the method returning @Nullable (how callers use its result)"))
        rm = re.search(r'\breturn\s+(.+?);', stmt)
        if rm:
            tm = trailing_member(rm.group(1))
            if tm and tm[0] == 'name':
                out += [x for x in name_member(repo, jf, idx, tm[1], "the returned value")
                        if x.kind == 'field']
        return out

    m = re.match(r"passing @Nullable parameter '(.+)' where @NonNull is required", message)
    if m:
        arg = blank_strings(m.group(1))
        pattern = r'\bnull\b' if arg == 'null' else re.escape(arg)
        for hit in re.finditer(pattern, stmt):
            call = enclosing_call(stmt, hit.start())
            if call is None:
                continue
            name, is_ctor, arg_index, args, receiver = call
            if name in ('this', 'super'):
                name = class_named(repo, jf, idx, name)
                if name is None:
                    continue
            decls = resolve_method(repo, jf, name, args, is_ctor, enc,
                                   receiver_class(repo, jf, idx, enc, receiver))
            if decls:
                out.append(Member('method', name, decls,
                                  f"receives the @Nullable argument as parameter {arg_index}",
                                  param_index=arg_index, is_ctor=is_ctor))
            break
        tm = trailing_member(m.group(1)) if arg != 'null' else None
        if tm and tm[0] == 'method':
            decls = resolve_method(repo, jf, tm[1], enc=enc,
                                   receiver_cls=receiver_class(repo, jf, idx, enc, tm[2]))
            if decls:
                out.append(Member('method', tm[1], decls, "returns the @Nullable argument"))
        elif tm:
            out += [x for x in name_member(repo, jf, idx, tm[1], "the @Nullable argument")
                    if x.kind == 'field']
        return out

    if message.startswith('assigning @Nullable expression to @NonNull field'):
        am = re.search(r'(' + _IDENT + r')\s*=(?!=)', strip_annotations(stmt))
        if am:
            decls = resolve_field(repo, jf, am.group(1))
            if decls:
                out.append(Member('field', am.group(1), decls, "assigned at the warning line"))
        return out

    fields = re.findall(r'(' + _IDENT + r') \(line \d+\)', message)
    if not fields:
        fm = re.search(r'field ([\w.$]+) (?:not initialized|before initialization)', message)
        if fm:
            fields = [re.split(r'[.$]', fm.group(1))[-1]]
    if fields:
        for f in fields:
            decls = resolve_field(repo, jf, f)
            if decls:
                out.append(Member('field', f, decls, "named in the warning"))
        return out

    m = re.search(r'superclass method [\w.$]+\.(' + _IDENT + r')\(', message)
    if m:
        decls = repo.method_decls(m.group(1))
        if decls:
            out.append(Member('method', m.group(1), decls,
                              "overridden method named in the warning"))
        return out

    # Fallback (e.g. "unboxing of a @Nullable value"): fields and methods of
    # this file that appear on the warning line.
    seen = set()
    for ident in re.findall(_IDENT, jf.cleaned[idx]):
        if ident in seen or ident in _NOT_MEMBERS:
            continue
        seen.add(ident)
        fdecls = [(jf, d) for d in jf.fields if d[0] == ident]
        mdecls = [(jf, d) for d in jf.methods if d[0] == ident
                  and re.search(r'\b' + re.escape(ident) + r'\s*\(', jf.cleaned[idx])]
        if fdecls:
            out.append(Member('field', ident, fdecls, "appears on the warning line"))
        elif mdecls and (enc is None or mdecls[0][1][2] != enc[2]):
            out.append(Member('method', ident, mdecls, "called on the warning line"))
    return out


# ── Collecting usages ──────────────────────────────────────────────────────────

def excerpt(jf: JavaFile, hits: list, context: int) -> list:
    """Blocks of (1-based line, text) around the hit indices."""
    ranges = []
    for idx in sorted(set(hits)):
        lo, hi = max(0, idx - context), min(len(jf.lines) - 1, idx + context)
        if ranges and lo <= ranges[-1][1] + 1:
            ranges[-1][1] = max(ranges[-1][1], hi)
        else:
            ranges.append([lo, hi])
    return [[(n + 1, jf.lines[n]) for n in range(lo, hi + 1)] for lo, hi in ranges]


def merge_blocks(blocks: list) -> list:
    """Re-groups (line, text) pairs from several blocks into contiguous blocks."""
    lines = dict(pair for b in blocks for pair in b)
    out = []
    for n in sorted(lines):
        if out and out[-1][-1][0] == n - 1:
            out[-1].append((n, lines[n]))
        else:
            out.append([(n, lines[n])])
    return out


def usages(repo: Repo, member: Member, skip_file: JavaFile, skip_span, context: int):
    """[(title, rel_file, blocks)] for one member: its declaration(s), then
    its uses (same file first, then other files)."""
    sections = []
    decl_files = {f.rel for f, _d in member.decls}
    class_names = {(d[1] or '').split('.')[-1] for _f, d in member.decls}
    if member.kind == 'method' and not member.is_ctor:
        # calls through a supertype (e.g. builder.runnerForClass(...) on a RunnerBuilder)
        class_names = set().union(*(repo.supertypes(c) for c in class_names if c))

    # Declarations (and, for a callee, how the parameter is used in its body).
    for f, d in member.decls:
        start, end = d[2], d[3]
        hdr_end = f.header_end(start, end) if member.kind == 'method' else min(end, start + 2)
        blocks = excerpt(f, list(range(start, hdr_end + 1)), 0)
        title = 'declaration'
        if member.kind == 'method' and member.param_index is not None \
                and member.param_index < len(d[5]):
            p = d[5][member.param_index]
            pat = re.compile(r'\b' + re.escape(p) + r'\b')
            uses = [i for i in range(hdr_end + 1, end + 1) if pat.search(f.cleaned[i])]
            if uses:
                title = f"declaration, and uses of parameter '{p}' in its body"
                blocks = merge_blocks(blocks + excerpt(f, uses, context))
        sections.append((title, f.rel, blocks))

    # Uses.
    if member.kind == 'field':
        own = re.compile(r'\b' + re.escape(member.name) + r'\b(?!\s*\()')
        qualified = re.compile(r'\.\s*' + re.escape(member.name) + r'\b(?!\s*\()')
    else:
        if member.is_ctor:
            own = qualified = re.compile(r'\bnew\s+' + re.escape(member.name) + r'\s*(<[^>]*>)?\s*\(|'
                                         r'\b(this|super)\s*\(')
        else:
            own = qualified = re.compile(r'(?<![\w$])' + re.escape(member.name) + r'\s*\(|::\s*'
                                         + re.escape(member.name) + r'\b')
    decl_lines = {(f.rel, i) for f, d in member.decls
                  for i in range(d[2], (f.header_end(d[2], d[3]) if member.kind == 'method'
                                        else d[3]) + 1)}
    files = sorted(repo.files(), key=lambda f: (f.rel not in decl_files, f.rel))
    for f in files:
        same = f.rel in decl_files
        if not same and not any(re.search(r'\b' + re.escape(c) + r'\b', f.text)
                                for c in class_names if c):
            continue
        pat = own if same else qualified
        if member.is_ctor and same:
            pat = re.compile(r'\bnew\s+' + re.escape(member.name) + r'\s*(<[^>]*>)?\s*\(|'
                             r'\b(this|super)\s*\(')
        if member.kind == 'method' and not member.is_ctor:
            # method declarations with this name are not call sites
            decl_lines |= {(f.rel, i) for d in f.methods if d[0] == member.name
                           for i in range(d[2], f.header_end(d[2], d[3]) + 1)}
        hits = []
        for i, line in enumerate(f.cleaned):
            if (f.rel, i) in decl_lines or not line.strip():
                continue
            if f is skip_file and skip_span and skip_span[0] <= i <= skip_span[1]:
                continue
            match = pat.search(line)
            if match and (member.kind == 'field' or calls_member(repo, member, f, i, match)):
                hits.append(i)
        if hits:
            sections.append(('uses', f.rel, excerpt(f, hits, context)))
    return sections


def calls_member(repo: Repo, member: Member, f: JavaFile, idx: int, match) -> bool:
    """Whether the call matched on line idx resolves (by argument count and
    types) to one of the member's own overloads. Calls whose arguments can't
    be read (e.g. spread over several lines) are kept."""
    if match.group(0).startswith('::') or re.match(r'(this|super)\s*\(', match.group(0)):
        return True
    # A declaration (e.g. an override in an anonymous class) is not a call.
    head = strip_annotations(f.cleaned[idx][:match.start()])
    if re.fullmatch(r'\s*((public|protected|private|static|final|abstract|synchronized|native)\s+)*'
                    r'(<[^>]*>\s*)?[\w$.<>\[\],?]+(\s*<[^>]*>)?\s+', head) \
            and not re.match(r'\s*(return|throw|new|else|case)\b', head):
        return False
    text = blank_strings(f.lines[idx])
    paren = text.find('(', match.start())
    if paren < 0:
        return True
    call = enclosing_call(text, paren + 1)
    if call is None or call[3] is None:
        return True
    overloads = repo.method_decls(member.name)
    if member.is_ctor:
        overloads = [(g, d) for g, d in overloads if pathlib.Path(g.rel).stem == member.name]
    chosen = best_overloads(overloads, f, enclosing_method(f, idx), call[3])
    own = {(g.rel, d[2]) for g, d in member.decls}
    return any((g.rel, d[2]) in own for g, d in chosen)


def render(message: str, members: list, sections_by_member: list, max_lines: int) -> str:
    out = [
        "Read-only excerpts from the ORIGINAL program (outside this reduced slice) showing",
        "how the field(s)/method(s) involved in the NullAway warning below are declared and",
        "used elsewhere. Use them as evidence for nullability inference. Do NOT annotate or",
        "modify these excerpts.",
        f"Warning: {message}",
        "Members of interest:",
    ]
    out += [f"  - {m.label()}: {m.reason}" for m in members]
    budget = max_lines
    truncated = False
    for member, sections in zip(members, sections_by_member):
        if budget <= 0:
            truncated = True
            break
        out.append(f"\n## {member.label()}")
        for title, rel, blocks in sections:
            if budget <= 0:
                truncated = True
                break
            out.append(f"# {rel} ({title})")
            for block in blocks:
                if budget <= 0:
                    truncated = True
                    break
                for lineno, text in block[:budget]:
                    out.append(f"  {lineno:>5}: {text}")
                budget -= len(block)
                out.append("  ---")
    if truncated:
        out.append(f"(truncated at {max_lines} excerpt lines)")
    return "\n".join(out).rstrip()


def build_usage_context(folder: pathlib.Path, src_root_override, context: int, max_lines: int):
    """(text or '', note). Raises ValueError when the original can't be located."""
    root_warning = read_first_line(folder / ROOT_WARNING_NAME)
    if root_warning is None:
        return '', f"no {ROOT_WARNING_NAME}"
    root, original, line, message = original_location(folder, src_root_override)
    repo = repo_for(root)
    jf = repo.file(original) or JavaFile(root, original)
    if line > len(jf.lines):
        raise ValueError(f"line {line} is past the end of {jf.rel} (source changed?)")

    members = []
    for m in members_for_warning(repo, jf, line, message):
        if m.key() not in {x.key() for x in members}:
            members.append(m)
    if not members:
        return '', "no field/method found for this warning (e.g. it is about a local variable)"

    method_spans = [(d[2], d[3]) for d in jf.methods]
    skip_span = innermost(method_spans + [(s, e) for s, e, _n in jf.field_spans], line - 1)
    sections = [usages(repo, m, jf, skip_span, context) for m in members]
    if not any(blocks for secs in sections for _t, _r, blocks in secs):
        return '', "no declarations or uses found"
    text = render(message, members, sections, max_lines)
    return text, ', '.join(m.label() for m in members)


# ── Main ───────────────────────────────────────────────────────────────────────

def _opt(flag, default=None):
    if flag in sys.argv:
        i = sys.argv.index(flag)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        print(f"ERROR: {flag} needs a value")
        sys.exit(2)
    return default


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    show = "--print" in sys.argv
    src_root = _opt("--src-root")
    src_root = pathlib.Path(src_root).expanduser() if src_root else None
    context = int(_opt("--context", DEFAULT_CONTEXT_LINES))
    max_lines = int(_opt("--max-lines", DEFAULT_MAX_LINES))
    only = _opt("--slice")

    if only:
        p = pathlib.Path(only).expanduser()
        folders = [p if p.is_dir() else SPECIMIN_OUT / only]
    else:
        print(f"SPECIMIN_OUT : {SPECIMIN_OUT}")
        if not SPECIMIN_OUT.exists():
            print(f"ERROR: SPECIMIN_OUT not found: {SPECIMIN_OUT}")
            sys.exit(1)
        folders = sorted(d for d in SPECIMIN_OUT.iterdir()
                         if d.is_dir() and not d.name.endswith("LLMInferenced"))
    if src_root is not None and not src_root.is_dir():
        print(f"ERROR: --src-root not found: {src_root}")
        sys.exit(1)
    if dry_run:
        print("(dry-run — no files will be written or removed)")

    written, empty, errors, no_root = [], [], [], 0
    for folder in folders:
        if not folder.is_dir():
            print(f"ERROR: slice folder not found: {folder}")
            sys.exit(1)
        out_file = folder / USAGE_CONTEXT_NAME
        try:
            text, note = build_usage_context(folder, src_root, context, max_lines)
        except (ValueError, OSError) as e:
            text, note = '', f"ERROR: {e}"
            errors.append((folder.name, str(e)))
        if not text:
            if note == f"no {ROOT_WARNING_NAME}":
                no_root += 1
            elif not note.startswith("ERROR"):
                empty.append((folder.name, note))
            if out_file.exists() and not dry_run:
                out_file.unlink()
                note += f" (removed stale {USAGE_CONTEXT_NAME})"
            if only or note != f"no {ROOT_WARNING_NAME}":
                print(f"── {folder.name}: {note}")
            continue
        n_lines = sum(1 for l in text.splitlines() if re.match(r'\s+\d+: ', l))
        written.append(folder.name)
        print(f"── {folder.name}: {n_lines} excerpt line(s) — {note}")
        if show:
            print(text + "\n")
        if not dry_run:
            out_file.write_text(text + "\n", encoding="utf-8")

    print(f"\n{'═' * 60}")
    print(f"Summary: {len(folders)} slice folder(s)")
    print(f"  {USAGE_CONTEXT_NAME} {'would be ' if dry_run else ''}written : {len(written)}")
    print(f"  root warning, but nothing to report  : {len(empty)}")
    print(f"  no {ROOT_WARNING_NAME} (skipped)      : {no_root}")
    if errors:
        print(f"  errors                               : {len(errors)}")
        for name, e in errors:
            print(f"    - {name}: {e}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
