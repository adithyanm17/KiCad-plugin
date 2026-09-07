"""A minimal S-expression reader/writer for KiCad files.

KiCad's ``.kicad_pcb`` and ``.kicad_mod`` files are S-expressions. Parsing them
in pure Python means this library can read footprints and emit boards with no
KiCad installation and no third-party dependency.

Representation:

* a list becomes a Python ``list``
* a bare token becomes :class:`Sym` (a ``str`` subclass) -- written unquoted
* a quoted token becomes a plain ``str`` -- written quoted and escaped
* numbers stay as ``int`` / ``float``

Keeping ``Sym`` distinct from ``str`` is what makes a round-trip byte-faithful:
``(layer "F.Cu")`` and ``(layer F.Cu)`` are both valid but only one is what
KiCad writes, and mangling it produces files that load with subtle differences.
"""

from __future__ import annotations

from typing import Any, Iterator, List, Optional, Sequence, Union

__all__ = ["Sym", "parse", "parse_all", "dumps", "find", "find_all", "get", "sexp_str"]

SExp = Union["Sym", str, int, float, List[Any]]

_WS = " \t\r\n"
_DELIM = _WS + "()"


class Sym(str):
    """A bare (unquoted) S-expression token, e.g. ``footprint`` or ``F.Cu``."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "Sym(%s)" % str.__repr__(self)


def _tokenize(text: str) -> Iterator[tuple]:
    """Yield ``(kind, value)`` tuples: ``('(',)``, ``(')',)``, ``('atom', tok)``."""
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in _WS:
            i += 1
            continue
        if ch == "(":
            yield ("(", None)
            i += 1
            continue
        if ch == ")":
            yield (")", None)
            i += 1
            continue
        if ch == ";":  # KiCad does not emit comments, but be tolerant
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == '"':
            i += 1
            buf = []
            while i < n:
                c = text[i]
                if c == "\\" and i + 1 < n:
                    nxt = text[i + 1]
                    buf.append({"n": "\n", "t": "\t", "r": "\r"}.get(nxt, nxt))
                    i += 2
                    continue
                if c == '"':
                    i += 1
                    break
                buf.append(c)
                i += 1
            yield ("atom", "".join(buf))  # plain str -> quoted on output
            continue
        start = i
        while i < n and text[i] not in _DELIM:
            i += 1
        yield ("atom", Sym(text[start:i]))


def _coerce(tok: Any) -> Any:
    """Turn a bare numeric token into an int/float; leave everything else alone."""
    if not isinstance(tok, Sym):
        return tok
    s = str(tok)
    if not s:
        return tok
    c = s[0]
    if not (c.isdigit() or (c in "+-." and len(s) > 1)):
        return tok
    try:
        if ("." not in s) and ("e" not in s) and ("E" not in s):
            return int(s)
        return float(s)
    except ValueError:
        return tok


def parse_all(text: str) -> List[SExp]:
    """Parse every top-level expression in ``text``."""
    stack: List[List[Any]] = []
    out: List[SExp] = []
    for kind, val in _tokenize(text):
        if kind == "(":
            node: List[Any] = []
            if stack:
                stack[-1].append(node)
            stack.append(node)
        elif kind == ")":
            if not stack:
                raise ValueError("unbalanced ')' in S-expression")
            node = stack.pop()
            if not stack:
                out.append(node)
        else:
            item = _coerce(val)
            if stack:
                stack[-1].append(item)
            else:
                out.append(item)
    if stack:
        raise ValueError("unbalanced '(' in S-expression")
    return out


def parse(text: str) -> SExp:
    """Parse exactly one top-level expression."""
    exprs = parse_all(text)
    if not exprs:
        raise ValueError("no S-expression found")
    return exprs[0]


def _quote(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = out.replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return '"%s"' % out


def _fmt_atom(a: Any) -> str:
    if isinstance(a, Sym):
        return str(a)
    if isinstance(a, bool):  # must precede int -- bool is an int subclass
        return "yes" if a else "no"
    if isinstance(a, int):
        return str(a)
    if isinstance(a, float):
        # KiCad writes trimmed decimals; %g would switch to exponent notation.
        s = ("%.6f" % a).rstrip("0").rstrip(".")
        return s if s not in ("", "-") else "0"
    return _quote(str(a))


def dumps(node: SExp, indent: int = 0, _depth: int = 0) -> str:
    """Serialise ``node`` back to S-expression text.

    With ``indent=0`` the output is compact (one line). KiCad reads either, but
    an indented file is far easier to diff and to eyeball during development.
    """
    if not isinstance(node, list):
        return _fmt_atom(node)

    parts = [dumps(c, indent, _depth + 1) for c in node]

    if indent <= 0:
        return "(" + " ".join(parts) + ")"

    # Keep short, all-atom forms on one line: (at 1 2), (layer "F.Cu").
    if all(not isinstance(c, list) for c in node):
        line = "(" + " ".join(parts) + ")"
        if len(line) <= 100:
            return line

    pad = " " * (indent * (_depth + 1))
    closing_pad = " " * (indent * _depth)
    head = parts[0] if parts else ""
    rest = parts[1:]
    if not rest:
        return "(" + head + ")"
    body = "\n".join(pad + p for p in rest)
    return "(" + head + "\n" + body + "\n" + closing_pad + ")"


# -- navigation helpers ---------------------------------------------------


def find(node: SExp, key: str) -> Optional[List[Any]]:
    """Return the first direct child list whose head is ``key``."""
    if not isinstance(node, list):
        return None
    for child in node:
        if isinstance(child, list) and child and str(child[0]) == key:
            return child
    return None


def find_all(node: SExp, key: str) -> List[List[Any]]:
    """Return every direct child list whose head is ``key``."""
    if not isinstance(node, list):
        return []
    return [
        c for c in node if isinstance(c, list) and c and str(c[0]) == key
    ]


def get(node: SExp, key: str, index: int = 1, default: Any = None) -> Any:
    """Return ``index``-th value of the first ``key`` child, or ``default``."""
    child = find(node, key)
    if child is None or len(child) <= index:
        return default
    return child[index]


def sexp_str(value: Any) -> str:
    """Coerce an S-expression atom to a plain Python string."""
    return "" if value is None else str(value)


def replace_child(node: List[Any], key: str, new_child: Sequence[Any]) -> None:
    """Replace the first ``key`` child in ``node``, appending if absent."""
    for i, child in enumerate(node):
        if isinstance(child, list) and child and str(child[0]) == key:
            node[i] = list(new_child)
            return
    node.append(list(new_child))


def remove_children(node: List[Any], key: str) -> int:
    """Drop every direct child list whose head is ``key``. Returns count removed."""
    before = len(node)
    node[:] = [
        c
        for c in node
        if not (isinstance(c, list) and c and str(c[0]) == key)
    ]
    return before - len(node)
