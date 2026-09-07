"""Footprint discovery and pad extraction.

A :class:`FootprintLibrary` merges three sources, in priority order:

1. directories the caller passes explicitly
2. KiCad's installed ``.pretty`` libraries, if KiCad was found
3. the built-in parametric footprints from :mod:`kicad_coder.library.builtin`

The built-ins are last so a real KiCad library always wins, but they guarantee
that a design using common passives can be built on a machine with no KiCad.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from ..backends import sexpr
from ..errors import LibraryError
from ..fab.toolchain import Toolchain, find_toolchain
from . import builtin

__all__ = ["Pad", "FootprintDef", "FootprintLibrary"]


@dataclass
class Pad:
    """One pad of a footprint, in footprint-local mm coordinates."""

    number: str
    type: str = "smd"  # smd | thru_hole | np_thru_hole | connect
    shape: str = "rect"
    x_mm: float = 0.0
    y_mm: float = 0.0
    width_mm: float = 0.0
    height_mm: float = 0.0
    drill_mm: float = 0.0
    rotation_deg: float = 0.0
    layers: List[str] = field(default_factory=list)

    @property
    def is_plated_hole(self) -> bool:
        return self.type == "thru_hole"

    @property
    def is_mechanical(self) -> bool:
        """A pad with no number carries no net (mounting holes, shields)."""
        return not self.number or self.type == "np_thru_hole"


@dataclass
class FootprintDef:
    """A parsed ``.kicad_mod``."""

    lib: str
    name: str
    node: list
    source: str = ""  # file path, or "<builtin>"

    @property
    def libid(self) -> str:
        return "%s:%s" % (self.lib, self.name)

    @property
    def description(self) -> str:
        return sexpr.sexp_str(sexpr.get(self.node, "descr", default=""))

    @property
    def tags(self) -> str:
        return sexpr.sexp_str(sexpr.get(self.node, "tags", default=""))

    @property
    def pads(self) -> List[Pad]:
        out: List[Pad] = []
        for p in sexpr.find_all(self.node, "pad"):
            # (pad "1" smd roundrect (at x y [rot]) (size w h) (drill d) (layers ...))
            number = sexpr.sexp_str(p[1]) if len(p) > 1 else ""
            ptype = sexpr.sexp_str(p[2]) if len(p) > 2 else "smd"
            shape = sexpr.sexp_str(p[3]) if len(p) > 3 else "rect"
            at = sexpr.find(p, "at") or []
            size = sexpr.find(p, "size") or []
            drill = sexpr.find(p, "drill")
            layers = sexpr.find(p, "layers") or []

            drill_mm = 0.0
            if drill:
                for v in drill[1:]:
                    if isinstance(v, (int, float)):
                        drill_mm = float(v)
                        break

            out.append(
                Pad(
                    number=number,
                    type=ptype,
                    shape=shape,
                    x_mm=float(at[1]) if len(at) > 1 else 0.0,
                    y_mm=float(at[2]) if len(at) > 2 else 0.0,
                    rotation_deg=float(at[3]) if len(at) > 3 else 0.0,
                    width_mm=float(size[1]) if len(size) > 1 else 0.0,
                    height_mm=float(size[2]) if len(size) > 2 else 0.0,
                    drill_mm=drill_mm,
                    layers=[sexpr.sexp_str(x) for x in layers[1:]],
                )
            )
        return out

    @property
    def pad_numbers(self) -> List[str]:
        """Distinct, numbered pads -- what a netlist may legally reference."""
        seen: List[str] = []
        for p in self.pads:
            if p.number and p.number not in seen:
                seen.append(p.number)
        return seen

    def has_pad(self, number: str) -> bool:
        return str(number).strip() in self.pad_numbers

    def courtyard_bbox(self) -> Tuple[float, float, float, float]:
        """``(min_x, min_y, max_x, max_y)`` of the courtyard, else pad extents.

        The placer uses this to keep parts from overlapping, so falling back to
        a padded pad-extent box matters for footprints with no courtyard.
        """
        xs: List[float] = []
        ys: List[float] = []
        for key in ("fp_rect", "fp_line", "fp_poly", "fp_circle", "fp_arc"):
            for item in sexpr.find_all(self.node, key):
                layer = sexpr.sexp_str(sexpr.get(item, "layer", default=""))
                if "CrtYd" not in layer:
                    continue

                if key == "fp_circle":
                    # (fp_circle (center x y) (end x y)) -- "end" is a point ON
                    # the circle, not a bounding corner. Treating it as a
                    # corner collapses the box to a line and lets other parts
                    # sit on top of the hole.
                    centre = sexpr.find(item, "center")
                    edge = sexpr.find(item, "end")
                    if centre and edge and len(centre) > 2 and len(edge) > 2:
                        cx, cy = float(centre[1]), float(centre[2])
                        r = ((float(edge[1]) - cx) ** 2
                             + (float(edge[2]) - cy) ** 2) ** 0.5
                        xs.extend([cx - r, cx + r])
                        ys.extend([cy - r, cy + r])
                    continue

                for tag in ("start", "end", "center", "mid"):
                    pt = sexpr.find(item, tag)
                    if pt and len(pt) > 2:
                        xs.append(float(pt[1]))
                        ys.append(float(pt[2]))
                poly = sexpr.find(item, "pts")
                if poly:
                    for xy in sexpr.find_all(poly, "xy"):
                        xs.append(float(xy[1]))
                        ys.append(float(xy[2]))
        if xs and ys and max(xs) > min(xs) and max(ys) > min(ys):
            return (min(xs), min(ys), max(xs), max(ys))

        for p in self.pads:
            xs.extend([p.x_mm - p.width_mm / 2, p.x_mm + p.width_mm / 2])
            ys.extend([p.y_mm - p.height_mm / 2, p.y_mm + p.height_mm / 2])
        if not xs:
            return (-0.5, -0.5, 0.5, 0.5)
        margin = 0.25
        return (min(xs) - margin, min(ys) - margin,
                max(xs) + margin, max(ys) + margin)

    def size_mm(self) -> Tuple[float, float]:
        x0, y0, x1, y1 = self.courtyard_bbox()
        return (x1 - x0, y1 - y0)

    def silk_overhang_mm(self) -> float:
        """How far silkscreen extends beyond the courtyard.

        Reference designators sit outside the courtyard by design -- an 0603's
        courtyard is 0.73 mm tall but its reference text is centred 1.43 mm
        out. Placement that only respects courtyards therefore lets KiCad clip
        the silkscreen against the board edge, so the placer holds parts back
        by this much extra.
        """
        cx0, cy0, cx1, cy1 = self.courtyard_bbox()
        overhang = 0.0

        for key in ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly"):
            for item in sexpr.find_all(self.node, key):
                if "SilkS" not in sexpr.sexp_str(
                        sexpr.get(item, "layer", default="")):
                    continue
                for tag in ("start", "end", "center", "mid"):
                    pt = sexpr.find(item, tag)
                    if pt and len(pt) > 2:
                        overhang = max(
                            overhang,
                            cx0 - float(pt[1]), float(pt[1]) - cx1,
                            cy0 - float(pt[2]), float(pt[2]) - cy1)

        for key in ("property", "fp_text"):
            for item in sexpr.find_all(self.node, key):
                if "SilkS" not in sexpr.sexp_str(
                        sexpr.get(item, "layer", default="")):
                    continue
                at = sexpr.find(item, "at")
                if not at or len(at) < 3:
                    continue
                x, y = float(at[1]), float(at[2])
                # Text is centred on its anchor; half a line of it in every
                # direction is a reasonable, slightly generous envelope.
                size = sexpr.find(sexpr.find(item, "effects") or [], "font")
                half = 0.6
                if size is not None:
                    dims = sexpr.find(size, "size")
                    if dims and len(dims) > 2:
                        half = max(float(dims[1]), float(dims[2]))
                overhang = max(
                    overhang,
                    cx0 - (x - half), (x + half) - cx1,
                    cy0 - (y - half), (y + half) - cy1)

        return max(0.0, overhang)

    def clone_node(self) -> list:
        """A deep copy of the raw S-expression, safe to mutate."""
        return _deep_copy(self.node)


def _deep_copy(node):
    if isinstance(node, list):
        return [_deep_copy(c) for c in node]
    return node


class FootprintLibrary:
    """Resolves ``"Lib:Name"`` ids to parsed footprints."""

    def __init__(
        self,
        search_dirs: Sequence[str] = (),
        toolchain: Optional[Toolchain] = None,
        include_builtin: bool = True,
        auto_detect: bool = True,
    ) -> None:
        self._explicit_dirs = [d for d in search_dirs if os.path.isdir(d)]
        self._include_builtin = include_builtin
        self._toolchain = toolchain if toolchain is not None else (
            find_toolchain() if auto_detect else Toolchain()
        )
        self._index: Optional[Dict[str, str]] = None  # libid -> path
        self._cache: Dict[str, FootprintDef] = {}

    # -- indexing ---------------------------------------------------------

    @property
    def search_dirs(self) -> List[str]:
        dirs = list(self._explicit_dirs)
        dirs.extend(d for d in self._toolchain.footprint_dirs if d not in dirs)
        return dirs

    def _build_index(self) -> Dict[str, str]:
        index: Dict[str, str] = {}
        # Reverse order so earlier search dirs overwrite later ones.
        for root in reversed(self.search_dirs):
            try:
                entries = os.listdir(root)
            except OSError:
                continue
            for entry in entries:
                if not entry.endswith(".pretty"):
                    continue
                lib = entry[: -len(".pretty")]
                libdir = os.path.join(root, entry)
                try:
                    mods = os.listdir(libdir)
                except OSError:
                    continue
                for mod in mods:
                    if mod.endswith(".kicad_mod"):
                        name = mod[: -len(".kicad_mod")]
                        index["%s:%s" % (lib, name)] = os.path.join(libdir, mod)
        return index

    @property
    def index(self) -> Dict[str, str]:
        if self._index is None:
            self._index = self._build_index()
        return self._index

    def refresh(self) -> None:
        self._index = None
        self._cache.clear()

    def libids(self) -> List[str]:
        """Every resolvable footprint id."""
        ids = set(self.index)
        if self._include_builtin:
            ids.update(builtin.BUILTIN_LIBRARY)
        return sorted(ids)

    def libraries(self) -> List[str]:
        return sorted({lid.split(":", 1)[0] for lid in self.libids()})

    # -- lookup -----------------------------------------------------------

    def exists(self, libid: str) -> bool:
        libid = libid.strip()
        return libid in self.index or (
            self._include_builtin and libid in builtin.BUILTIN_LIBRARY
        )

    def get(self, libid: str) -> FootprintDef:
        """Load and parse one footprint. Raises :class:`LibraryError`."""
        libid = libid.strip()
        if libid in self._cache:
            return self._cache[libid]

        if ":" not in libid:
            raise LibraryError(
                "footprint id must be 'Library:Name', got %r" % (libid,)
            )
        lib, name = libid.split(":", 1)

        text: Optional[str] = None
        source = ""
        path = self.index.get(libid)
        if path:
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    text = fh.read()
                source = path
            except OSError as exc:
                raise LibraryError("could not read %s: %s" % (path, exc)) from exc
        elif self._include_builtin and libid in builtin.BUILTIN_LIBRARY:
            text = builtin.BUILTIN_LIBRARY[libid]
            source = "<builtin>"

        if text is None:
            hint = ""
            near = self.search(name, limit=3)
            if near:
                hint = "  Did you mean: %s" % ", ".join(near)
            raise LibraryError(
                "footprint %r not found in %d indexed footprints.%s"
                % (libid, len(self.libids()), hint)
            )

        try:
            node = sexpr.parse(text)
        except ValueError as exc:
            raise LibraryError("malformed footprint %s: %s" % (libid, exc)) from exc
        if not isinstance(node, list) or not node or str(node[0]) != "footprint":
            raise LibraryError("%s is not a footprint definition" % libid)

        fp = FootprintDef(lib=lib, name=name, node=node, source=source)
        self._cache[libid] = fp
        return fp

    # -- search -----------------------------------------------------------

    def search(self, query: str, limit: int = 20,
               library: str = "") -> List[str]:
        """Fuzzy-ish search over footprint ids.

        Scores substring hits, then token hits, so ``"0603 resistor"`` finds
        ``Resistor_SMD:R_0603_1608Metric`` without needing exact syntax.
        """
        q = query.strip().lower()
        if not q:
            return []
        tokens = [t for t in re.split(r"[\s_:,\-]+", q) if t]

        scored: List[Tuple[float, str]] = []
        for libid in self.libids():
            if library and not libid.lower().startswith(library.lower() + ":"):
                continue
            hay = libid.lower()
            name_part = hay.split(":", 1)[1]

            score = 0.0
            if q == name_part:
                score += 100
            if q in hay:
                score += 40

            hit = 0
            for t in tokens:
                pos = name_part.find(t)
                if pos < 0:
                    if t not in hay:
                        continue
                    # matched only in the library name, which is weaker
                    hit += 1
                    score += 6
                    continue
                hit += 1
                score += 12
                # Position matters more than it looks. A metric package code
                # appears late in a name -- "R_0201_0603Metric" contains 0603
                # because an 0201 imperial part is 0603 metric. Ranking that
                # above "R_0603_1608Metric" for the query "0603" hands a model
                # a part a third of the intended size, so an early match wins.
                score += max(0.0, 6.0 - pos * 0.8)
                # Whole-field match, delimited by separators or string ends.
                before_ok = pos == 0 or name_part[pos - 1] in "_-."
                end = pos + len(t)
                after_ok = end == len(name_part) or name_part[end] in "_-."
                if before_ok and after_ok:
                    score += 8

            if hit == 0:
                continue
            if hit == len(tokens):
                score += 25
            # prefer shorter, more canonical names
            score -= len(name_part) * 0.05
            scored.append((score, libid))

        scored.sort(key=lambda s: (-s[0], s[1]))
        return [lid for _, lid in scored[:limit]]

    def describe(self, libid: str) -> Dict[str, object]:
        """A compact dict for feeding back to a model."""
        fp = self.get(libid)
        w, h = fp.size_mm()
        return {
            "libid": fp.libid,
            "description": fp.description,
            "tags": fp.tags,
            "pads": fp.pad_numbers,
            "pad_count": len(fp.pad_numbers),
            "size_mm": [round(w, 3), round(h, 3)],
            "source": "builtin" if fp.source == "<builtin>" else fp.source,
        }

    def status(self) -> str:
        n_kicad = len(self.index)
        n_builtin = len(builtin.BUILTIN_LIBRARY) if self._include_builtin else 0
        dirs = self.search_dirs
        lines = [
            "Footprint library: %d from KiCad, %d built-in, %d libraries total"
            % (n_kicad, n_builtin, len(self.libraries()))
        ]
        if dirs:
            lines.append("  search dirs: " + ", ".join(dirs))
        else:
            lines.append(
                "  search dirs: none found -- using built-in footprints only"
            )
        return "\n".join(lines)
