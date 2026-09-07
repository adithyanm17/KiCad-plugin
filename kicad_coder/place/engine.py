"""Component placement.

Placement is a geometric optimisation problem, which is exactly the kind of
thing language models are bad at. So the model never places anything: it
supplies *intent* (``group``, ``near``, ``side`` on each component) and this
module turns that into coordinates.

Two strategies:

``grid``
    Deterministic row-major packing, sorted by size. Ugly but always valid --
    useful as a fallback and for tests.

``force``
    Force-directed relaxation: nets pull connected parts together, courtyards
    push overlapping parts apart, the board edge confines everything. Then a
    hard overlap-resolution pass guarantees the result is legal.

Both are seeded and deterministic: the same design always places identically,
which matters when you are diffing revisions.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

from ..ir.types import Design, Placement

__all__ = ["place", "PlacementResult"]

_CONNECTOR_PREFIXES = ("J", "P", "CN", "X", "SW", "USB")
_MOUNTING_PREFIXES = ("H", "MH")


@dataclass
class _Part:
    """A component as the solver sees it: an axis-aligned courtyard box.

    ``off_x``/``off_y`` carry the courtyard centre relative to the footprint
    origin. Plenty of real footprints are not symmetric about their origin --
    a 2-pin header's courtyard sits 1.28 mm off centre, so assuming otherwise
    silently under-protects one side and KiCad reports overlapping courtyards
    on a layout the solver believed was clean.
    """

    ref: str
    w: float
    h: float
    x: float = 0.0
    y: float = 0.0
    rot: float = 0.0
    side: str = "top"
    locked: bool = False
    edge_seeking: bool = False
    off_x: float = 0.0
    off_y: float = 0.0
    #: extra room this part needs against the board edge, for silkscreen
    silk: float = 0.0

    @property
    def half_w(self) -> float:
        return self.w / 2.0

    @property
    def half_h(self) -> float:
        return self.h / 2.0

    @property
    def cx(self) -> float:
        """Courtyard centre X in board coordinates."""
        return self.x + self.off_x

    @property
    def cy(self) -> float:
        return self.y + self.off_y


@dataclass
class PlacementResult:
    placements: Dict[str, Placement]
    overlaps: List[Tuple[str, str]]
    outside: List[str]
    iterations: int
    strategy: str

    @property
    def ok(self) -> bool:
        return not self.overlaps and not self.outside

    def summary(self) -> str:
        lines = [
            "Placed %d component(s) using the %s strategy (%d iterations)"
            % (len(self.placements), self.strategy, self.iterations)
        ]
        if self.overlaps:
            lines.append(
                "  %d unresolved overlap(s): %s"
                % (len(self.overlaps),
                   ", ".join("%s/%s" % p for p in self.overlaps[:6]))
            )
        if self.outside:
            lines.append(
                "  %d component(s) could not fit inside the outline: %s"
                % (len(self.outside), ", ".join(self.outside[:6]))
            )
        if self.ok:
            lines.append("  no overlaps, everything inside the board outline")
        return "\n".join(lines)


def _part_boxes(design: Design, library
                ) -> Dict[str, Tuple[float, float, float, float, float]]:
    """``ref -> (width, height, offset_x, offset_y, silk_overhang)`` in mm."""
    boxes: Dict[str, Tuple[float, float, float, float, float]] = {}
    for c in design.components:
        w = h = 3.0  # sane default when the footprint cannot be resolved
        ox = oy = silk = 0.0
        if library is not None and library.exists(c.footprint):
            fp = library.get(c.footprint)
            x0, y0, x1, y1 = fp.courtyard_bbox()
            if x1 > x0 and y1 > y0:
                w, h = x1 - x0, y1 - y0
                ox, oy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            silk = fp.silk_overhang_mm()
        boxes[c.ref] = (w, h, ox, oy, silk)
    return boxes


def _build_parts(design: Design, library, clearance: float) -> List[_Part]:
    boxes = _part_boxes(design, library)
    parts: List[_Part] = []
    for c in design.components:
        w, h, ox, oy, silk = boxes[c.ref]
        existing = design.placements.get(c.ref)
        parts.append(_Part(
            ref=c.ref,
            w=w + clearance,
            h=h + clearance,
            off_x=ox,
            off_y=oy,
            silk=silk,
            x=existing.x_mm if existing else 0.0,
            y=existing.y_mm if existing else 0.0,
            rot=existing.rotation_deg if existing else 0.0,
            side=existing.side if existing else c.side,
            locked=bool(existing and existing.locked),
            edge_seeking=c.prefix in _CONNECTOR_PREFIXES,
        ))
    return parts


def _grid_seed(parts: List[_Part], x0: float, y0: float,
               x1: float, y1: float) -> None:
    """Row-major packing, tallest first. Always produces a legal-ish start."""
    free = [p for p in parts if not p.locked]
    free.sort(key=lambda p: (-p.h, -p.w, p.ref))
    cx, cy, row_h = x0, y0, 0.0
    for p in free:
        if cx + p.w > x1 and cx > x0:
            cx = x0
            cy += row_h
            row_h = 0.0
        p.x = cx + p.half_w - p.off_x
        p.y = cy + p.half_h - p.off_y
        cx += p.w
        row_h = max(row_h, p.h)


def _net_springs(design: Design, max_fanout: int = 12
                 ) -> List[Tuple[str, str, float]]:
    """Pairwise attraction terms derived from the netlist.

    High-fanout nets (ground, a wide supply) connect nearly everything, so
    letting them pull with full strength collapses the layout into a blob.
    Weight falls off as 1/(n-1), and very high fanout nets are skipped
    entirely -- in a real board those are handled by a plane, not by routing.
    """
    springs: List[Tuple[str, str, float]] = []
    for net in design.nets:
        refs = sorted(set(net.refs))
        n = len(refs)
        if n < 2:
            continue
        if n > max_fanout:
            continue
        weight = 1.0 / (n - 1)
        if net.is_ground():
            weight *= 0.25
        elif net.is_power():
            weight *= 0.5
        for i in range(n):
            for j in range(i + 1, n):
                springs.append((refs[i], refs[j], weight))

    # explicit "near" hints outrank anything inferred from the netlist
    for c in design.components:
        for other in c.near:
            springs.append((c.ref, other, 4.0))
    return springs


def _resolve_overlaps(parts: List[_Part], bounds: Tuple[float, float, float, float],
                      passes: int = 60) -> List[Tuple[str, str]]:
    """Push overlapping AABBs apart along their axis of least penetration."""
    x0, y0, x1, y1 = bounds
    movable = [p for p in parts if not p.locked]
    for _ in range(passes):
        moved = False
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                a, b = parts[i], parts[j]
                if a.side != b.side:
                    continue
                dx = b.cx - a.cx
                dy = b.cy - a.cy
                overlap_x = (a.half_w + b.half_w) - abs(dx)
                overlap_y = (a.half_h + b.half_h) - abs(dy)
                if overlap_x <= 0 or overlap_y <= 0:
                    continue
                moved = True
                if overlap_x < overlap_y:
                    shift = overlap_x / 2.0 + 1e-4
                    sign = 1.0 if dx >= 0 else -1.0
                    if not a.locked:
                        a.x -= sign * shift
                    if not b.locked:
                        b.x += sign * shift
                else:
                    shift = overlap_y / 2.0 + 1e-4
                    sign = 1.0 if dy >= 0 else -1.0
                    if not a.locked:
                        a.y -= sign * shift
                    if not b.locked:
                        b.y += sign * shift
        # Clamp to the courtyard box only, never the silk-inflated one. The
        # force stage already settles parts inside the silk-safe region; if
        # honouring silk here would push two parts back into each other,
        # separation wins -- overlapping courtyards are a DRC error while
        # clipped silkscreen is only a warning.
        for p in movable:
            p.x = min(max(p.x, x0 + p.half_w - p.off_x), x1 - p.half_w - p.off_x)
            p.y = min(max(p.y, y0 + p.half_h - p.off_y), y1 - p.half_h - p.off_y)
        if not moved:
            break

    remaining: List[Tuple[str, str]] = []
    for i in range(len(parts)):
        for j in range(i + 1, len(parts)):
            a, b = parts[i], parts[j]
            if a.side != b.side:
                continue
            # Compare courtyard centres, not footprint origins -- for an
            # off-centre courtyard those differ, and using the origin reports
            # overlaps that are not there.
            if (a.half_w + b.half_w) - abs(b.cx - a.cx) > 1e-3 and \
               (a.half_h + b.half_h) - abs(b.cy - a.cy) > 1e-3:
                remaining.append((a.ref, b.ref))
    return remaining


def _force_relax(parts: List[_Part], springs: Sequence[Tuple[str, str, float]],
                 bounds: Tuple[float, float, float, float],
                 iterations: int, rng: random.Random) -> None:
    x0, y0, x1, y1 = bounds
    index = {p.ref: p for p in parts}
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

    for step in range(iterations):
        # cooling schedule: big moves early, fine adjustment late
        temp = 1.0 - (step / float(max(iterations - 1, 1)))
        alpha = 0.10 * (0.25 + 0.75 * temp)

        fx: Dict[str, float] = {p.ref: 0.0 for p in parts}
        fy: Dict[str, float] = {p.ref: 0.0 for p in parts}

        # attraction along nets
        for a_ref, b_ref, w in springs:
            a = index.get(a_ref)
            b = index.get(b_ref)
            if a is None or b is None or a is b:
                continue
            dx = b.cx - a.cx
            dy = b.cy - a.cy
            dist = math.hypot(dx, dy)
            rest = (a.half_w + b.half_w + a.half_h + b.half_h) / 2.0
            if dist < 1e-6:
                dx, dy, dist = rng.uniform(-1, 1), rng.uniform(-1, 1), 1.0
            stretch = dist - rest
            if stretch <= 0:
                continue
            f = w * stretch
            ux, uy = dx / dist, dy / dist
            fx[a_ref] += f * ux
            fy[a_ref] += f * uy
            fx[b_ref] -= f * ux
            fy[b_ref] -= f * uy

        # repulsion between anything whose courtyards are close
        for i in range(len(parts)):
            for j in range(i + 1, len(parts)):
                a, b = parts[i], parts[j]
                if a.side != b.side:
                    continue
                dx = b.cx - a.cx
                dy = b.cy - a.cy
                gap_x = abs(dx) - (a.half_w + b.half_w)
                gap_y = abs(dy) - (a.half_h + b.half_h)
                if gap_x > 2.0 or gap_y > 2.0:
                    continue
                dist = math.hypot(dx, dy)
                if dist < 1e-6:
                    dx, dy, dist = rng.uniform(-1, 1), rng.uniform(-1, 1), 1.0
                push = max(0.0, 2.0 - max(gap_x, gap_y)) * 1.5
                ux, uy = dx / dist, dy / dist
                fx[a.ref] -= push * ux
                fy[a.ref] -= push * uy
                fx[b.ref] += push * ux
                fy[b.ref] += push * uy

        # connectors drift toward the nearest board edge
        for p in parts:
            if not p.edge_seeking:
                continue
            d_left, d_right = p.cx - x0, x1 - p.cx
            d_top, d_bottom = p.cy - y0, y1 - p.cy
            nearest = min(d_left, d_right, d_top, d_bottom)
            strength = 1.2
            if nearest == d_left:
                fx[p.ref] -= strength
            elif nearest == d_right:
                fx[p.ref] += strength
            elif nearest == d_top:
                fy[p.ref] -= strength
            else:
                fy[p.ref] += strength

        # weak pull to centre keeps the cluster from drifting into a corner
        for p in parts:
            fx[p.ref] += (cx - p.cx) * 0.01
            fy[p.ref] += (cy - p.cy) * 0.01

        max_step = 3.0
        for p in parts:
            if p.locked:
                continue
            dx = max(-max_step, min(max_step, fx[p.ref] * alpha))
            dy = max(-max_step, min(max_step, fy[p.ref] * alpha))
            mw, mh = p.half_w + p.silk, p.half_h + p.silk
            p.x = min(max(p.x + dx, x0 + mw - p.off_x), x1 - mw - p.off_x)
            p.y = min(max(p.y + dy, y0 + mh - p.off_y), y1 - mh - p.off_y)


def place(
    design: Design,
    library=None,
    strategy: str = "force",
    iterations: int = 400,
    seed: int = 0,
    clearance_mm: float = 0.5,
    keep_locked: bool = True,
    grid_mm: float = 0.05,
) -> PlacementResult:
    """Place every component and write the result into ``design.placements``.

    Existing placements marked ``locked`` are preserved when ``keep_locked``.
    """
    if strategy not in ("force", "grid"):
        raise ValueError("strategy must be 'force' or 'grid'")
    if not design.components:
        return PlacementResult({}, [], [], 0, strategy)

    if not keep_locked:
        design.placements = {}

    rng = random.Random(seed)
    parts = _build_parts(design, library, clearance_mm)

    # Silkscreen (reference designators, outlines) extends past the copper
    # courtyard, so keeping only the copper-to-edge clearance gets the text
    # clipped by the board outline. Hold parts a full millimetre back.
    edge = max(design.rules.edge_clearance_mm, 1.0)
    bx0, by0, bx1, by1 = design.outline.bounds()
    bounds = (bx0 + edge, by0 + edge, bx1 - edge, by1 - edge)

    # If the board is simply too small, report it rather than looping forever.
    outside: List[str] = []
    for p in parts:
        if p.w > (bounds[2] - bounds[0]) or p.h > (bounds[3] - bounds[1]):
            outside.append(p.ref)

    _grid_seed(parts, *bounds)

    used_iterations = 0
    if strategy == "force":
        # jitter breaks the perfect symmetry of the grid seed
        for p in parts:
            if not p.locked:
                p.x += rng.uniform(-0.5, 0.5)
                p.y += rng.uniform(-0.5, 0.5)
        springs = _net_springs(design)
        _force_relax(parts, springs, bounds, iterations, rng)
        used_iterations = iterations

    overlaps = _resolve_overlaps(parts, bounds)

    def snap(v: float) -> float:
        return round(round(v / grid_mm) * grid_mm, 4) if grid_mm > 0 else round(v, 4)

    placements: Dict[str, Placement] = {}
    for p in parts:
        placements[p.ref] = Placement(
            ref=p.ref,
            x_mm=snap(p.x),
            y_mm=snap(p.y),
            rotation_deg=p.rot,
            side=p.side,
            locked=p.locked,
        )
    design.placements = placements

    return PlacementResult(
        placements=placements,
        overlaps=overlaps,
        outside=outside,
        iterations=used_iterations,
        strategy=strategy,
    )
