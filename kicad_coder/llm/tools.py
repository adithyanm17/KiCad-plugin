"""Provider-agnostic tool definitions and the session that executes them.

Nothing in this module imports a vendor SDK. A :class:`Tool` is a name, a
description, a JSON Schema and a handler; :mod:`kicad_coder.llm.adapters`
reshapes that trio into whatever wire format a given provider wants. Adding a
new provider means writing one adapter function, not touching this file.

The schemas deliberately stay in the intersection of what every major provider
accepts: object/array/string/number/boolean/integer, ``enum``, ``required``,
``description``. No ``oneOf``, ``anyOf``, ``$ref`` or nested
``additionalProperties`` -- those are where cross-provider support breaks.
"""

from __future__ import annotations

import json
import os
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..backends.board import write_board
from ..errors import KiCadCoderError, ToolError
from ..fab.bom import bom_from_design, write_bom
from ..fab.toolchain import Toolchain, find_toolchain
from ..ir.types import (BoardOutline, Component, Design, DesignRules, NetClass,
                        Placement)
from ..ir.validate import validate
from ..library.footprints import FootprintLibrary
from ..place.engine import place
from ..review.rules import review, review_context

__all__ = ["Tool", "ToolResult", "DesignSession", "build_tools", "TOOL_NAMES"]


@dataclass
class ToolResult:
    """What a tool call returned.

    ``content`` is the human/LLM-readable string that goes back into the
    conversation. ``data`` carries the same information structurally for
    callers that want to inspect it programmatically.
    """

    ok: bool
    content: str
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: str = ""

    def to_dict(self) -> Dict[str, Any]:
        out: Dict[str, Any] = {"ok": self.ok, "content": self.content}
        if self.data:
            out["data"] = self.data
        if self.error_code:
            out["error_code"] = self.error_code
        return out

    def __str__(self) -> str:
        return self.content


@dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]
    handler: Callable[["DesignSession", Dict[str, Any]], ToolResult]
    mutates: bool = False

    def schema(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
        }


def _obj(properties: Dict[str, Any], required: Sequence[str] = ()) -> Dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
    }


