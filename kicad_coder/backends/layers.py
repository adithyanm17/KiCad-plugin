"""Layer id tables.

KiCad 9 renumbered ``PCB_LAYER_ID``: in KiCad 8 ``B.Cu`` is 31 and inner
copper runs 1..30; in KiCad 9 ``B.Cu`` is 2 and inner copper runs 4, 6, 8...
with the technical layers interleaved on the odd numbers.

A board file declares a format version, and the layer ids inside it must match
that version's numbering, so both tables are kept here and selected by the
target version. Values were taken from ``include/layer_ids.h`` in the KiCad
source and cross-checked against the boards in ``demos/``.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

__all__ = ["LayerMap", "V8", "V9", "V10", "for_version",
           "BOARD_FILE_VERSIONS", "nets_by_name"]

#: board file format version -> (generator_version string, layer table key)
#:
#: Values are taken from boards the corresponding KiCad release writes:
#: 20240108 from KiCad 8, 20241229 from KiCad 9, and 20260206 from the
#: KiCad 10 board shipped in its own demos (pic_programmer).
BOARD_FILE_VERSIONS: Dict[str, Tuple[int, str]] = {
    "8.0": (20240108, "8.0"),
    "9.0": (20241229, "9.0"),
    "10.0": (20260206, "10.0"),
}


def nets_by_name(version: str) -> bool:
    """True when nets are written by name only, with no numbered net table.

    KiCad 10 dropped the top-level ``(net <code> "<name>")`` table: a pad now
    carries ``(net "VCC")`` and the net is created from that name. The parser
    still reads the old numbered form -- its own comment calls it "legacy
    files (pre-10.0)" -- but writing the modern shape avoids a migration
    prompt when the board is opened.
    """
    return version in ("10", "10.0")


class LayerMap:
    """Maps layer names to ids for one KiCad file format generation."""

    def __init__(self, name: str, copper: Dict[str, int],
                 tech: List[Tuple[int, str, str, str]]) -> None:
        self.name = name
        self._copper = copper
        self._tech = tech  # (id, name, type, user_name)

    def copper_id(self, layer: str) -> int:
        return self._copper[layer]

    def id_of(self, layer: str) -> int:
        if layer in self._copper:
            return self._copper[layer]
        for lid, lname, _, _ in self._tech:
            if lname == layer:
                return lid
        raise KeyError("unknown layer %r for KiCad %s" % (layer, self.name))

    def copper_names(self, count: int) -> List[str]:
        """Copper layer names for an ``count``-layer stackup, top to bottom."""
        if count < 1:
            raise ValueError("need at least one copper layer")
        if count == 1:
            return ["F.Cu"]
        inner = ["In%d.Cu" % i for i in range(1, count - 1)]
        return ["F.Cu"] + inner + ["B.Cu"]

    def stackup_entries(self, count: int) -> List[Tuple[int, str, str, str]]:
        """``(id, name, type, user_name)`` rows for the board's ``layers`` block."""
        rows: List[Tuple[int, str, str, str]] = []
        for name in self.copper_names(count):
            rows.append((self.copper_id(name), name, "signal", ""))
        rows.extend(self._tech)
        return rows


def _v8() -> LayerMap:
    copper = {"F.Cu": 0, "B.Cu": 31}
    for i in range(1, 31):
        copper["In%d.Cu" % i] = i
    tech = [
        (32, "B.Adhes", "user", "B.Adhesive"),
        (33, "F.Adhes", "user", "F.Adhesive"),
        (34, "B.Paste", "user", ""),
        (35, "F.Paste", "user", ""),
        (36, "B.SilkS", "user", "B.Silkscreen"),
        (37, "F.SilkS", "user", "F.Silkscreen"),
        (38, "B.Mask", "user", ""),
        (39, "F.Mask", "user", ""),
        (40, "Dwgs.User", "user", "User.Drawings"),
        (41, "Cmts.User", "user", "User.Comments"),
        (42, "Eco1.User", "user", "User.Eco1"),
        (43, "Eco2.User", "user", "User.Eco2"),
        (44, "Edge.Cuts", "user", ""),
        (45, "Margin", "user", ""),
        (46, "B.CrtYd", "user", "B.Courtyard"),
        (47, "F.CrtYd", "user", "F.Courtyard"),
        (48, "B.Fab", "user", ""),
        (49, "F.Fab", "user", ""),
    ]
    return LayerMap("8.0", copper, tech)


def _v9() -> LayerMap:
    copper = {"F.Cu": 0, "B.Cu": 2}
    for i in range(1, 31):
        copper["In%d.Cu" % i] = 2 + 2 * i
    tech = [
        (9, "F.Adhes", "user", "F.Adhesive"),
        (11, "B.Adhes", "user", "B.Adhesive"),
        (13, "F.Paste", "user", ""),
        (15, "B.Paste", "user", ""),
        (5, "F.SilkS", "user", "F.Silkscreen"),
        (7, "B.SilkS", "user", "B.Silkscreen"),
        (1, "F.Mask", "user", ""),
        (3, "B.Mask", "user", ""),
        (17, "Dwgs.User", "user", "User.Drawings"),
        (19, "Cmts.User", "user", "User.Comments"),
        (21, "Eco1.User", "user", "User.Eco1"),
        (23, "Eco2.User", "user", "User.Eco2"),
        (25, "Edge.Cuts", "user", ""),
        (27, "Margin", "user", ""),
        (31, "F.CrtYd", "user", "F.Courtyard"),
        (29, "B.CrtYd", "user", "B.Courtyard"),
        (35, "F.Fab", "user", ""),
        (33, "B.Fab", "user", ""),
    ]
    return LayerMap("9.0", copper, tech)


V8 = _v8()
V9 = _v9()
#: KiCad 10 keeps KiCad 9's layer numbering; only the net encoding changed.
V10 = _v9()
V10.name = "10.0"


def for_version(version: str) -> LayerMap:
    if version in ("8", "8.0"):
        return V8
    if version in ("9", "9.0"):
        return V9
    if version in ("10", "10.0"):
        return V10
    raise ValueError(
        "unsupported KiCad version %r -- use '8.0', '9.0' or '10.0'"
        % (version,)
    )


#: Front/back layer pairs, used when flipping a footprint to the bottom side.
FLIP_PAIRS = [
    ("F.Cu", "B.Cu"),
    ("F.Adhes", "B.Adhes"),
    ("F.Paste", "B.Paste"),
    ("F.SilkS", "B.SilkS"),
    ("F.Mask", "B.Mask"),
    ("F.CrtYd", "B.CrtYd"),
    ("F.Fab", "B.Fab"),
]

_FLIP: Dict[str, str] = {}
for _a, _b in FLIP_PAIRS:
    _FLIP[_a] = _b
    _FLIP[_b] = _a


def flip_layer(layer: str) -> str:
    """Return the opposite-side layer name, or ``layer`` if it has no pair."""
    return _FLIP.get(layer, layer)
