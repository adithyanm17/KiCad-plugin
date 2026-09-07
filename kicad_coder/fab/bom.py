"""Bill of materials generation.

Two sources, same output shape:

* from the IR -- works with no KiCad and no board file
* from a ``.kicad_pcb`` -- reads the properties the board writer stamped onto
  each footprint, so a board handed to you by someone else still yields a BOM

Lines are consolidated by (value, footprint, MPN), which is the grouping a
purchaser actually wants: one row per orderable part, with the references that
row covers.
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from ..backends import sexpr
from ..ir.types import Design

__all__ = ["BomLine", "Bom", "bom_from_design", "bom_from_board", "write_bom"]

#: Columns emitted by default, in order.
DEFAULT_COLUMNS = [
    "Item", "Qty", "References", "Value", "Footprint",
    "MPN", "Manufacturer", "Description", "DNP",
]


@dataclass
class BomLine:
    references: List[str]
    value: str
    footprint: str
    mpn: str = ""
    manufacturer: str = ""
    description: str = ""
    datasheet: str = ""
    dnp: bool = False
    extra: Dict[str, str] = field(default_factory=dict)

    @property
    def quantity(self) -> int:
        return len(self.references)

    @property
    def refs_str(self) -> str:
        return ", ".join(_natural_sort(self.references))

    def as_row(self, item: int, columns: Sequence[str]) -> Dict[str, str]:
        base = {
            "Item": str(item),
            "Qty": str(self.quantity),
            "References": self.refs_str,
            "Value": self.value,
            "Footprint": self.footprint,
            "MPN": self.mpn,
            "Manufacturer": self.manufacturer,
            "Description": self.description,
            "Datasheet": self.datasheet,
            "DNP": "DNP" if self.dnp else "",
        }
        base.update(self.extra)
        return {c: base.get(c, "") for c in columns}


@dataclass
class Bom:
    lines: List[BomLine]
    source: str = ""
    columns: List[str] = field(default_factory=lambda: list(DEFAULT_COLUMNS))

    @property
    def total_parts(self) -> int:
        return sum(l.quantity for l in self.lines if not l.dnp)

    @property
    def unique_parts(self) -> int:
        return len([l for l in self.lines if not l.dnp])

    def rows(self) -> List[Dict[str, str]]:
        return [l.as_row(i, self.columns) for i, l in enumerate(self.lines, 1)]

    def to_csv(self) -> str:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=self.columns, lineterminator="\n")
        writer.writeheader()
        for row in self.rows():
            writer.writerow(row)
        return buf.getvalue()

    def to_json(self) -> str:
        return json.dumps({
            "source": self.source,
            "unique_parts": self.unique_parts,
            "total_parts": self.total_parts,
            "lines": self.rows(),
        }, indent=2)

    def to_markdown(self) -> str:
        cols = self.columns
        out = ["| " + " | ".join(cols) + " |",
               "|" + "|".join("---" for _ in cols) + "|"]
        for row in self.rows():
            out.append("| " + " | ".join(
                str(row.get(c, "")).replace("|", "\\|") for c in cols) + " |")
        return "\n".join(out)

    def missing_mpn(self) -> List[str]:
        """References on lines with no manufacturer part number."""
        out: List[str] = []
        for line in self.lines:
            if not line.mpn and not line.dnp:
                out.extend(line.references)
        return _natural_sort(out)

    def summary(self) -> str:
        lines = [
            "BOM: %d unique part(s), %d placement(s)"
            % (self.unique_parts, self.total_parts)
        ]
        dnp = [l for l in self.lines if l.dnp]
        if dnp:
            lines.append("  %d do-not-populate line(s)" % len(dnp))
        missing = self.missing_mpn()
        if missing:
            lines.append("  %d part(s) without an MPN: %s"
                         % (len(missing), ", ".join(missing[:10])))
        return "\n".join(lines)


def _natural_sort(refs: Iterable[str]) -> List[str]:
    def key(r: str):
        m = re.match(r"^([A-Za-z_]+)(\d+)$", r)
        return (m.group(1), int(m.group(2))) if m else (r, 0)
    return sorted(set(refs), key=key)


def _consolidate(entries: List[Dict[str, str]],
                 group_by: Sequence[str]) -> List[BomLine]:
    groups: Dict[tuple, BomLine] = {}
    order: List[tuple] = []
    for e in entries:
        key = tuple(e.get(k, "") for k in group_by) + (e.get("dnp", ""),)
        line = groups.get(key)
        if line is None:
            line = BomLine(
                references=[],
                value=e.get("value", ""),
                footprint=e.get("footprint", ""),
                mpn=e.get("mpn", ""),
                manufacturer=e.get("manufacturer", ""),
                description=e.get("description", ""),
                datasheet=e.get("datasheet", ""),
                dnp=bool(e.get("dnp")),
                extra={k: v for k, v in e.items()
                       if k not in ("ref", "value", "footprint", "mpn",
                                    "manufacturer", "description", "datasheet",
                                    "dnp")},
            )
            groups[key] = line
            order.append(key)
        line.references.append(e["ref"])
    return [groups[k] for k in order]


def bom_from_design(design: Design, group_by: Sequence[str] = ("value", "footprint", "mpn"),
                    columns: Optional[Sequence[str]] = None,
                    include_dnp: bool = True) -> Bom:
    """Build a BOM straight from the IR."""
    entries: List[Dict[str, str]] = []
    extra_keys: List[str] = []
    for c in design.components:
        if c.exclude_from_bom:
            continue
        if c.dnp and not include_dnp:
            continue
        e = {
            "ref": c.ref,
            "value": c.value,
            "footprint": c.footprint,
            "mpn": c.mpn,
            "manufacturer": c.manufacturer,
            "description": c.description,
            "datasheet": c.datasheet,
            "dnp": "1" if c.dnp else "",
        }
        for k, v in c.fields.items():
            e[k] = v
            if k not in extra_keys:
                extra_keys.append(k)
        entries.append(e)

    lines = _consolidate(entries, group_by)
    lines.sort(key=lambda l: _natural_sort(l.references)[0] if l.references else "")
    cols = list(columns) if columns else list(DEFAULT_COLUMNS) + extra_keys
    return Bom(lines=lines, source=design.name, columns=cols)


def bom_from_board(path: str,
                   group_by: Sequence[str] = ("value", "footprint", "mpn"),
                   columns: Optional[Sequence[str]] = None,
                   include_dnp: bool = True) -> Bom:
    """Build a BOM by reading footprint properties out of a ``.kicad_pcb``."""
    with open(path, "r", encoding="utf-8") as fh:
        board = sexpr.parse(fh.read())

    entries: List[Dict[str, str]] = []
    extra_keys: List[str] = []
    for fp in sexpr.find_all(board, "footprint"):
        props: Dict[str, str] = {}
        for p in sexpr.find_all(fp, "property"):
            if len(p) >= 3:
                props[sexpr.sexp_str(p[1])] = sexpr.sexp_str(p[2])
        for t in sexpr.find_all(fp, "fp_text"):
            if len(t) >= 3:
                kind = sexpr.sexp_str(t[1])
                if kind == "reference":
                    props.setdefault("Reference", sexpr.sexp_str(t[2]))
                elif kind == "value":
                    props.setdefault("Value", sexpr.sexp_str(t[2]))

        ref = props.get("Reference", "")
        if not ref or ref.startswith("REF"):
            continue

        attrs = sexpr.find(fp, "attr")
        flags = {sexpr.sexp_str(a) for a in (attrs[1:] if attrs else [])}
        if "exclude_from_bom" in flags:
            continue
        dnp = "dnp" in flags
        if dnp and not include_dnp:
            continue

        e = {
            "ref": ref,
            "value": props.get("Value", ""),
            "footprint": sexpr.sexp_str(fp[1]) if len(fp) > 1 else "",
            "mpn": props.get("MPN", ""),
            "manufacturer": props.get("Manufacturer", ""),
            "description": props.get("Description", ""),
            "datasheet": props.get("Datasheet", ""),
            "dnp": "1" if dnp else "",
        }
        for k, v in props.items():
            if k in ("Reference", "Value", "Footprint", "MPN", "Manufacturer",
                     "Description", "Datasheet"):
                continue
            e[k] = v
            if k not in extra_keys:
                extra_keys.append(k)
        entries.append(e)

    lines = _consolidate(entries, group_by)
    lines.sort(key=lambda l: _natural_sort(l.references)[0] if l.references else "")
    cols = list(columns) if columns else list(DEFAULT_COLUMNS) + extra_keys
    return Bom(lines=lines, source=os.path.basename(path), columns=cols)


def write_bom(bom: Bom, path: str, fmt: str = "") -> str:
    """Write ``bom`` to ``path``. Format inferred from the extension if unset."""
    fmt = (fmt or os.path.splitext(path)[1].lstrip(".") or "csv").lower()
    if fmt not in ("csv", "json", "md", "markdown"):
        raise ValueError("bom format must be csv, json or md, got %r" % fmt)
    text = {
        "csv": bom.to_csv,
        "json": bom.to_json,
        "md": bom.to_markdown,
        "markdown": bom.to_markdown,
    }[fmt]()
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path