def _str(desc: str, enum: Optional[Sequence[str]] = None,
         default: Optional[str] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": "string", "description": desc}
    if enum:
        out["enum"] = list(enum)
    if default is not None:
        out["description"] += " Default: %s." % default
    return out


def _num(desc: str, default: Optional[float] = None) -> Dict[str, Any]:
    out = {"type": "number", "description": desc}
    if default is not None:
        out["description"] += " Default: %g." % default
    return out


def _int(desc: str, default: Optional[int] = None) -> Dict[str, Any]:
    out = {"type": "integer", "description": desc}
    if default is not None:
        out["description"] += " Default: %d." % default
    return out


def _bool(desc: str, default: Optional[bool] = None) -> Dict[str, Any]:
    out = {"type": "boolean", "description": desc}
    if default is not None:
        out["description"] += " Default: %s." % str(default).lower()
    return out


class DesignSession:
    """Holds one design and executes tool calls against it.

    A session is the unit an agent loop drives: create it, hand
    :meth:`tool_schemas` to the model, and route every tool call the model
    makes through :meth:`call`.
    """

    def __init__(
        self,
        design: Optional[Design] = None,
        library: Optional[FootprintLibrary] = None,
        toolchain: Optional[Toolchain] = None,
        work_dir: str = "build",
        kicad_version: str = "9.0",
    ) -> None:
        self.design = design or Design()
        self.library = library or FootprintLibrary()
        self.toolchain = toolchain if toolchain is not None else find_toolchain()
        self.work_dir = os.path.abspath(work_dir)
        self.kicad_version = kicad_version
        self.history: List[Dict[str, Any]] = []
        self._tools: Dict[str, Tool] = {t.name: t for t in build_tools()}

    # -- tool plumbing ----------------------------------------------------

    @property
    def tools(self) -> List[Tool]:
        return list(self._tools.values())

    def tool_schemas(self) -> List[Dict[str, Any]]:
        return [t.schema() for t in self.tools]

    def call(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> ToolResult:
        """Execute one tool call. Never raises -- failures come back as results.

        A model that gets an exception traceback learns nothing; a model that
        gets "footprint X not found, closest matches are Y and Z" fixes itself
        on the next turn. So every error is caught and turned into guidance.
        """
        args = dict(arguments or {})
        tool = self._tools.get(name)
        if tool is None:
            close = [n for n in self._tools if name.lower() in n.lower()]
            return ToolResult(
                False,
                "No tool named %r. Available tools: %s%s"
                % (name, ", ".join(sorted(self._tools)),
                   ("  Did you mean: %s?" % ", ".join(close)) if close else ""),
                error_code="unknown_tool",
            )

        try:
            result = tool.handler(self, args)
        except ToolError as exc:
            result = ToolResult(False, str(exc), error_code=exc.code)
        except KiCadCoderError as exc:
            result = ToolResult(False, str(exc), error_code="library_error")
        except (TypeError, ValueError, KeyError) as exc:
            result = ToolResult(
                False, "%s: %s" % (type(exc).__name__, exc),
                error_code="bad_arguments")
        except Exception as exc:  # pragma: no cover - unexpected
            result = ToolResult(
                False,
                "internal error in %s: %s\n%s"
                % (name, exc, traceback.format_exc(limit=3)),
                error_code="internal_error")

        self.history.append({
            "tool": name, "arguments": args,
            "ok": result.ok, "content": result.content[:2000],
        })
        return result

    def _path(self, path: str, default_name: str, suffix: str) -> str:
        if not path:
            path = os.path.join(self.work_dir, default_name + suffix)
        if not os.path.isabs(path):
            path = os.path.join(self.work_dir, path)
        return os.path.abspath(path)


# -- handlers -------------------------------------------------------------


def _h_create_design(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    name = str(a.get("name") or "untitled").strip()
    d = Design(name=name, description=str(a.get("description", "")))
    d.outline = BoardOutline(
        shape="rect",
        width_mm=float(a.get("width_mm", 50)),
        height_mm=float(a.get("height_mm", 50)),
    )
    d.rules = DesignRules(copper_layers=int(a.get("copper_layers", 2)))
    s.design = d
    return ToolResult(True, "Created design %r: %g x %g mm, %d copper layers."
                      % (name, d.outline.width_mm, d.outline.height_mm,
                         d.rules.copper_layers),
                      {"name": name})


def _h_set_board_outline(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    shape = str(a.get("shape", "rect"))
    if shape == "polygon":
        pts = [(float(p["x_mm"]), float(p["y_mm"])) for p in a.get("points", [])]
        s.design.outline = BoardOutline(shape="polygon", points=pts)
        return ToolResult(True, "Board outline set to a %d-point polygon "
                          "(%.1f mm2)." % (len(pts), s.design.outline.area_mm2()))
    s.design.outline = BoardOutline(
        shape="rect",
        width_mm=float(a.get("width_mm", 50)),
        height_mm=float(a.get("height_mm", 50)),
        corner_radius_mm=float(a.get("corner_radius_mm", 0)),
    )
    return ToolResult(True, "Board outline set to %g x %g mm."
                      % (s.design.outline.width_mm, s.design.outline.height_mm))


def _h_set_design_rules(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    r = s.design.rules
    changed = []
    for key in ("copper_layers", "board_thickness_mm", "copper_weight_oz",
                "min_track_mm", "min_clearance_mm", "min_via_diameter_mm",
                "min_via_drill_mm", "min_hole_to_hole_mm",
                "min_annular_ring_mm", "edge_clearance_mm",
                "allow_blind_buried_vias", "allow_microvias"):
        if key in a and a[key] is not None:
            value = a[key]
            if key == "copper_layers":
                value = int(value)
            elif key.startswith("allow_"):
                value = bool(value)
            else:
                value = float(value)
            setattr(r, key, value)
            changed.append("%s=%s" % (key, value))
    # re-run the dataclass invariants
    s.design.rules = DesignRules(**{
        f: getattr(r, f) for f in r.__dataclass_fields__
    })
    if not changed:
        return ToolResult(False, "No design rule fields were supplied.",
                          error_code="bad_arguments")
    return ToolResult(True, "Design rules updated: %s" % ", ".join(changed))


def _h_add_net_class(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    name = str(a.get("name", "")).strip()
    if not name:
        raise ToolError("net class needs a name", "bad_arguments")
    nc = NetClass(
        name=name,
        track_width_mm=float(a.get("track_width_mm", 0.25)),
        clearance_mm=float(a.get("clearance_mm", 0.2)),
        via_diameter_mm=float(a.get("via_diameter_mm", 0.6)),
        via_drill_mm=float(a.get("via_drill_mm", 0.3)),
        diff_pair_width_mm=float(a.get("diff_pair_width_mm", 0.2)),
        diff_pair_gap_mm=float(a.get("diff_pair_gap_mm", 0.25)),
        description=str(a.get("description", "")),
    )
    s.design.rules.net_classes = [
        x for x in s.design.rules.net_classes if x.name != name
    ] + [nc]
    return ToolResult(True, "Net class %r: %g mm tracks, %g mm clearance, "
                      "%g/%g mm vias." % (name, nc.track_width_mm,
                                          nc.clearance_mm, nc.via_diameter_mm,
                                          nc.via_drill_mm))


def _h_search_footprints(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    query = str(a.get("query", "")).strip()
    if not query:
        raise ToolError("search_footprints needs a query", "bad_arguments")
    limit = int(a.get("limit", 15))
    hits = s.library.search(query, limit=limit, library=str(a.get("library", "")))
    if not hits:
        return ToolResult(
            True,
            "No footprints matched %r. The library holds %d footprints across "
            "%d libraries; try a broader term such as the package name alone "
            "('0603', 'SOIC', 'header')."
            % (query, len(s.library.libids()), len(s.library.libraries())),
            {"results": []})
    lines = ["%d match(es) for %r:" % (len(hits), query)]
    details = []
    for libid in hits:
        info = s.library.describe(libid)
        details.append(info)
        lines.append("  %s  (%d pads: %s)"
                     % (libid, info["pad_count"],
                        ", ".join(str(p) for p in info["pads"][:8])))
    return ToolResult(True, "\n".join(lines), {"results": details})


def _h_get_footprint(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    libid = str(a.get("footprint", "")).strip()
    info = s.library.describe(libid)
    return ToolResult(
        True,
        "%s\n  %s\n  pads (%d): %s\n  courtyard: %g x %g mm"
        % (info["libid"], info["description"] or "(no description)",
           info["pad_count"], ", ".join(str(p) for p in info["pads"]),
           info["size_mm"][0], info["size_mm"][1]),
        info)


def _h_add_components(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    items = a.get("components") or []
    if not isinstance(items, list) or not items:
        raise ToolError(
            "add_components needs a non-empty 'components' array", "bad_arguments")

    added, failed = [], []
    for raw in items:
        if not isinstance(raw, dict):
            failed.append("%r is not an object" % (raw,))
            continue
        try:
            comp = Component(
                ref=str(raw["ref"]),
                value=str(raw.get("value", "")),
                footprint=str(raw["footprint"]),
                mpn=str(raw.get("mpn", "")),
                manufacturer=str(raw.get("manufacturer", "")),
                datasheet=str(raw.get("datasheet", "")),
                description=str(raw.get("description", "")),
                dnp=bool(raw.get("dnp", False)),
                exclude_from_bom=bool(raw.get("exclude_from_bom", False)),
                side=str(raw.get("side", "top")),
                group=str(raw.get("group", "")),
                near=[str(x) for x in (raw.get("near") or [])],
                fields={str(k): str(v) for k, v in (raw.get("fields") or {}).items()},
            )
        except (KeyError, ValueError) as exc:
            failed.append("%s: %s" % (raw.get("ref", "?"), exc))
            continue

        if not s.library.exists(comp.footprint):
            near = s.library.search(comp.footprint.split(":")[-1], limit=3)
            failed.append(
                "%s: footprint %r not found%s"
                % (comp.ref, comp.footprint,
                   ("; closest: %s" % ", ".join(near)) if near else ""))
            continue

        try:
            s.design.add_component(comp, replace=bool(a.get("replace", False)))
            added.append(comp.ref)
        except ValueError as exc:
            failed.append(str(exc))

    msg = "Added %d component(s): %s" % (len(added), ", ".join(added)) if added \
        else "No components were added."
    if failed:
        msg += "\n%d failed:\n  %s" % (len(failed), "\n  ".join(failed))
    return ToolResult(bool(added) or not failed, msg,
                      {"added": added, "failed": failed})


def _h_remove_component(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    ref = str(a.get("ref", "")).strip().upper()
    if s.design.component(ref) is None:
        raise ToolError("no component named %r" % ref, "unknown_ref")
    s.design.remove_component(ref)
    return ToolResult(True, "Removed %s and any connections to it." % ref)


def _h_connect(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    nets = a.get("nets") or []
    if not isinstance(nets, list) or not nets:
        raise ToolError(
            "connect needs a non-empty 'nets' array, each entry being "
            "{name, connections:[{ref, pad}], net_class}", "bad_arguments")

    made, failed = [], []
    for raw in nets:
        name = str(raw.get("name", "")).strip()
        conns = raw.get("connections") or []
        if not name:
            failed.append("a net entry is missing 'name'")
            continue
        pairs = []
        for c in conns:
            try:
                pairs.append((str(c["ref"]), str(c["pad"])))
            except (KeyError, TypeError):
                failed.append("%s: connection %r needs 'ref' and 'pad'"
                              % (name, c))
        if not pairs:
            continue
        try:
            net = s.design.connect(name, pairs,
                                   net_class=str(raw.get("net_class", "Default")))
            made.append("%s (%d pins)" % (net.name, len(net.connections)))
        except ValueError as exc:
            failed.append("%s: %s" % (name, exc))

    msg = "Connected %d net(s): %s" % (len(made), ", ".join(made)) if made \
        else "No nets were connected."
    if failed:
        msg += "\nProblems:\n  " + "\n  ".join(failed)
    return ToolResult(bool(made), msg, {"nets": made, "failed": failed})


def _h_remove_net(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    name = str(a.get("name", "")).strip()
    net = s.design.net(name)
    if net is None:
        raise ToolError("no net named %r" % name, "unknown_net")
    s.design.nets.remove(net)
    return ToolResult(True, "Removed net %s." % name)


def _h_place_components(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    result = place(
        s.design, s.library,
        strategy=str(a.get("strategy", "force")),
        seed=int(a.get("seed", 0)),
        iterations=int(a.get("iterations", 400)),
        keep_locked=bool(a.get("keep_locked", True)),
    )
    data = {"ok": result.ok, "overlaps": result.overlaps,
            "outside": result.outside}
    if not result.ok:
        return ToolResult(
            False,
            result.summary() + "\n\nEnlarge the board outline with "
            "set_board_outline, or move parts to the bottom side.",
            data, error_code="placement_failed")
    return ToolResult(True, result.summary(), data)


def _h_set_placement(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    ref = str(a.get("ref", "")).strip().upper()
    if s.design.component(ref) is None:
        raise ToolError("no component named %r" % ref, "unknown_ref")
    p = Placement(
        ref=ref,
        x_mm=float(a.get("x_mm", 0)),
        y_mm=float(a.get("y_mm", 0)),
        rotation_deg=float(a.get("rotation_deg", 0)),
        side=str(a.get("side", "top")),
        locked=bool(a.get("locked", True)),
    )
    s.design.placements[ref] = p
    return ToolResult(True, "%s pinned at (%.2f, %.2f) rot %g on the %s side%s."
                      % (ref, p.x_mm, p.y_mm, p.rotation_deg, p.side,
                         ", locked" if p.locked else ""))


def _h_validate(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    res = validate(s.design, library=s.library,
                   strict=bool(a.get("strict", False)))
    return ToolResult(res.ok, str(res), res.to_dict(),
                      error_code="" if res.ok else "invalid_design")


def _h_review(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    findings = review(s.design, s.library)
    ctx = review_context(s.design, s.library)
    if findings:
        body = "\n".join(str(f) for f in findings)
        msg = "Review found %d item(s):\n%s" % (len(findings), body)
    else:
        msg = "Review found no issues."
    msg += "\n\nDesign facts (use these to write the review):\n%s" % json.dumps(
        ctx, indent=2)
    return ToolResult(True, msg,
                      {"findings": [f.to_dict() for f in findings],
                       "context": ctx})


def _h_describe_design(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    d = s.design
    lines = [d.summary(), ""]
    if d.components:
        lines.append("Components:")
        for c in d.components:
            pads = d.pads_of(c.ref)
            lines.append("  %-5s %-14s %-45s %s"
                         % (c.ref, c.value, c.footprint,
                            "%d net(s)" % len(pads)))
    if d.nets:
        lines.append("")
        lines.append("Nets:")
        for n in d.nets:
            lines.append("  %-12s [%s] %s"
                         % (n.name, n.net_class,
                            " ".join("%s.%s" % p for p in n.connections)))
    if d.notes:
        lines.append("")
        lines.append("Notes:")
        lines.extend("  - " + n for n in d.notes)
    return ToolResult(True, "\n".join(lines), d.to_dict())


def _h_add_note(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    text = str(a.get("text", "")).strip()
    if not text:
        raise ToolError("add_note needs 'text'", "bad_arguments")
    s.design.notes.append(text)
    return ToolResult(True, "Note recorded (%d total)." % len(s.design.notes))


def _h_generate_board(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    path = s._path(str(a.get("path", "")), s.design.name, ".kicad_pcb")
    if not s.design.placements:
        place(s.design, s.library, seed=0)
    res = write_board(
        s.design, s.library, path,
        version=str(a.get("kicad_version", s.kicad_version)),
        ground_planes=bool(a.get("ground_planes", True)),
    )
    return ToolResult(True, res.summary(),
                      {"pcb_path": res.pcb_path, "pro_path": res.pro_path})


def _h_generate_bom(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    fmt = str(a.get("format", "csv")).lower()
    ext = {"csv": ".csv", "json": ".json", "md": ".md", "markdown": ".md"}.get(fmt)
    if ext is None:
        raise ToolError("format must be csv, json or md", "bad_arguments")
    path = s._path(str(a.get("path", "")), s.design.name + "-bom", ext)
    bom = bom_from_design(s.design, include_dnp=bool(a.get("include_dnp", True)))
    write_bom(bom, path, fmt=fmt)
    body = bom.to_markdown() if len(bom.lines) <= 40 else ""
    return ToolResult(True, "%s\n  wrote %s%s"
                      % (bom.summary(), path, ("\n\n" + body) if body else ""),
                      {"path": path, "lines": bom.rows(),
                       "missing_mpn": bom.missing_mpn()})


def _h_run_drc(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    if not s.toolchain.available:
        return ToolResult(
            False,
            "DRC needs a KiCad installation and none was found. Everything "
            "else (design, board generation, review, BOM) still works. "
            "Install KiCad or set KICAD_HOME.",
            error_code="no_toolchain")
    from ..fab.drc import run_drc
    path = s._path(str(a.get("path", "")), s.design.name, ".kicad_pcb")
    if not os.path.isfile(path):
        raise ToolError("no board at %s -- run generate_board first" % path,
                        "missing_board")
    res = run_drc(path, toolchain=s.toolchain,
                  schematic_parity=bool(a.get("schematic_parity", False)))
    return ToolResult(res.ok, res.summary(), res.to_dict(),
                      error_code="" if res.ok else "drc_violations")


def _h_export_fab(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    if not s.toolchain.available:
        return ToolResult(
            False,
            "Fabrication export needs a KiCad installation and none was found.",
            error_code="no_toolchain")
    from ..fab.exports import export_fab_package
    board = s._path(str(a.get("path", "")), s.design.name, ".kicad_pcb")
    out_dir = str(a.get("out_dir", "")) or os.path.join(s.work_dir, "fab")
    res = export_fab_package(board, out_dir,
                             copper_layers=s.design.rules.copper_layers,
                             toolchain=s.toolchain)
    return ToolResult(True, res.summary(),
                      {"files": res.files, "archive": res.archive})


def _h_save_design(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    path = s._path(str(a.get("path", "")), s.design.name, ".json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(s.design.to_dict(), fh, indent=2)
    return ToolResult(True, "Design saved to %s" % path, {"path": path})


def _h_load_design(s: DesignSession, a: Dict[str, Any]) -> ToolResult:
    path = s._path(str(a.get("path", "")), s.design.name, ".json")
    if not os.path.isfile(path):
        raise ToolError("no design file at %s" % path, "missing_file")
    with open(path, "r", encoding="utf-8") as fh:
        s.design = Design.from_dict(json.load(fh))
    return ToolResult(True, "Loaded design from %s\n%s"
                      % (path, s.design.summary()))


# -- registry -------------------------------------------------------------


_COMPONENT_ITEM = {
    "type": "object",
    "properties": {
        "ref": _str("Reference designator, letters then digits: R1, U3, C12."),
        "value": _str("Component value or part name: '10k', '100n', 'ATtiny85'."),
        "footprint": _str(
            "KiCad footprint id 'Library:Name'. Must exist -- call "
            "search_footprints first if unsure."),
        "mpn": _str("Manufacturer part number. Include it whenever known: "
                    "a BOM without MPNs cannot be ordered."),
        "manufacturer": _str("Manufacturer name."),
        "datasheet": _str("Datasheet URL."),
        "description": _str("One-line description for the BOM."),
        "dnp": _bool("Do not populate this part.", False),
        "exclude_from_bom": _bool("Leave off the BOM (mounting holes, "
                                  "fiducials).", False),
        "side": _str("Board side.", ["top", "bottom"], "top"),
        "group": _str("Placement grouping hint: 'power', 'mcu', 'analog'."),
        "near": {
            "type": "array",
            "description": "References this part should be placed close to. "
                           "Use it for decoupling capacitors and crystals.",
            "items": {"type": "string"},
        },
        "fields": {
            "type": "object",
            "description": "Extra BOM columns, e.g. {'Tolerance': '1%'}.",
            "properties": {},
        },
    },
    "required": ["ref", "footprint"],
}

_CONNECTION_ITEM = {
    "type": "object",
    "properties": {
        "ref": _str("Component reference, e.g. 'U1'."),
        "pad": _str("Pad number as a string, e.g. '1' or 'A4'."),
    },
    "required": ["ref", "pad"],
}

_NET_ITEM = {
    "type": "object",
    "properties": {
        "name": _str("Net name. Use GND for ground and +3V3 / +5V style names "
                     "for supplies so tools recognise them."),
        "connections": {
            "type": "array",
            "description": "Pins on this net. A net needs at least two.",
            "items": _CONNECTION_ITEM,
        },
        "net_class": _str("Net class name.", None, "Default"),
    },
    "required": ["name", "connections"],
}


def build_tools() -> List[Tool]:
    """Construct the full tool set. Order is the order a model should use."""
    return [
        Tool("create_design",
             "Start a new PCB design. Call this first. Replaces any design "
             "currently in the session.",
             _obj({
                 "name": _str("Short project name, used for filenames."),
                 "description": _str("What this board does."),
                 "width_mm": _num("Board width in mm.", 50),
                 "height_mm": _num("Board height in mm.", 50),
                 "copper_layers": _int("Copper layer count: 2 for simple "
                                       "boards, 4 when you need ground and "
                                       "power planes.", 2),
             }, ["name"]),
             _h_create_design, mutates=True),

        Tool("search_footprints",
             "Find footprint library ids by keyword. ALWAYS call this before "
             "adding a component unless you are certain the id exists -- an "
             "invented footprint id will fail validation.",
             _obj({
                 "query": _str("Keywords: '0603 resistor', 'SOIC-8', "
                               "'pin header 1x04'."),
                 "limit": _int("Maximum results.", 15),
                 "library": _str("Restrict to one library name."),
             }, ["query"]),
             _h_search_footprints),

        Tool("get_footprint",
             "Show a footprint's pad numbers and physical size. Use it to "
             "learn which pad numbers are legal before wiring nets.",
             _obj({"footprint": _str("Footprint id 'Library:Name'.")},
                  ["footprint"]),
             _h_get_footprint),

        Tool("add_components",
             "Add one or more components. Batch them into a single call. "
             "Every footprint must resolve or that component is rejected.",
             _obj({
                 "components": {
                     "type": "array",
                     "description": "Components to add.",
                     "items": _COMPONENT_ITEM,
                 },
                 "replace": _bool("Overwrite a component with the same "
                                  "reference instead of failing.", False),
             }, ["components"]),
             _h_add_components, mutates=True),

        Tool("remove_component",
             "Delete a component and every connection to it.",
             _obj({"ref": _str("Reference designator.")}, ["ref"]),
             _h_remove_component, mutates=True),

        Tool("connect",
             "Create or extend nets. Batch every net into one call. A pad may "
             "belong to exactly one net -- connecting it twice is a short and "
             "will fail validation.",
             _obj({
                 "nets": {
                     "type": "array",
                     "description": "Nets to create or extend.",
                     "items": _NET_ITEM,
                 },
             }, ["nets"]),
             _h_connect, mutates=True),

        Tool("remove_net",
             "Delete a net entirely.",
             _obj({"name": _str("Net name.")}, ["name"]),
             _h_remove_net, mutates=True),

        Tool("set_board_outline",
             "Set the board edge, as a rectangle or an explicit polygon.",
             _obj({
                 "shape": _str("Outline type.", ["rect", "polygon"], "rect"),
                 "width_mm": _num("Width, for shape=rect."),
                 "height_mm": _num("Height, for shape=rect."),
                 "corner_radius_mm": _num("Corner radius, for shape=rect.", 0),
                 "points": {
                     "type": "array",
                     "description": "Polygon vertices, for shape=polygon.",
                     "items": {
                         "type": "object",
                         "properties": {"x_mm": _num("X"), "y_mm": _num("Y")},
                         "required": ["x_mm", "y_mm"],
                     },
                 },
             }),
             _h_set_board_outline, mutates=True),

        Tool("set_design_rules",
             "Set fabrication constraints. Only the fields you pass change. "
             "Defaults describe a conservative 2-layer process every fab "
             "house can build.",
             _obj({
                 "copper_layers": _int("Copper layers: 1, 2, 4, 6..."),
                 "board_thickness_mm": _num("Board thickness.", 1.6),
                 "copper_weight_oz": _num("Copper weight in oz.", 1.0),
                 "min_track_mm": _num("Minimum track width.", 0.2),
                 "min_clearance_mm": _num("Minimum copper clearance.", 0.2),
                 "min_via_diameter_mm": _num("Minimum via pad diameter.", 0.6),
                 "min_via_drill_mm": _num("Minimum via drill.", 0.3),
                 "min_hole_to_hole_mm": _num("Minimum hole-to-hole.", 0.25),
                 "min_annular_ring_mm": _num("Minimum annular ring.", 0.13),
                 "edge_clearance_mm": _num("Copper-to-board-edge.", 0.3),
                 "allow_blind_buried_vias": _bool("Permit blind/buried vias. "
                                                  "Expensive.", False),
                 "allow_microvias": _bool("Permit microvias. Expensive.", False),
             }),
             _h_set_design_rules, mutates=True),

        Tool("add_net_class",
             "Define a net class with its own track width and clearance, then "
             "assign nets to it via the net_class field on connect. Use one "
             "for power rails.",
             _obj({
                 "name": _str("Class name, e.g. 'Power' or 'USB'."),
                 "track_width_mm": _num("Track width.", 0.25),
                 "clearance_mm": _num("Clearance.", 0.2),
                 "via_diameter_mm": _num("Via diameter.", 0.6),
                 "via_drill_mm": _num("Via drill.", 0.3),
                 "diff_pair_width_mm": _num("Differential pair width.", 0.2),
                 "diff_pair_gap_mm": _num("Differential pair gap.", 0.25),
                 "description": _str("What this class is for."),
             }, ["name"]),
             _h_add_net_class, mutates=True),

        Tool("place_components",
             "Compute physical positions for every component. Do not try to "
             "place parts yourself with coordinates -- this solver handles "
             "overlap, board bounds and net-driven clustering.",
             _obj({
                 "strategy": _str("Placement algorithm.",
                                  ["force", "grid"], "force"),
                 "seed": _int("Random seed; same seed gives the same result.", 0),
                 "iterations": _int("Relaxation steps.", 400),
                 "keep_locked": _bool("Preserve positions pinned with "
                                      "set_placement.", True),
             }),
             _h_place_components, mutates=True),

        Tool("set_placement",
             "Pin one component to an exact position and lock it, so "
             "place_components works around it. Use for connectors and "
             "mounting holes that must land in a specific spot.",
             _obj({
                 "ref": _str("Reference designator."),
                 "x_mm": _num("X position in mm from the board origin."),
                 "y_mm": _num("Y position in mm."),
                 "rotation_deg": _num("Rotation in degrees.", 0),
                 "side": _str("Board side.", ["top", "bottom"], "top"),
                 "locked": _bool("Keep this position when placing.", True),
             }, ["ref", "x_mm", "y_mm"]),
             _h_set_placement, mutates=True),

        Tool("validate_design",
             "Check the design for structural errors: unknown footprints, "
             "invalid pad numbers, shorted pins, rule violations. Call this "
             "before generating a board and fix every error it reports.",
             _obj({"strict": _bool("Also report missing MPNs and unused pins.",
                                   False)}),
             _h_validate),

        Tool("review_design",
             "Run an engineering review and return both the findings and a "
             "structured set of design facts. Use the facts to write your own "
             "review -- do not assert problems the data does not show.",
             _obj({}),
             _h_review),

        Tool("describe_design",
             "Print the full current state: components, nets and notes. Use it "
             "to re-orient before making changes.",
             _obj({}),
             _h_describe_design),

        Tool("add_note",
             "Record a note for the human reviewer -- an assumption you made, "
             "a part that needs checking, a decision you deferred.",
             _obj({"text": _str("The note.")}, ["text"]),
             _h_add_note, mutates=True),

        Tool("generate_board",
             "Write the .kicad_pcb and .kicad_pro files. Places components "
             "automatically if that has not been done yet. The board will "
             "have footprints, nets and a ground pour, but no routed tracks.",
             _obj({
                 "path": _str("Output path. Defaults to <name>.kicad_pcb in "
                              "the work directory."),
                 "kicad_version": _str("Target file format.",
                                       ["8.0", "9.0"], "9.0"),
                 "ground_planes": _bool("Add a ground zone.", True),
             }),
             _h_generate_board),

        Tool("generate_bom",
             "Write the bill of materials, consolidated by part.",
             _obj({
                 "path": _str("Output path."),
                 "format": _str("Output format.", ["csv", "json", "md"], "csv"),
                 "include_dnp": _bool("Include do-not-populate parts.", True),
             }),
             _h_generate_bom),

        Tool("run_drc",
             "Run KiCad's design rule check on the generated board. Requires "
             "a KiCad installation. Expect unconnected-item violations until "
             "the board is routed.",
             _obj({
                 "path": _str("Board file. Defaults to the generated board."),
                 "schematic_parity": _bool("Also check against a schematic.",
                                           False),
             }),
             _h_run_drc),

        Tool("export_fab",
             "Export gerbers, drill files and pick-and-place, zipped for a "
             "fab house. Requires a KiCad installation.",
             _obj({
                 "path": _str("Board file."),
                 "out_dir": _str("Output directory."),
             }),
             _h_export_fab),

        Tool("save_design",
             "Save the design IR as JSON so it can be reloaded later.",
             _obj({"path": _str("Output path.")}),
             _h_save_design),

        Tool("load_design",
             "Load a design IR previously written by save_design.",
             _obj({"path": _str("Input path.")}, ["path"]),
             _h_load_design, mutates=True),
    ]


TOOL_NAMES = [t.name for t in build_tools()]
