"""Read a live or on-disk KiCad board into the IR, using ``pcbnew``.

This is the only module that imports ``pcbnew``, and it imports it lazily. It
exists so the plugin can review the board a user already has open, and so an
existing ``.kicad_pcb`` from any source can be pulled into the IR for review
and BOM generation.

The SWIG API shifts between KiCad releases, so every accessor here is probed
with :func:`_try` rather than called directly. A method that disappeared in a
point release degrades to a missing field, not a crash inside someone's PCB
editor.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from ..errors import BackendError
from ..ir.types import (BoardOutline, Component, Design, DesignRules, NetClass,
                        Placement)

__all__ = ["pcbnew_available", "board_to_design", "load_board_file"]


def pcbnew_available() -> bool:
    try:
        import pcbnew  # noqa: F401
        return True
    except ImportError:
        return False


def _pcbnew():
    try:
        import pcbnew
        return pcbnew
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise BackendError(
            "the pcbnew module is not importable. It only exists inside "
            "KiCad's bundled Python -- run this from a KiCad plugin or "
            "KiCad's own interpreter. Everything except this module works "
            "without it."
        ) from exc


def _try(fn: Callable, default: Any = None) -> Any:
    """Call ``fn``, returning ``default`` if the API is absent or unhappy."""
    try:
        return fn()
    except Exception:
        return default


def _to_mm(pcbnew, value) -> float:
    return float(_try(lambda: pcbnew.ToMM(value), 0.0) or 0.0)


def _fp_id(fp) -> str:
    for getter in ("GetFPIDAsString", "GetFPID"):
        val = _try(lambda: getattr(fp, getter)())
        if val is None:
            continue
        text = str(val if isinstance(val, str) else _try(lambda: val.Format(), ""))
        if text:
            return text
    return ""


def _fp_fields(fp) -> Dict[str, str]:
    """Every custom field on a footprint, keyed by name."""
    out: Dict[str, str] = {}
    fields = _try(lambda: fp.GetFields(), None)
    if not fields:
        return out
    for f in fields:
        name = str(_try(lambda: f.GetName(), "") or "")
        text = str(_try(lambda: f.GetText(), "") or "")
        if name and name not in ("Reference", "Value", "Footprint"):
            out[name] = text
    return out


def _fp_flag(pcbnew, fp, const_name: str) -> bool:
    const = getattr(pcbnew, const_name, None)
    if const is None:
        return False
    attrs = _try(lambda: fp.GetAttributes(), 0) or 0
    return bool(attrs & const)


def board_to_design(board, name: str = "") -> Design:
    """Convert a ``pcbnew.BOARD`` into a :class:`Design`.

    Tracks, vias and zones are not represented -- the IR describes intent, and
    routing is deliberately outside it. What comes back is the netlist, the
    component list with BOM fields, the placements and the design rules, which
    is everything the review and BOM paths need.
    """
    pcbnew = _pcbnew()

    design = Design(name=name or str(_try(lambda: board.GetFileName(), "") or
                                     "board").split("/")[-1].split("\\")[-1]
                    .replace(".kicad_pcb", "") or "board")

    # -- components and placements ---------------------------------------
    nets: Dict[str, List] = {}
    for fp in _try(lambda: list(board.GetFootprints()), []) or []:
        ref = str(_try(lambda: fp.GetReference(), "") or "")
        if not ref:
            continue

        fields = _fp_fields(fp)
        layer = _try(lambda: fp.GetLayer(), 0)
        bottom = layer == getattr(pcbnew, "B_Cu", -999)

        try:
            comp = Component(
                ref=ref,
                value=str(_try(lambda: fp.GetValue(), "") or ""),
                footprint=_fp_id(fp) or "Unknown:Unknown",
                mpn=fields.pop("MPN", "") or fields.pop("mpn", ""),
                manufacturer=fields.pop("Manufacturer", ""),
                datasheet=fields.pop("Datasheet", ""),
                description=str(_try(lambda: fp.GetLibDescription(), "") or ""),
                dnp=_fp_flag(pcbnew, fp, "FP_DNP"),
                exclude_from_bom=_fp_flag(pcbnew, fp, "FP_EXCLUDE_FROM_BOM"),
                exclude_from_pos=_fp_flag(pcbnew, fp, "FP_EXCLUDE_FROM_POS_FILES"),
                side="bottom" if bottom else "top",
                fields=fields,
            )
        except ValueError:
            # A reference like "REF**" or "?" is not a usable designator.
            continue

        design.add_component(comp, replace=True)

        pos = _try(lambda: fp.GetPosition(), None)
        if pos is not None:
            design.placements[comp.ref] = Placement(
                ref=comp.ref,
                x_mm=_to_mm(pcbnew, _try(lambda: pos.x, 0)),
                y_mm=_to_mm(pcbnew, _try(lambda: pos.y, 0)),
                rotation_deg=float(_try(lambda: fp.GetOrientationDegrees(), 0.0) or 0.0),
                side=comp.side,
            )

        for pad in _try(lambda: list(fp.Pads()), []) or []:
            number = str(_try(lambda: pad.GetNumber(), "") or "")
            netname = str(_try(lambda: pad.GetNetname(), "") or "")
            if number and netname:
                nets.setdefault(netname, []).append((comp.ref, number))

    for netname, conns in nets.items():
        try:
            design.connect(netname, conns)
        except ValueError:
            continue  # a net name KiCad allows but the IR does not

    # -- outline ----------------------------------------------------------
    bbox = _try(lambda: board.GetBoardEdgesBoundingBox(), None)
    if bbox is not None:
        w = _to_mm(pcbnew, _try(lambda: bbox.GetWidth(), 0))
        h = _to_mm(pcbnew, _try(lambda: bbox.GetHeight(), 0))
        x = _to_mm(pcbnew, _try(lambda: bbox.GetX(), 0))
        y = _to_mm(pcbnew, _try(lambda: bbox.GetY(), 0))
        if w > 0 and h > 0:
            design.outline = BoardOutline(shape="rect", width_mm=w, height_mm=h,
                                          origin_x_mm=x, origin_y_mm=y)

    # -- design rules -----------------------------------------------------
    ds = _try(lambda: board.GetDesignSettings(), None)
    rules = DesignRules(
        copper_layers=int(_try(lambda: board.GetCopperLayerCount(), 2) or 2),
    )
    if ds is not None:
        for attr, field in (("m_TrackMinWidth", "min_track_mm"),
                            ("m_MinClearance", "min_clearance_mm"),
                            ("m_ViasMinSize", "min_via_diameter_mm"),
                            ("m_MinThroughDrill", "min_via_drill_mm"),
                            ("m_HoleToHoleMin", "min_hole_to_hole_mm"),
                            ("m_CopperEdgeClearance", "edge_clearance_mm")):
            raw = _try(lambda a=attr: getattr(ds, a))
            if raw is not None:
                mm = _to_mm(pcbnew, raw)
                if mm > 0:
                    setattr(rules, field, mm)
    design.rules = rules

    # -- net classes ------------------------------------------------------
    classes: List[NetClass] = []
    netclasses = _try(lambda: board.GetNetClasses(), None)
    if netclasses:
        for item in _try(lambda: list(netclasses), []) or []:
            nc_name = str(item)
            nc = _try(lambda: netclasses[nc_name], None)
            if nc is None:
                continue
            classes.append(NetClass(
                name=nc_name,
                track_width_mm=_to_mm(pcbnew, _try(lambda: nc.GetTrackWidth(), 0)) or 0.25,
                clearance_mm=_to_mm(pcbnew, _try(lambda: nc.GetClearance(), 0)) or 0.2,
                via_diameter_mm=_to_mm(pcbnew, _try(lambda: nc.GetViaDiameter(), 0)) or 0.6,
                via_drill_mm=_to_mm(pcbnew, _try(lambda: nc.GetViaDrill(), 0)) or 0.3,
            ))
    if classes:
        design.rules.net_classes = classes
        design.rules.__post_init__()

    return design


def load_board_file(path: str, name: str = "") -> Design:
    """Load a ``.kicad_pcb`` from disk through ``pcbnew`` and convert it."""
    pcbnew = _pcbnew()
    board = pcbnew.LoadBoard(path)
    if board is None:
        raise BackendError("pcbnew could not load %s" % path)
    return board_to_design(board, name=name)


def current_board_design(name: str = "") -> Design:
    """Convert the board currently open in Pcbnew. GUI only."""
    pcbnew = _pcbnew()
    board = pcbnew.GetBoard()
    if board is None:
        raise BackendError(
            "no board is open. GetBoard() only works inside the running "
            "Pcbnew GUI; use load_board_file(path) from a standalone script."
        )
    return board_to_design(board, name=name)
