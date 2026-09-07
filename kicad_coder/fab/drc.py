"""Run KiCad's design rule check and parse the result.

The Python bindings expose DRC only as ``WriteDRCReport``, which dumps a text
file and tells you nothing structured. ``kicad-cli pcb drc --format json`` is
the better door: it returns severities, rule names, coordinates and affected
items, which is exactly the shape a model can reason about.

``kicad-cli`` returns exit code 5 when it finds violations. That is a
successful run with a non-empty result, not a failure, so it is accepted.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from ..errors import ToolchainError
from .toolchain import Toolchain, find_toolchain, run_cli

__all__ = ["Violation", "DrcResult", "run_drc"]

_VIOLATIONS_EXIT = 5


@dataclass
class Violation:
    """One DRC finding."""

    severity: str  # error | warning | exclusion | ignore
    type: str      # rule id, e.g. "clearance", "unconnected_items"
    description: str
    items: List[str] = field(default_factory=list)
    x_mm: Optional[float] = None
    y_mm: Optional[float] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "severity": self.severity,
            "type": self.type,
            "description": self.description,
            "items": self.items,
            "location_mm": [self.x_mm, self.y_mm]
            if self.x_mm is not None else None,
        }

    def __str__(self) -> str:
        where = ""
        if self.x_mm is not None:
            where = " at (%.2f, %.2f)" % (self.x_mm, self.y_mm)
        return "%-8s %s: %s%s" % (
            self.severity.upper(), self.type, self.description, where
        )


@dataclass
class DrcResult:
    violations: List[Violation]
    unconnected: List[Violation]
    schematic_parity: List[Violation]
    raw: Dict[str, object] = field(default_factory=dict)
    source: str = ""

    @property
    def errors(self) -> List[Violation]:
        return [v for v in self.all() if v.severity == "error"]

    @property
    def warnings(self) -> List[Violation]:
        return [v for v in self.all() if v.severity == "warning"]

    def all(self) -> List[Violation]:
        return self.violations + self.unconnected + self.schematic_parity

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "unconnected_count": len(self.unconnected),
            "violations": [v.to_dict() for v in self.all()],
        }

    def by_type(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for v in self.all():
            counts[v.type] = counts.get(v.type, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    def summary(self, limit: int = 20) -> str:
        lines = [
            "DRC on %s: %d error(s), %d warning(s), %d unconnected"
            % (os.path.basename(self.source), len(self.errors),
               len(self.warnings), len(self.unconnected))
        ]
        counts = self.by_type()
        if counts:
            lines.append("  by type: " + ", ".join(
                "%s x%d" % (k, v) for k, v in list(counts.items())[:8]))
        for v in self.all()[:limit]:
            lines.append("  " + str(v))
        if len(self.all()) > limit:
            lines.append("  ... and %d more" % (len(self.all()) - limit))
        if self.ok and not self.all():
            lines.append("  clean")
        return "\n".join(lines)


def _parse_items(entry: dict) -> List[str]:
    out: List[str] = []
    for item in entry.get("items", []) or []:
        desc = item.get("description") or item.get("uuid") or ""
        if desc:
            out.append(str(desc))
    return out


def _parse_group(entries, kind: str) -> List[Violation]:
    out: List[Violation] = []
    for e in entries or []:
        pos = e.get("pos") or {}
        x = pos.get("x")
        y = pos.get("y")
        out.append(Violation(
            severity=str(e.get("severity", kind)),
            type=str(e.get("type", kind)),
            description=str(e.get("description", "")),
            items=_parse_items(e),
            x_mm=float(x) if isinstance(x, (int, float)) else None,
            y_mm=float(y) if isinstance(y, (int, float)) else None,
        ))
    return out


def run_drc(
    board_path: str,
    toolchain: Optional[Toolchain] = None,
    schematic_parity: bool = False,
    all_track_errors: bool = True,
    severity: str = "all",
    units: str = "mm",
    report_path: str = "",
    timeout: int = 600,
) -> DrcResult:
    """Run DRC on ``board_path`` and return structured violations.

    Raises :class:`~kicad_coder.errors.ToolchainError` when KiCad is not
    installed -- check ``find_toolchain().available`` first if you want to
    degrade gracefully instead.
    """
    tool = toolchain or find_toolchain()
    tool.require()

    board_path = os.path.abspath(board_path)
    if not os.path.isfile(board_path):
        raise ToolchainError("board file not found: %s" % board_path)

    tmp_dir = None
    out_path = report_path
    if not out_path:
        tmp_dir = tempfile.mkdtemp(prefix="kicad_coder_drc_")
        out_path = os.path.join(tmp_dir, "drc.json")

    args = ["pcb", "drc", "--format", "json", "--output", out_path,
            "--units", units, "--exit-code-violations"]
    if all_track_errors:
        args.append("--all-track-errors")
    if schematic_parity:
        args.append("--schematic-parity")
    if severity == "all":
        args.append("--severity-all")
    elif severity == "error":
        args.append("--severity-error")
    elif severity == "warning":
        args.append("--severity-warning")
    args.append(board_path)

    run_cli(tool, args, timeout=timeout,
            ok_returncodes=(0, _VIOLATIONS_EXIT))

    try:
        with open(out_path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise ToolchainError(
            "DRC ran but its report could not be read (%s): %s" % (out_path, exc)
        ) from exc
    finally:
        if tmp_dir and not report_path:
            try:
                os.remove(out_path)
                os.rmdir(tmp_dir)
            except OSError:
                pass

    return DrcResult(
        violations=_parse_group(data.get("violations"), "violation"),
        unconnected=_parse_group(data.get("unconnected_items"), "unconnected"),
        schematic_parity=_parse_group(data.get("schematic_parity"), "parity"),
        raw=data,
        source=board_path,
    )
