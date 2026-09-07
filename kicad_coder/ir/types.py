"""The intermediate representation an LLM writes.

The central design rule of this library: **a model never emits geometry.** It
emits this IR -- components, nets, rules, intent -- and deterministic code
turns that into a board. Everything here is a plain dataclass with no
third-party dependencies so it imports cleanly inside KiCad's bundled Python.

Units are millimetres and degrees throughout the IR. Conversion to KiCad's
integer nanometres happens only at the backend boundary.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Tuple

__all__ = [
    "Component",
    "Net",
    "NetClass",
    "DesignRules",
    "BoardOutline",
    "Placement",
    "Design",
]

_REF_RE = re.compile(r"^[A-Za-z_]+\d+$")
_NET_RE = re.compile(r"^[^\s()]+$")


def _norm_side(side: str) -> str:
    s = (side or "top").strip().lower()
    if s in ("top", "f", "front", "f.cu"):
        return "top"
    if s in ("bottom", "b", "back", "b.cu"):
        return "bottom"
    raise ValueError("side must be 'top' or 'bottom', got %r" % (side,))


@dataclass
class Component:
    """One placeable part.

    ``footprint`` is a KiCad library id -- ``"Library:FootprintName"``, e.g.
    ``"Resistor_SMD:R_0603_1608Metric"``. ``fields`` carries any extra BOM
    columns (tolerance, voltage rating, supplier part number...).
    """

    ref: str
    value: str
    footprint: str
    mpn: str = ""
    manufacturer: str = ""
    datasheet: str = ""
    description: str = ""
    dnp: bool = False
    exclude_from_bom: bool = False
    exclude_from_pos: bool = False
    side: str = "top"
    #: free-form grouping hint used by the placer ("power", "mcu", "connectors")
    group: str = ""
    #: refs this part wants to sit near; the placer treats these as strong springs
    near: List[str] = field(default_factory=list)
    fields: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.ref = self.ref.strip().upper()
        self.side = _norm_side(self.side)
        self.near = [str(r).strip().upper() for r in self.near]
        if not _REF_RE.match(self.ref):
            raise ValueError(
                "invalid reference designator %r: expected letters followed by "
                "digits, e.g. 'R1', 'U3', 'TP12'" % (self.ref,)
            )
        if ":" not in self.footprint:
            raise ValueError(
                "footprint for %s must be 'Library:Name', got %r"
                % (self.ref, self.footprint)
            )

    @property
    def prefix(self) -> str:
        """The letter part of the reference, e.g. ``'R'`` for ``'R10'``."""
        return re.sub(r"\d+$", "", self.ref)

    def bom_fields(self) -> Dict[str, str]:
        """Everything that should appear on a BOM line for this part."""
        out = {
            "Reference": self.ref,
            "Value": self.value,
            "Footprint": self.footprint,
            "MPN": self.mpn,
            "Manufacturer": self.manufacturer,
            "Datasheet": self.datasheet,
            "Description": self.description,
            "DNP": "yes" if self.dnp else "",
        }
        out.update(self.fields)
        return out


@dataclass
class Net:
    """A named electrical node.

    ``connections`` is a list of ``(ref, pad)`` pairs. ``pad`` is the pad
    *number* as it appears in the footprint -- a string, because plenty of
    footprints use ``"A1"`` or ``"MP"``.
    """

    name: str
    connections: List[Tuple[str, str]] = field(default_factory=list)
    net_class: str = "Default"

    def __post_init__(self) -> None:
        self.name = self.name.strip()
        if not self.name or not _NET_RE.match(self.name):
            raise ValueError(
                "invalid net name %r: must be non-empty and contain no "
                "whitespace or parentheses" % (self.name,)
            )
        self.connections = [
            (str(r).strip().upper(), str(p).strip()) for r, p in self.connections
        ]

    def add(self, ref: str, pad: str) -> None:
        pair = (str(ref).strip().upper(), str(pad).strip())
        if pair not in self.connections:
            self.connections.append(pair)

    @property
    def refs(self) -> List[str]:
        return [r for r, _ in self.connections]

    def is_ground(self) -> bool:
        n = self.name.upper().lstrip("+-")
        return bool(re.match(r"^(GND|AGND|DGND|PGND|VSS|EARTH)\b|^(GND|VSS)$", n))

    def is_power(self) -> bool:
        """Heuristic: does this net look like a supply rail or ground?"""
        if self.is_ground():
            return True
        n = self.name.upper().lstrip("+-")
        return bool(
            re.match(r"^(VCC|VDD|VBUS|VIN|VOUT|VBAT|VVDD|AVDD|AVCC|VREF)", n)
            or re.match(r"^\d+V\d*$", n)
            or re.match(r"^\d+V\d*_", n)
        )


@dataclass
class NetClass:
    """Track/clearance geometry applied to a group of nets."""

    name: str = "Default"
    clearance_mm: float = 0.2
    track_width_mm: float = 0.25
    via_diameter_mm: float = 0.6
    via_drill_mm: float = 0.3
    diff_pair_width_mm: float = 0.2
    diff_pair_gap_mm: float = 0.25
    description: str = ""


@dataclass
class DesignRules:
    """Board-wide fabrication constraints.

    Defaults describe a conservative 2-layer 1oz process that essentially every
    prototype fab house can build without upcharge.
    """

    copper_layers: int = 2
    board_thickness_mm: float = 1.6
    copper_weight_oz: float = 1.0
    min_track_mm: float = 0.2
    min_clearance_mm: float = 0.2
    min_via_diameter_mm: float = 0.6
    min_via_drill_mm: float = 0.3
    min_hole_to_hole_mm: float = 0.25
    min_annular_ring_mm: float = 0.13
    edge_clearance_mm: float = 0.3
    allow_blind_buried_vias: bool = False
    allow_microvias: bool = False
    net_classes: List[NetClass] = field(default_factory=lambda: [NetClass()])

    def __post_init__(self) -> None:
        if self.copper_layers < 1 or self.copper_layers > 32:
            raise ValueError("copper_layers must be between 1 and 32")
        if self.copper_layers != 1 and self.copper_layers % 2:
            raise ValueError("copper_layers must be 1 or an even number")
        if not any(nc.name == "Default" for nc in self.net_classes):
            self.net_classes.insert(0, NetClass())

    def net_class(self, name: str) -> NetClass:
        for nc in self.net_classes:
            if nc.name == name:
                return nc
        return self.net_classes[0]


@dataclass
class BoardOutline:
    """The board edge, drawn on ``Edge.Cuts``.

    Either a rectangle (``width_mm``/``height_mm``) or an explicit polygon. The
    rectangle's origin is its top-left corner at ``(origin_x_mm, origin_y_mm)``.
    """

    shape: str = "rect"  # "rect" | "polygon"
    width_mm: float = 50.0
    height_mm: float = 50.0
    corner_radius_mm: float = 0.0
    origin_x_mm: float = 0.0
    origin_y_mm: float = 0.0
    points: List[Tuple[float, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.shape not in ("rect", "polygon"):
            raise ValueError("outline shape must be 'rect' or 'polygon'")
        self.points = [(float(x), float(y)) for x, y in self.points]
        if self.shape == "polygon" and len(self.points) < 3:
            raise ValueError("polygon outline needs at least 3 points")

    def bounds(self) -> Tuple[float, float, float, float]:
        """Return ``(min_x, min_y, max_x, max_y)`` in mm."""
        if self.shape == "rect":
            return (
                self.origin_x_mm,
                self.origin_y_mm,
                self.origin_x_mm + self.width_mm,
                self.origin_y_mm + self.height_mm,
            )
        xs = [p[0] for p in self.points]
        ys = [p[1] for p in self.points]
        return (min(xs), min(ys), max(xs), max(ys))

    def area_mm2(self) -> float:
        if self.shape == "rect":
            return self.width_mm * self.height_mm
        pts = self.points
        s = 0.0
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            s += x1 * y2 - x2 * y1
        return abs(s) / 2.0


@dataclass
class Placement:
    """Where a component ended up. Produced by the placer, not by the LLM."""

    ref: str
    x_mm: float
    y_mm: float
    rotation_deg: float = 0.0
    side: str = "top"
    locked: bool = False

    def __post_init__(self) -> None:
        self.ref = self.ref.strip().upper()
        self.side = _norm_side(self.side)
        self.x_mm = float(self.x_mm)
        self.y_mm = float(self.y_mm)
        self.rotation_deg = float(self.rotation_deg) % 360.0


@dataclass
class Design:
    """A complete board description: the unit of work in this library."""

    name: str = "untitled"
    description: str = ""
    components: List[Component] = field(default_factory=list)
    nets: List[Net] = field(default_factory=list)
    rules: DesignRules = field(default_factory=DesignRules)
    outline: BoardOutline = field(default_factory=BoardOutline)
    placements: Dict[str, Placement] = field(default_factory=dict)
    #: free-form notes the model can leave for a human reviewer
    notes: List[str] = field(default_factory=list)

    # -- lookup -----------------------------------------------------------

    def component(self, ref: str) -> Optional[Component]:
        ref = ref.strip().upper()
        for c in self.components:
            if c.ref == ref:
                return c
        return None

    def net(self, name: str) -> Optional[Net]:
        for n in self.nets:
            if n.name == name:
                return n
        return None

    def nets_of(self, ref: str) -> List[Net]:
        """Every net that touches ``ref``."""
        ref = ref.strip().upper()
        return [n for n in self.nets if ref in n.refs]

    def pads_of(self, ref: str) -> Dict[str, str]:
        """Map ``pad -> net name`` for one component."""
        ref = ref.strip().upper()
        out: Dict[str, str] = {}
        for n in self.nets:
            for r, p in n.connections:
                if r == ref:
                    out[p] = n.name
        return out

    # -- mutation ---------------------------------------------------------

    def add_component(self, comp: Component, replace: bool = False) -> Component:
        existing = self.component(comp.ref)
        if existing is not None:
            if not replace:
                raise ValueError("duplicate reference designator %r" % (comp.ref,))
            self.components.remove(existing)
        self.components.append(comp)
        return comp

    def remove_component(self, ref: str) -> None:
        ref = ref.strip().upper()
        c = self.component(ref)
        if c is not None:
            self.components.remove(c)
        self.placements.pop(ref, None)
        for n in self.nets:
            n.connections = [(r, p) for r, p in n.connections if r != ref]
        self.nets = [n for n in self.nets if n.connections]

    def connect(
        self,
        net_name: str,
        connections: Iterable[Tuple[str, str]],
        net_class: str = "Default",
    ) -> Net:
        """Add connections to ``net_name``, creating the net if needed."""
        net = self.net(net_name)
        if net is None:
            net = Net(name=net_name, net_class=net_class)
            self.nets.append(net)
        for ref, pad in connections:
            net.add(ref, pad)
        return net

    def next_ref(self, prefix: str) -> str:
        """Return the next free reference for ``prefix``, e.g. ``'R'`` -> ``'R4'``."""
        prefix = prefix.strip().upper()
        used = set()
        for c in self.components:
            m = re.match(r"^%s(\d+)$" % re.escape(prefix), c.ref)
            if m:
                used.add(int(m.group(1)))
        i = 1
        while i in used:
            i += 1
        return "%s%d" % (prefix, i)

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["nets"] = [
            {
                "name": n.name,
                "net_class": n.net_class,
                "connections": [list(c) for c in n.connections],
            }
            for n in self.nets
        ]
        d["outline"]["points"] = [list(p) for p in self.outline.points]
        d["placements"] = {k: asdict(v) for k, v in self.placements.items()}
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Design":
        rules_d = dict(data.get("rules") or {})
        ncs = [NetClass(**nc) for nc in (rules_d.pop("net_classes", None) or [])]
        if ncs:
            rules = DesignRules(net_classes=ncs, **rules_d)
        else:
            rules = DesignRules(**rules_d)

        outline_d = dict(data.get("outline") or {})
        outline_d["points"] = [tuple(p) for p in (outline_d.get("points") or [])]
        outline = BoardOutline(**outline_d)

        return cls(
            name=data.get("name", "untitled"),
            description=data.get("description", ""),
            components=[Component(**c) for c in data.get("components", [])],
            nets=[
                Net(
                    name=n["name"],
                    connections=[tuple(c) for c in n.get("connections", [])],
                    net_class=n.get("net_class", "Default"),
                )
                for n in data.get("nets", [])
            ],
            rules=rules,
            outline=outline,
            placements={
                k: Placement(**v) for k, v in (data.get("placements") or {}).items()
            },
            notes=list(data.get("notes", [])),
        )

    def summary(self) -> str:
        """A compact human/LLM-readable description of the current state."""
        if self.outline.shape == "rect":
            geom = "%d-layer, %g x %g mm" % (
                self.rules.copper_layers,
                self.outline.width_mm,
                self.outline.height_mm,
            )
        else:
            geom = "%d-layer, polygon outline (%.1f mm2)" % (
                self.rules.copper_layers,
                self.outline.area_mm2(),
            )
        lines = ["Design: %s" % self.name]
        if self.description:
            lines.append("  %s" % self.description)
        lines.append(
            "  %d components, %d nets, %d/%d placed"
            % (
                len(self.components),
                len(self.nets),
                len(self.placements),
                len(self.components),
            )
        )
        lines.append("  " + geom)
        return "\n".join(lines)
