"""Deterministic design review.

Validation asks "is this buildable?"; review asks "is this any good?". These
checks encode the things an experienced engineer looks for first, and they run
without a model. Their real job is to be the *evidence* a language model
narrates over: an LLM asked to review a netlist cold will invent problems, but
an LLM handed "U1 has 2 power pins and 0 decoupling capacitors" writes an
accurate, specific review.

Every check is a pure function of the IR (plus an optional footprint library),
so results are reproducible and diffable between revisions.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Optional, Tuple

from ..ir.types import Component, Design, Net
from ..ir.validate import ERROR, INFO, WARNING, Issue

__all__ = ["review", "review_context", "CHECKS"]

#: Reference prefixes that are active devices worth decoupling.
_IC_PREFIXES = ("U", "IC", "Q", "RN")
_CAP_PREFIXES = ("C",)
_CONNECTOR_PREFIXES = ("J", "P", "CN", "X")
_MECHANICAL_PREFIXES = ("H", "MH", "FID", "LOGO", "TP")


def _by_prefix(design: Design, prefixes: Tuple[str, ...]) -> List[Component]:
    return [c for c in design.components if c.prefix in prefixes]


def _net_of(design: Design, ref: str, pad: str) -> Optional[Net]:
    for n in design.nets:
        if (ref, pad) in n.connections:
            return n
    return None


# -- individual checks ----------------------------------------------------


def check_ground_exists(design: Design, library=None) -> List[Issue]:
    if not design.nets:
        return []
    if any(n.is_ground() for n in design.nets):
        return []
    return [Issue(ERROR, "no_ground",
                  "no ground net found", [],
                  "Name your return net GND (or VSS/AGND/DGND) so tools, "
                  "net classes and zone pours recognise it.")]


def check_decoupling(design: Design, library=None) -> List[Issue]:
    """Every IC power pin wants a local bypass capacitor."""
    issues: List[Issue] = []
    # Note: an empty cap set is not an early exit -- a design with no
    # capacitors at all is precisely the case worth reporting.
    caps = {c.ref for c in _by_prefix(design, _CAP_PREFIXES)}

    for ic in _by_prefix(design, _IC_PREFIXES):
        pads = design.pads_of(ic.ref)
        power_nets = {
            name for name in pads.values()
            if (net := design.net(name)) and net.is_power() and not net.is_ground()
        }
        if not power_nets:
            continue

        for rail in sorted(power_nets):
            net = design.net(rail)
            if net is None:
                continue
            caps_on_rail = {r for r in net.refs if r in caps}
            # a cap only decouples if its other pad returns to ground
            decoupling = set()
            for cap_ref in caps_on_rail:
                cap_nets = {n.name for n in design.nets_of(cap_ref)}
                if any(design.net(nn) and design.net(nn).is_ground()
                       for nn in cap_nets):
                    decoupling.add(cap_ref)

            if not decoupling:
                issues.append(Issue(
                    WARNING, "missing_decoupling",
                    "%s is powered from %s but that rail has no decoupling "
                    "capacitor to ground" % (ic.ref, rail), [ic.ref],
                    "Add a 100nF capacitor from %s to GND, placed as close to "
                    "the %s power pin as the layout allows." % (rail, ic.ref)))
    return issues


def check_decoupling_ratio(design: Design, library=None) -> List[Issue]:
    """Rough count: roughly one bypass cap per IC supply pin."""
    issues: List[Issue] = []
    caps = {c.ref for c in _by_prefix(design, _CAP_PREFIXES)}
    ics = _by_prefix(design, _IC_PREFIXES)
    if not ics or not caps:
        return issues

    supply_pins = 0
    for ic in ics:
        for name in design.pads_of(ic.ref).values():
            net = design.net(name)
            if net and net.is_power() and not net.is_ground():
                supply_pins += 1

    bypass = 0
    for cap_ref in caps:
        nets = [n for n in design.nets_of(cap_ref)]
        if any(n.is_ground() for n in nets) and any(
            n.is_power() and not n.is_ground() for n in nets
        ):
            bypass += 1

    if supply_pins and bypass < supply_pins:
        issues.append(Issue(
            INFO, "low_decoupling_count",
            "%d IC supply pin(s) but only %d bypass capacitor(s)"
            % (supply_pins, bypass), [],
            "The usual rule is one 100nF per supply pin, plus one bulk "
            "capacitor (1-10uF) per rail."))
    return issues


def check_bulk_capacitance(design: Design, library=None) -> List[Issue]:
    """Each supply rail wants at least one bulk capacitor."""
    issues: List[Issue] = []
    rails = [n for n in design.nets if n.is_power() and not n.is_ground()]
    for rail in rails:
        bulk = False
        for ref in rail.refs:
            comp = design.component(ref)
            if comp is None or comp.prefix not in _CAP_PREFIXES:
                continue
            if _cap_value_farads(comp.value) >= 1e-6:
                bulk = True
                break
        if not bulk and len(rail.connections) > 2:
            issues.append(Issue(
                INFO, "no_bulk_cap",
                "rail %s has no bulk capacitor (>= 1uF)" % rail.name, [],
                "Add a 10uF ceramic near the rail's source to hold up the "
                "supply during current transients."))
    return issues


def check_unconnected_pins(design: Design, library=None) -> List[Issue]:
    """Report ICs with a high proportion of unconnected pins."""
    issues: List[Issue] = []
    if library is None:
        return issues
    for ic in _by_prefix(design, _IC_PREFIXES):
        if not library.exists(ic.footprint):
            continue
        fp = library.get(ic.footprint)
        total = len(fp.pad_numbers)
        if total < 4:
            continue
        used = len(design.pads_of(ic.ref))
        if used == 0:
            continue
        unused = total - used
        if unused > 0 and unused >= total * 0.4:
            issues.append(Issue(
                WARNING, "many_unconnected_pins",
                "%s leaves %d of %d pins unconnected" % (ic.ref, unused, total),
                [ic.ref],
                "Unused CMOS inputs should be tied high or low rather than "
                "left floating; confirm against the datasheet."))
    return issues


def check_power_net_class(design: Design, library=None) -> List[Issue]:
    """Power rails carrying current should not sit on Default track widths."""
    issues: List[Issue] = []
    default = design.rules.net_class("Default")
    for n in design.nets:
        if not n.is_power():
            continue
        if n.net_class != "Default":
            continue
        fanout = len(n.connections)
        if fanout >= 4:
            issues.append(Issue(
                INFO, "power_on_default_class",
                "power net %s has %d connections but uses the Default net "
                "class (%g mm tracks)" % (n.name, fanout, default.track_width_mm),
                [],
                "Define a 'Power' net class with wider tracks (0.4-0.8 mm) and "
                "assign %s to it." % n.name))
    return issues


def check_dnp_connected(design: Design, library=None) -> List[Issue]:
    issues = []
    for c in design.components:
        if c.dnp and design.nets_of(c.ref):
            issues.append(Issue(
                INFO, "dnp_connected",
                "%s is marked do-not-populate but is wired into the netlist"
                % c.ref, [c.ref],
                "That is normal for a stuffing option -- confirm the circuit "
                "still works with it absent."))
    return issues


def check_mounting_holes(design: Design, library=None) -> List[Issue]:
    holes = [c for c in design.components
             if c.prefix in ("H", "MH") or "MountingHole" in c.footprint]
    if holes:
        return []
    if design.outline.area_mm2() < 400:  # < 20x20mm, probably a module
        return []
    return [Issue(INFO, "no_mounting_holes",
                  "board has no mounting holes", [],
                  "Boards larger than about 20x20 mm usually need at least "
                  "two M3 mounting holes.")]


def check_test_points(design: Design, library=None) -> List[Issue]:
    tps = [c for c in design.components if c.prefix == "TP"]
    rails = [n for n in design.nets if n.is_power()]
    if not rails or tps:
        return []
    if len(design.components) < 8:
        return []
    return [Issue(INFO, "no_test_points",
                  "no test points on a board with %d power/ground nets"
                  % len(rails), [],
                  "A test point on each supply rail and ground makes bring-up "
                  "and fault-finding far easier.")]


def check_board_density(design: Design, library=None) -> List[Issue]:
    """Compare summed courtyard area against board area."""
    if library is None:
        return []
    board_area = design.outline.area_mm2()
    if board_area <= 0:
        return []
    part_area = 0.0
    for c in design.components:
        if not library.exists(c.footprint):
            continue
        w, h = library.get(c.footprint).size_mm()
        part_area += w * h

    ratio = part_area / board_area
    if ratio > 0.85:
        return [Issue(ERROR, "board_too_small",
                      "components occupy %.0f%% of the board area -- they will "
                      "not fit" % (ratio * 100), [],
                      "Enlarge the outline to at least %.0f x %.0f mm, or move "
                      "parts to the bottom side."
                      % ((part_area / 0.45) ** 0.5, (part_area / 0.45) ** 0.5))]
    if ratio > 0.6:
        return [Issue(WARNING, "board_dense",
                      "components occupy %.0f%% of the board area, leaving "
                      "little room to route" % (ratio * 100), [],
                      "Aim for under 50%% on a 2-layer board.")]
    if ratio < 0.05 and len(design.components) > 3:
        return [Issue(INFO, "board_sparse",
                      "components occupy only %.1f%% of the board area"
                      % (ratio * 100), [],
                      "The board could likely be smaller, which is cheaper.")]
    return []


def check_layer_count(design: Design, library=None) -> List[Issue]:
    """Flag designs whose pin count outgrows a 2-layer stackup."""
    if design.rules.copper_layers > 2 or library is None:
        return []
    pins = 0
    fine_pitch = False
    for c in design.components:
        if not library.exists(c.footprint):
            continue
        fp = library.get(c.footprint)
        pins += len(fp.pad_numbers)
        if len(fp.pad_numbers) >= 32:
            fine_pitch = True
    if pins > 250 or fine_pitch:
        return [Issue(WARNING, "consider_more_layers",
                      "%d pads%s on a 2-layer board"
                      % (pins, " including a >=32-pin package" if fine_pitch else ""),
                      [],
                      "Routing this on 2 layers will be difficult. A 4-layer "
                      "stackup with dedicated ground and power planes is "
                      "usually cheaper than the time spent fighting it.")]
    return []


def check_net_naming(design: Design, library=None) -> List[Issue]:
    issues = []
    generic = re.compile(r"^(NET|N)\$?\d+$|^UNNAMED", re.I)
    bad = [n.name for n in design.nets if generic.match(n.name)]
    if bad:
        issues.append(Issue(
            INFO, "generic_net_names",
            "%d net(s) have auto-generated names: %s"
            % (len(bad), ", ".join(bad[:6])), [],
            "Meaningful net names make DRC reports and reviews readable."))
    return issues


def check_single_side_assembly(design: Design, library=None) -> List[Issue]:
    sides = {c.side for c in design.components}
    if len(sides) > 1:
        bottom = [c.ref for c in design.components if c.side == "bottom"]
        return [Issue(INFO, "double_sided_assembly",
                      "%d component(s) on the bottom side: %s"
                      % (len(bottom), ", ".join(bottom[:8])), bottom[:8],
                      "Double-sided assembly roughly doubles the setup cost. "
                      "Keep everything on top if it fits.")]
    return []


def check_connector_pin1(design: Design, library=None) -> List[Issue]:
    conns = _by_prefix(design, _CONNECTOR_PREFIXES)
    if not conns:
        return []
    grounded = 0
    for c in conns:
        nets = design.nets_of(c.ref)
        if any(n.is_ground() for n in nets):
            grounded += 1
    if grounded < len(conns):
        missing = [c.ref for c in conns
                   if not any(n.is_ground() for n in design.nets_of(c.ref))]
        return [Issue(WARNING, "connector_no_ground",
                      "connector(s) with no ground pin: %s" % ", ".join(missing),
                      missing,
                      "Almost every connector needs a ground return; without "
                      "one, signals have no reference.")]
    return []


CHECKS: List[Callable] = [
    check_ground_exists,
    check_decoupling,
    check_decoupling_ratio,
    check_bulk_capacitance,
    check_unconnected_pins,
    check_power_net_class,
    check_dnp_connected,
    check_mounting_holes,
    check_test_points,
    check_board_density,
    check_layer_count,
    check_net_naming,
    check_single_side_assembly,
    check_connector_pin1,
]


def review(design: Design, library=None) -> List[Issue]:
    """Run every check. Returns issues sorted most-severe first."""
    issues: List[Issue] = []
    for check in CHECKS:
        try:
            issues.extend(check(design, library))
        except Exception as exc:  # a broken check must not sink the review
            issues.append(Issue(INFO, "check_failed",
                                "%s raised %s" % (check.__name__, exc), []))
    order = {ERROR: 0, WARNING: 1, INFO: 2}
    issues.sort(key=lambda i: (order.get(i.severity, 3), i.code))
    return issues


def _cap_value_farads(value: str) -> float:
    """Parse ``'100n'``, ``'4u7'``, ``'10uF'``, ``'0.1uF'`` into farads.

    Returns 0.0 when the value is not a recognisable capacitance.
    """
    v = value.strip().replace("F", "").replace("f", "").strip()
    mult = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3}
    # "4u7" style
    m = re.match(r"^(\d+)([pnuµm])(\d+)$", v)
    if m:
        return float("%s.%s" % (m.group(1), m.group(3))) * mult[m.group(2)]
    m = re.match(r"^([\d.]+)\s*([pnuµm])?$", v)
    if m:
        try:
            base = float(m.group(1))
        except ValueError:
            return 0.0
        return base * mult.get(m.group(2) or "", 1.0)
    return 0.0


def review_context(design: Design, library=None) -> Dict[str, object]:
    """Structured facts for a model to narrate over.

    This is deliberately dense and free of opinion -- the numbers a reviewer
    would gather before forming a view. Feed it to an LLM alongside the
    deterministic findings.
    """
    rails = [n for n in design.nets if n.is_power() and not n.is_ground()]
    grounds = [n for n in design.nets if n.is_ground()]
    signals = [n for n in design.nets if not n.is_power()]

    ics = []
    for ic in _by_prefix(design, _IC_PREFIXES):
        pads = design.pads_of(ic.ref)
        pad_total = None
        if library is not None and library.exists(ic.footprint):
            pad_total = len(library.get(ic.footprint).pad_numbers)
        ics.append({
            "ref": ic.ref,
            "value": ic.value,
            "footprint": ic.footprint,
            "connected_pins": len(pads),
            "total_pins": pad_total,
            "power_nets": sorted({
                nm for nm in pads.values()
                if design.net(nm) and design.net(nm).is_power()
            }),
        })

    fanout = sorted(
        ((len(n.connections), n.name) for n in design.nets), reverse=True
    )[:10]

    return {
        "name": design.name,
        "description": design.description,
        "component_count": len(design.components),
        "net_count": len(design.nets),
        "components_by_type": _count_by_prefix(design),
        "copper_layers": design.rules.copper_layers,
        "board_mm": [design.outline.width_mm, design.outline.height_mm]
        if design.outline.shape == "rect" else None,
        "board_area_mm2": round(design.outline.area_mm2(), 2),
        "power_rails": [
            {"name": n.name, "connections": len(n.connections),
             "net_class": n.net_class} for n in rails
        ],
        "ground_nets": [n.name for n in grounds],
        "signal_net_count": len(signals),
        "highest_fanout_nets": [{"net": nm, "connections": c} for c, nm in fanout],
        "integrated_circuits": ics,
        "net_classes": [
            {"name": nc.name, "track_mm": nc.track_width_mm,
             "clearance_mm": nc.clearance_mm}
            for nc in design.rules.net_classes
        ],
        "min_track_mm": design.rules.min_track_mm,
        "min_clearance_mm": design.rules.min_clearance_mm,
        "placed": len(design.placements),
        "notes": design.notes,
    }


def _count_by_prefix(design: Design) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for c in design.components:
        out[c.prefix] = out.get(c.prefix, 0) + 1
    return dict(sorted(out.items()))
