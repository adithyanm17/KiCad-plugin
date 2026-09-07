"""Structural validation of a :class:`~kicad_coder.ir.types.Design`.

Validation answers one question: *can this design be turned into a board at
all?* Judgement calls about whether it is a **good** design live in
:mod:`kicad_coder.review`.

Every problem is reported as an :class:`Issue` rather than raised, so a model
driving this library gets the full list in one round trip instead of fixing
one error at a time.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from .types import Design

__all__ = ["Issue", "ValidationResult", "validate"]

ERROR = "error"
WARNING = "warning"
INFO = "info"


@dataclass
class Issue:
    """One validation or review finding."""

    severity: str  # error | warning | info
    code: str
    message: str
    refs: List[str]
    hint: str = ""

    def to_dict(self) -> Dict[str, object]:
        return asdict(self)

    def __str__(self) -> str:
        where = " [%s]" % ", ".join(self.refs) if self.refs else ""
        out = "%-7s %s: %s%s" % (self.severity.upper(), self.code, self.message, where)
        if self.hint:
            out += "\n          hint: %s" % self.hint
        return out


@dataclass
class ValidationResult:
    issues: List[Issue]

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def ok(self) -> bool:
        """True when nothing blocks board generation."""
        return not self.errors

    def to_dict(self) -> Dict[str, object]:
        return {
            "ok": self.ok,
            "error_count": len(self.errors),
            "warning_count": len(self.warnings),
            "issues": [i.to_dict() for i in self.issues],
        }

    def __str__(self) -> str:
        if not self.issues:
            return "Design is valid: no issues."
        head = "%d error(s), %d warning(s)" % (len(self.errors), len(self.warnings))
        return head + "\n" + "\n".join(str(i) for i in self.issues)


def validate(design: Design, library=None, strict: bool = False) -> ValidationResult:
    """Check ``design`` for structural problems.

    Pass a :class:`~kicad_coder.library.footprints.FootprintLibrary` as
    ``library`` to additionally verify that every footprint resolves and every
    referenced pad actually exists on it -- by far the most common class of
    mistake a model makes.
    """
    issues: List[Issue] = []
    add = issues.append

    # -- components -------------------------------------------------------

    seen: Dict[str, int] = {}
    for c in design.components:
        seen[c.ref] = seen.get(c.ref, 0) + 1
    for ref, count in sorted(seen.items()):
        if count > 1:
            add(Issue(ERROR, "duplicate_ref",
                      "reference designator %s used %d times" % (ref, count),
                      [ref],
                      "Every component needs a unique reference."))

    if not design.components:
        add(Issue(ERROR, "no_components", "design has no components", [],
                  "Add components with add_component before generating a board."))

    for c in design.components:
        if not c.value.strip():
            add(Issue(WARNING, "empty_value",
                      "%s has no value" % c.ref, [c.ref],
                      "Value appears on the BOM and the silkscreen."))
        if strict and not c.mpn.strip() and not c.exclude_from_bom:
            add(Issue(WARNING, "missing_mpn",
                      "%s has no manufacturer part number" % c.ref, [c.ref],
                      "An MPN makes the BOM orderable."))

    # -- nets -------------------------------------------------------------

    refs = {c.ref for c in design.components}
    net_names: Dict[str, int] = {}
    for n in design.nets:
        net_names[n.name] = net_names.get(n.name, 0) + 1
    for name, count in sorted(net_names.items()):
        if count > 1:
            add(Issue(ERROR, "duplicate_net",
                      "net %s declared %d times" % (name, count), [],
                      "Merge the connections into a single net."))

    pin_use: Dict[str, List[str]] = {}
    for n in design.nets:
        if not n.connections:
            add(Issue(WARNING, "empty_net", "net %s has no connections" % n.name,
                      [], "Remove it or connect it."))
        if len(n.connections) == 1:
            ref, pad = n.connections[0]
            add(Issue(WARNING, "single_pin_net",
                      "net %s connects only %s.%s" % (n.name, ref, pad), [ref],
                      "A net with one pin is electrically meaningless -- it is "
                      "either an unfinished connection or should be deleted."))

        for ref, pad in n.connections:
            if ref not in refs:
                add(Issue(ERROR, "unknown_ref",
                          "net %s references unknown component %s" % (n.name, ref),
                          [ref],
                          "Add the component first, or fix the reference."))
            key = "%s.%s" % (ref, pad)
            pin_use.setdefault(key, []).append(n.name)

        if n.net_class != "Default":
            known = {nc.name for nc in design.rules.net_classes}
            if n.net_class not in known:
                add(Issue(ERROR, "unknown_net_class",
                          "net %s uses undefined net class %r"
                          % (n.name, n.net_class), [],
                          "Define it in design rules, or use 'Default'."))

    for pin, nets in sorted(pin_use.items()):
        if len(nets) > 1:
            ref = pin.split(".")[0]
            add(Issue(ERROR, "pin_short",
                      "%s is connected to %d nets: %s"
                      % (pin, len(nets), ", ".join(nets)), [ref],
                      "One pad can belong to exactly one net. This is a short."))

    connected = set()
    for n in design.nets:
        connected.update(n.refs)
    for c in design.components:
        if c.ref not in connected and not c.ref.startswith(("H", "FID")):
            add(Issue(WARNING, "unconnected_component",
                      "%s has no connections" % c.ref, [c.ref],
                      "Mechanical parts are fine; anything else is probably "
                      "an omission."))

    # -- rules and geometry ----------------------------------------------

    r = design.rules
    if r.min_track_mm <= 0 or r.min_clearance_mm <= 0:
        add(Issue(ERROR, "bad_rules",
                  "min_track_mm and min_clearance_mm must be positive", []))
    if r.min_via_drill_mm >= r.min_via_diameter_mm:
        add(Issue(ERROR, "bad_via",
                  "via drill (%g mm) must be smaller than via diameter (%g mm)"
                  % (r.min_via_drill_mm, r.min_via_diameter_mm), []))
    else:
        ring = (r.min_via_diameter_mm - r.min_via_drill_mm) / 2.0
        if ring < r.min_annular_ring_mm:
            add(Issue(WARNING, "thin_annular_ring",
                      "via annular ring is %.3f mm, below the %.3f mm minimum"
                      % (ring, r.min_annular_ring_mm), [],
                      "Increase via diameter or reduce drill."))

    for nc in r.net_classes:
        if nc.track_width_mm < r.min_track_mm:
            add(Issue(ERROR, "track_below_min",
                      "net class %s uses %g mm tracks, below the %g mm minimum"
                      % (nc.name, nc.track_width_mm, r.min_track_mm), []))
        if nc.clearance_mm < r.min_clearance_mm:
            add(Issue(ERROR, "clearance_below_min",
                      "net class %s uses %g mm clearance, below the %g mm minimum"
                      % (nc.name, nc.clearance_mm, r.min_clearance_mm), []))

    o = design.outline
    if o.shape == "rect" and (o.width_mm <= 0 or o.height_mm <= 0):
        add(Issue(ERROR, "bad_outline", "board outline has non-positive size", []))
    if o.area_mm2() <= 0:
        add(Issue(ERROR, "bad_outline", "board outline encloses no area", []))

    # -- footprints (needs a library) -------------------------------------

    if library is not None:
        for c in design.components:
            if not library.exists(c.footprint):
                near = library.search(c.footprint.split(":")[-1], limit=3)
                add(Issue(ERROR, "unknown_footprint",
                          "%s uses footprint %r which is not in any library"
                          % (c.ref, c.footprint), [c.ref],
                          ("Closest matches: %s" % ", ".join(near)) if near
                          else "Use search_footprints to find a valid id."))
                continue

            fp = library.get(c.footprint)
            valid_pads = set(fp.pad_numbers)
            used = design.pads_of(c.ref)
            for pad in sorted(used):
                if pad not in valid_pads:
                    add(Issue(ERROR, "unknown_pad",
                              "%s (%s) has no pad %r"
                              % (c.ref, c.footprint, pad), [c.ref],
                              "Available pads: %s"
                              % ", ".join(sorted(valid_pads, key=_pad_sort))))
            if strict:
                unused = valid_pads - set(used)
                if unused and len(valid_pads) > 2:
                    add(Issue(INFO, "unused_pads",
                              "%s leaves %d of %d pads unconnected: %s"
                              % (c.ref, len(unused), len(valid_pads),
                                 ", ".join(sorted(unused, key=_pad_sort)[:8])),
                              [c.ref]))

    # -- placement --------------------------------------------------------

    for ref, p in design.placements.items():
        if ref not in refs:
            add(Issue(WARNING, "orphan_placement",
                      "placement for unknown component %s" % ref, [ref],
                      "It will be ignored."))
        x0, y0, x1, y1 = o.bounds()
        if not (x0 <= p.x_mm <= x1 and y0 <= p.y_mm <= y1):
            add(Issue(WARNING, "placement_outside_board",
                      "%s is placed at (%.2f, %.2f), outside the board outline"
                      % (ref, p.x_mm, p.y_mm), [ref]))

    return ValidationResult(issues=issues)


def _pad_sort(pad: str):
    m = re.match(r"^(\d+)$", pad)
    return (0, int(m.group(1)), "") if m else (1, 0, pad)
