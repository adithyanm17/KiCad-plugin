"""Emit a ``.kicad_pcb`` (and its ``.kicad_pro``) in pure Python.

Writing the S-expression directly, rather than driving ``pcbnew``, is what lets
this library run anywhere: in CI, in a container, inside a plugin, or on a
machine where KiCad has not been installed yet.

Two facts govern how footprints are placed, both confirmed against the KiCad
parser source:

* pad **positions** are stored in unrotated footprint-local coordinates
  (``PAD::SetFPRelativePosition``)
* pad **angles** are stored in the board frame -- absolute, not relative -- so
  a footprint rotated by R writes each pad at ``local_angle + R``

Design rules and net classes are written to the ``.kicad_pro`` project file,
because that is where KiCad 7+ reads them from; a board file alone will be
checked against defaults.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import BackendError, ValidationError
from ..ir.types import Component, Design, Placement
from ..ir.validate import validate
from . import sexpr
from .layers import (BOARD_FILE_VERSIONS, LayerMap, flip_layer, for_version,
                     nets_by_name)
from .sexpr import Sym

__all__ = ["BoardWriter", "write_board", "BoardResult", "resolve_version",
           "DEFAULT_VERSION"]

#: Used when no version is given and no KiCad installation can be found.
#: KiCad 9 format is the safe middle ground: KiCad 10 opens and migrates it,
#: and it stays readable by KiCad 9.
DEFAULT_VERSION = "9.0"


def resolve_version(version: str = "auto") -> str:
    """Turn ``"auto"`` into a concrete board format version.

    ``auto`` matches the installed KiCad, so a board opens natively rather
    than prompting to migrate. With no KiCad present it falls back to
    :data:`DEFAULT_VERSION`.
    """
    if version and version != "auto":
        return version
    try:
        from ..fab.toolchain import find_toolchain
        tool = find_toolchain()
    except Exception:
        return DEFAULT_VERSION
    if not tool.available or not tool.version:
        return DEFAULT_VERSION
    major = tool.version.strip().split(".")[0]
    candidate = "%s.0" % major
    return candidate if candidate in BOARD_FILE_VERSIONS else DEFAULT_VERSION


class BoardResult:
    """What :func:`write_board` produced."""

    def __init__(self, pcb_path: str, pro_path: str, net_count: int,
                 footprint_count: int, warnings: List[str]) -> None:
        self.pcb_path = pcb_path
        self.pro_path = pro_path
        self.net_count = net_count
        self.footprint_count = footprint_count
        self.warnings = warnings

    def summary(self) -> str:
        lines = [
            "Wrote %s" % self.pcb_path,
            "  %d footprints, %d nets" % (self.footprint_count, self.net_count),
            "  project file: %s" % self.pro_path,
        ]
        for w in self.warnings:
            lines.append("  warning: %s" % w)
        return "\n".join(lines)


def _deep_copy(node):
    if isinstance(node, list):
        return [_deep_copy(c) for c in node]
    return node


#: Project keys this library owns. Everything else in an existing .kicad_pro
#: is preserved untouched.
_OWNED_PROJECT_PATHS = (
    ("board", "design_settings", "rules"),
    ("board", "design_settings", "defaults"),
    ("board", "design_settings", "track_widths"),
    ("board", "design_settings", "via_dimensions"),
    ("net_settings", "classes"),
    ("net_settings", "netclass_patterns"),
    ("meta", "filename"),
)


def _dig(data: dict, path: Sequence[str]):
    node = data
    for key in path:
        if not isinstance(node, dict) or key not in node:
            return None, False
        node = node[key]
    return node, True


def _plant(data: dict, path: Sequence[str], value) -> None:
    node = data
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def _merge_project(existing: dict, generated: dict) -> dict:
    """Write our design settings into an existing project, keeping the rest.

    A ``.kicad_pro`` carries far more than design rules -- KiCad 10 adds
    component classes, tuning profiles and cvpcb state, and a user may have
    their own settings in there. Replacing the file wholesale silently discards
    all of it, so only the keys this library actually owns are overwritten.
    """
    merged = json.loads(json.dumps(existing))  # deep copy
    for path in _OWNED_PROJECT_PATHS:
        value, found = _dig(generated, path)
        if found:
            _plant(merged, path, value)
    return merged


class BoardWriter:
    """Turns a :class:`Design` into KiCad files."""

    def __init__(self, design: Design, library, version: str = "auto") -> None:
        version = resolve_version(version)
        if version not in BOARD_FILE_VERSIONS:
            raise BackendError(
                "unsupported KiCad version %r -- use one of %s"
                % (version, ", ".join(sorted(BOARD_FILE_VERSIONS)))
            )
        self.design = design
        self.library = library
        self.version = version
        self.file_version, _ = BOARD_FILE_VERSIONS[version]
        self.layers: LayerMap = for_version(version)
        self.nets_by_name = nets_by_name(version)
        self.warnings: List[str] = []
        self._net_codes: Dict[str, int] = {}

    # -- nets -------------------------------------------------------------

    def _assign_net_codes(self) -> Dict[str, int]:
        """Net 0 is always the unconnected net; user nets start at 1."""
        codes = {"": 0}
        for i, net in enumerate(self.design.nets, start=1):
            codes[net.name] = i
        self._net_codes = codes
        return codes

    def _net_of_pad(self, ref: str, pad: str) -> Tuple[int, str]:
        for net in self.design.nets:
            if (ref, pad) in net.connections:
                return (self._net_codes[net.name], net.name)
        return (0, "")

    # -- footprints -------------------------------------------------------

    def _place_footprint(self, comp: Component, placement: Placement) -> list:
        fp = self.library.get(comp.footprint)
        node = _deep_copy(fp.node)

        flip = placement.side == "bottom"
        rot = placement.rotation_deg

        # header: (footprint "Lib:Name" (layer ...) (at x y rot) ...)
        node[1] = comp.footprint  # board files use the full library id

        # A library footprint carries no board-level identity; drop anything
        # stale so KiCad regenerates it on load rather than complaining about
        # duplicates when the same footprint is placed many times. version and
        # generator are library-file tokens -- KiCad does not write them on a
        # footprint instance inside a board.
        # The library's own attribute flags are meaningful -- KiCad marks test
        # points and mounting holes exclude_from_pos_files/exclude_from_bom --
        # so keep them and merge, rather than overwriting with our own guess.
        lib_attrs = sexpr.find(node, "attr")
        self._lib_attrs = ([sexpr.sexp_str(a) for a in lib_attrs[1:]]
                           if lib_attrs is not None else None)

        for key in ("uuid", "tstamp", "path", "version", "generator",
                    "generator_version", "attr"):
            sexpr.remove_children(node, key)

        # Mirror first, then stamp position and layer. Doing it the other way
        # round lets the child transform flip the footprint's own layer back,
        # which silently lands bottom-side parts on the top copper.
        self._transform_children(node, flip=flip, rot=rot, top_level=True)

        sexpr.replace_child(node, "layer",
                            [Sym("layer"), "B.Cu" if flip else "F.Cu"])

        at = [Sym("at"), round(placement.x_mm, 4), round(placement.y_mm, 4)]
        if rot:
            at.append(round(rot, 4))
        sexpr.replace_child(node, "at", at)
        self._set_identity(node, comp, flip)
        self._rotate_text(node, flip=flip, rot=rot)
        self._assign_pad_nets(node, comp, flip=flip, rot=rot)

        attrs = self._attributes(node, comp)
        if attrs:
            node.append([Sym("attr")] + [Sym(a) for a in attrs])

        return node

    def _attributes(self, node: list, comp: Component) -> List[str]:
        """Build the single consolidated ``(attr ...)`` list KiCad expects.

        Mounting type comes first and is inferred from the pads: KiCad uses it
        to decide whether a part belongs in the pick-and-place file.
        """
        lib_attrs = getattr(self, "_lib_attrs", None)
        attrs: List[str] = list(lib_attrs or [])

        # Only infer a mounting type when the library declared no attr list at
        # all. If it declared one, it is authoritative -- KiCad's TestPoint
        # footprints deliberately carry no "smd" flag, and adding one makes the
        # board copy differ from the library and trips lib_footprint_mismatch.
        if lib_attrs is None:
            pads = sexpr.find_all(node, "pad")
            kinds = {sexpr.sexp_str(p[2]) for p in pads if len(p) > 2}
            if kinds and kinds <= {"smd", "connect"}:
                attrs.insert(0, "smd")
            elif "thru_hole" in kinds:
                attrs.insert(0, "through_hole")
            elif not kinds or kinds == {"np_thru_hole"}:
                # Only mechanical pads: nothing for a machine to place.
                if "exclude_from_pos_files" not in attrs:
                    attrs.append("exclude_from_pos_files")

        for flag, wanted in (("exclude_from_pos_files", comp.exclude_from_pos),
                             ("exclude_from_bom", comp.exclude_from_bom),
                             ("dnp", comp.dnp)):
            if wanted and flag not in attrs:
                attrs.append(flag)
        return attrs

    def _rotate_text(self, node: list, flip: bool, rot: float) -> None:
        """Text angles are board-frame absolute, exactly like pad angles."""
        if not rot and not flip:
            return
        for key in ("property", "fp_text"):
            for item in sexpr.find_all(node, key):
                at = sexpr.find(item, "at")
                if at is None:
                    continue
                local = float(at[3]) if len(at) > 3 else 0.0
                angle = (-(local) - rot) if flip else (local + rot)
                angle %= 360.0
                if len(at) > 3:
                    at[3] = round(angle, 4)
                else:
                    at.append(round(angle, 4))

    def _transform_children(self, node: list, flip: bool, rot: float,
                            top_level: bool = False) -> None:
        """Mirror geometry and flip layer names for bottom-side placement.

        KiCad mirrors a flipped footprint about the horizontal axis: local Y
        negates and every front layer becomes its back counterpart. Pad angles
        are handled separately in :meth:`_assign_pad_nets` because they are
        board-frame absolute.
        """
        if not flip:
            return
        for child in node:
            if not isinstance(child, list) or not child:
                continue
            head = str(child[0])

            if head == "layer" and len(child) > 1:
                child[1] = flip_layer(sexpr.sexp_str(child[1]))
            elif head == "layers":
                for i in range(1, len(child)):
                    child[i] = flip_layer(sexpr.sexp_str(child[i]))
            elif head in ("at", "start", "end", "center", "mid", "xy") and len(child) > 2:
                if not (top_level and head == "at"):
                    child[2] = -float(child[2])
            elif head == "pts":
                for xy in sexpr.find_all(child, "xy"):
                    if len(xy) > 2:
                        xy[2] = -float(xy[2])

            # recurse, but never re-process the footprint's own (at ...)
            self._transform_children(child, flip=flip, rot=rot, top_level=False)

    def _set_identity(self, node: list, comp: Component, flip: bool) -> None:
        """Write the reference and value onto the placed footprint.

        KiCad 8 stores these as ``(property "Reference" ...)``; KiCad 6/7
        libraries use ``(fp_text reference ...)``. User libraries contain both
        vintages, so handle either and leave the form as we found it.
        """
        wrote_ref = wrote_val = False

        # Normalise the KiCad 6/7 form into the modern one. Both still load,
        # but emitting a single representation keeps the board file uniform
        # and lets downstream readers (the BOM extractor) look in one place.
        for txt in list(sexpr.find_all(node, "fp_text")):
            if len(txt) < 3:
                continue
            kind = sexpr.sexp_str(txt[1])
            key = {"reference": "Reference", "value": "Value"}.get(kind)
            if key is None:
                continue
            converted = [Sym("property"), key, sexpr.sexp_str(txt[2])]
            converted.extend(
                c for c in txt[3:]
                if isinstance(c, list) and str(c[0]) in ("at", "layer", "effects", "hide")
            )
            node[node.index(txt)] = converted

        for prop in sexpr.find_all(node, "property"):
            if len(prop) < 3:
                continue
            key = sexpr.sexp_str(prop[1])
            if key == "Reference":
                prop[2] = comp.ref
                wrote_ref = True
            elif key == "Value":
                prop[2] = comp.value
                wrote_val = True
            elif key == "Footprint":
                prop[2] = comp.footprint
            elif key == "Datasheet" and comp.datasheet:
                prop[2] = comp.datasheet
            elif key == "Description" and comp.description:
                prop[2] = comp.description

        for txt in sexpr.find_all(node, "fp_text"):  # any left after normalisation
            if len(txt) < 3:
                continue
            kind = sexpr.sexp_str(txt[1])
            if kind == "reference":
                txt[2] = comp.ref
                wrote_ref = True
            elif kind == "value":
                txt[2] = comp.value
                wrote_val = True

        silk = "B.SilkS" if flip else "F.SilkS"
        fab = "B.Fab" if flip else "F.Fab"
        if not wrote_ref:
            node.append(self._text_property("Reference", comp.ref, 0, -1.5, silk))
        if not wrote_val:
            node.append(self._text_property("Value", comp.value, 0, 1.5, fab))

        # Extra BOM fields ride along as hidden properties so a board-derived
        # BOM keeps the MPN even without a schematic.
        extra = dict(comp.fields)
        if comp.mpn:
            extra.setdefault("MPN", comp.mpn)
        if comp.manufacturer:
            extra.setdefault("Manufacturer", comp.manufacturer)
        if comp.datasheet:
            extra.setdefault("Datasheet", comp.datasheet)
        for key, value in extra.items():
            if not value:
                continue
            node.append(self._text_property(key, str(value), 0, 3.0, fab,
                                            hide=True))

    def _text_property(self, key: str, value: str, x: float, y: float,
                       layer: str, hide: bool = False) -> list:
        prop = [
            Sym("property"), key, value,
            [Sym("at"), x, y, 0],
            [Sym("layer"), layer],
            [Sym("effects"),
             [Sym("font"), [Sym("size"), 1.0, 1.0], [Sym("thickness"), 0.15]]],
        ]
        if hide:
            prop.insert(4, [Sym("hide"), Sym("yes")])
        return prop

    def _assign_pad_nets(self, node: list, comp: Component,
                         flip: bool, rot: float) -> None:
        used_pads = self.design.pads_of(comp.ref)
        for pad in sexpr.find_all(node, "pad"):
            number = sexpr.sexp_str(pad[1]) if len(pad) > 1 else ""

            # Pad angle is board-frame absolute (see module docstring).
            at = sexpr.find(pad, "at")
            if at is not None:
                local_angle = float(at[3]) if len(at) > 3 else 0.0
                board_angle = (-(local_angle) - rot) if flip else (local_angle + rot)
                board_angle %= 360.0
                if board_angle:
                    if len(at) > 3:
                        at[3] = round(board_angle, 4)
                    else:
                        at.append(round(board_angle, 4))
                elif len(at) > 3:
                    del at[3:]

            sexpr.remove_children(pad, "net")
            sexpr.remove_children(pad, "uuid")
            sexpr.remove_children(pad, "tstamp")

            if not number:
                continue  # mechanical pad, carries no net
            if number not in used_pads:
                continue  # unconnected pad: no (net ...) at all

            code, name = self._net_of_pad(comp.ref, number)
            if code:
                # KiCad 10 writes the net name alone; earlier formats need the
                # numeric code first.
                pad.append([Sym("net"), name] if self.nets_by_name
                           else [Sym("net"), code, name])

    # -- board geometry ---------------------------------------------------

    def _outline_items(self) -> List[list]:
        o = self.design.outline
        items: List[list] = []
        width = 0.1

        def line(x1, y1, x2, y2):
            return [
                Sym("gr_line"),
                [Sym("start"), round(x1, 4), round(y1, 4)],
                [Sym("end"), round(x2, 4), round(y2, 4)],
                [Sym("stroke"), [Sym("width"), width], [Sym("type"), Sym("solid")]],
                [Sym("layer"), "Edge.Cuts"],
            ]

        if o.shape == "rect":
            x0, y0, x1, y1 = o.bounds()
            items.extend([
                line(x0, y0, x1, y0),
                line(x1, y0, x1, y1),
                line(x1, y1, x0, y1),
                line(x0, y1, x0, y0),
            ])
        else:
            pts = o.points
            for i in range(len(pts)):
                x1, y1 = pts[i]
                x2, y2 = pts[(i + 1) % len(pts)]
                items.append(line(x1, y1, x2, y2))
        return items

    def _ground_zone(self, layer: str, net_name: str) -> Optional[list]:
        code = self._net_codes.get(net_name)
        if code is None:
            return None
        x0, y0, x1, y1 = self.design.outline.bounds()
        inset = self.design.rules.edge_clearance_mm
        x0, y0, x1, y1 = x0 + inset, y0 + inset, x1 - inset, y1 - inset
        if self.design.outline.shape == "rect":
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        else:
            pts = self.design.outline.points

        nc = self.design.rules.net_class("Default")
        net_fields = ([[Sym("net"), net_name]] if self.nets_by_name
                      else [[Sym("net"), code], [Sym("net_name"), net_name]])
        return [
            Sym("zone"),
        ] + net_fields + [
            [Sym("layer"), layer],
            [Sym("hatch"), Sym("edge"), 0.5],
            [Sym("connect_pads"), [Sym("clearance"), round(nc.clearance_mm, 4)]],
            [Sym("min_thickness"), round(self.design.rules.min_track_mm, 4)],
            [Sym("filled_areas_thickness"), Sym("no")],
            [Sym("fill"),
             [Sym("thermal_gap"), 0.5],
             [Sym("thermal_bridge_width"), 0.5]],
            [Sym("polygon"),
             [Sym("pts")] + [[Sym("xy"), round(x, 4), round(y, 4)] for x, y in pts]],
        ]

    # -- assembly ---------------------------------------------------------

    def build(self, ground_planes: bool = True) -> list:
        d = self.design
        codes = self._assign_net_codes()
        lm = self.layers

        board: list = [
            Sym("kicad_pcb"),
            [Sym("version"), self.file_version],
            [Sym("generator"), "kicad_coder"],
            [Sym("generator_version"), self.version],
            [Sym("general"),
             [Sym("thickness"), round(d.rules.board_thickness_mm, 4)],
             [Sym("legacy_teardrops"), Sym("no")]],
            [Sym("paper"), "A4"],
            [Sym("title_block"),
             [Sym("title"), d.name],
             [Sym("comment"), 1, d.description or "generated by kicad_coder"]],
        ]

        layers_node: list = [Sym("layers")]
        for lid, name, ltype, user in lm.stackup_entries(d.rules.copper_layers):
            row = [lid, name, Sym(ltype)]
            if user:
                row.append(user)
            layers_node.append(row)
        board.append(layers_node)

        board.append([
            Sym("setup"),
            [Sym("pad_to_mask_clearance"), 0],
            [Sym("allow_soldermask_bridges_in_footprints"), Sym("no")],
        ])

        # KiCad 10 has no numbered net table: nets come into being from the
        # names on pads, zones and tracks.
        if not self.nets_by_name:
            for name, code in sorted(codes.items(), key=lambda kv: kv[1]):
                board.append([Sym("net"), code, name])

        placed = 0
        for comp in d.components:
            placement = d.placements.get(comp.ref)
            if placement is None:
                self.warnings.append(
                    "%s has no placement; it was put at the board origin"
                    % comp.ref
                )
                placement = Placement(ref=comp.ref, x_mm=0.0, y_mm=0.0,
                                      side=comp.side)
            if not self.library.exists(comp.footprint):
                raise BackendError(
                    "cannot place %s: footprint %r not found"
                    % (comp.ref, comp.footprint)
                )
            board.append(self._place_footprint(comp, placement))
            placed += 1

        board.extend(self._outline_items())

        if ground_planes:
            ground = next((n.name for n in d.nets if n.is_ground()), None)
            if ground:
                names = lm.copper_names(d.rules.copper_layers)
                targets = [names[-1]] if len(names) > 1 else [names[0]]
                if d.rules.copper_layers >= 4:
                    targets = [names[1]]  # dedicate the first inner layer
                for layer in targets:
                    zone = self._ground_zone(layer, ground)
                    if zone:
                        board.append(zone)

        self._placed_count = placed
        return board

    # -- project file -----------------------------------------------------

    def project_dict(self, board_filename: str) -> dict:
        r = self.design.rules
        classes = []
        for nc in r.net_classes:
            classes.append({
                "bus_width": 12,
                "clearance": nc.clearance_mm,
                "diff_pair_gap": nc.diff_pair_gap_mm,
                "diff_pair_via_gap": nc.diff_pair_gap_mm,
                "diff_pair_width": nc.diff_pair_width_mm,
                "line_style": 0,
                "microvia_diameter": 0.508,
                "microvia_drill": 0.127,
                "name": nc.name,
                "pcb_color": "rgba(0, 0, 0, 0.000)",
                "priority": 2147483647 if nc.name == "Default" else 0,
                "schematic_color": "rgba(0, 0, 0, 0.000)",
                "track_width": nc.track_width_mm,
                "via_diameter": nc.via_diameter_mm,
                "via_drill": nc.via_drill_mm,
                "wire_width": 6,
            })

        patterns = []
        for net in self.design.nets:
            if net.net_class != "Default":
                patterns.append({"pattern": net.name, "netclass": net.net_class})

        return {
            "board": {
                "design_settings": {
                    "defaults": {
                        "board_outline_line_width": 0.05,
                        "copper_line_width": self.design.rules.min_track_mm,
                        "copper_text_size_h": 1.5,
                        "copper_text_size_v": 1.5,
                        "copper_text_thickness": 0.3,
                        "silk_line_width": 0.1,
                        "silk_text_size_h": 1.0,
                        "silk_text_size_v": 1.0,
                        "silk_text_thickness": 0.1,
                    },
                    "diff_pair_dimensions": [],
                    "drc_exclusions": [],
                    "rules": {
                        "allow_blind_buried_vias": r.allow_blind_buried_vias,
                        "allow_microvias": r.allow_microvias,
                        "max_error": 0.005,
                        "min_clearance": r.min_clearance_mm,
                        "min_connection": 0.0,
                        "min_copper_edge_clearance": r.edge_clearance_mm,
                        "min_hole_clearance": 0.25,
                        "min_hole_to_hole": r.min_hole_to_hole_mm,
                        "min_microvia_diameter": 0.2,
                        "min_microvia_drill": 0.1,
                        "min_resolved_spokes": 2,
                        "min_silk_clearance": 0.0,
                        "min_text_height": 0.8,
                        "min_text_thickness": 0.08,
                        "min_through_hole_diameter": r.min_via_drill_mm,
                        "min_track_width": r.min_track_mm,
                        "min_via_annular_width": r.min_annular_ring_mm,
                        "min_via_diameter": r.min_via_diameter_mm,
                        "solder_mask_to_copper_clearance": 0.0,
                        "use_height_for_length_calcs": True,
                    },
                    "track_widths": [0.0] + sorted(
                        {nc.track_width_mm for nc in r.net_classes}
                    ),
                    "via_dimensions": [{"diameter": 0.0, "drill": 0.0}] + [
                        {"diameter": nc.via_diameter_mm, "drill": nc.via_drill_mm}
                        for nc in r.net_classes
                    ],
                    "zones_allow_external_fillets": False,
                },
            },
            "boards": [],
            "libraries": {"pinned_footprint_libs": [], "pinned_symbol_libs": []},
            "meta": {"filename": board_filename, "version": 3},
            "net_settings": {
                "classes": classes,
                "meta": {"version": 4},
                "net_colors": None,
                "netclass_assignments": None,
                "netclass_patterns": patterns,
            },
            "pcbnew": {
                "last_paths": {
                    "gencad": "", "idf": "", "netlist": "", "plot": "",
                    "pos_files": "", "specctra_dsn": "", "step": "",
                    "svg": "", "vrml": "",
                },
                "page_layout_descr_file": "",
            },
            "schematic": {"legacy_lib_dir": "", "legacy_lib_list": []},
            "sheets": [],
            "text_variables": {},
        }


def write_board(
    design: Design,
    library,
    path: str,
    version: str = "auto",
    ground_planes: bool = True,
    write_project: bool = True,
    check: bool = True,
    indent: int = 1,
) -> BoardResult:
    """Write ``design`` to ``path`` as a ``.kicad_pcb``.

    ``version`` defaults to ``"auto"``, which matches the installed KiCad so
    the board opens without a migration prompt; pass ``"8.0"``, ``"9.0"`` or
    ``"10.0"`` to pin it.

    Raises :class:`~kicad_coder.errors.ValidationError` when the design has
    blocking problems, unless ``check`` is False.
    """
    if check:
        result = validate(design, library=library)
        if not result.ok:
            raise ValidationError(
                "cannot generate a board from an invalid design:\n%s" % result
            )

    if not path.endswith(".kicad_pcb"):
        path += ".kicad_pcb"
    path = os.path.abspath(path)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)

    writer = BoardWriter(design, library, version=version)
    node = writer.build(ground_planes=ground_planes)
    text = sexpr.dumps(node, indent=indent) + "\n"

    # A file we cannot re-read is a file KiCad will reject; catch it here
    # rather than at the user's next double-click.
    try:
        sexpr.parse(text)
    except ValueError as exc:  # pragma: no cover - defensive
        raise BackendError("generated board is not valid S-expression: %s" % exc)

    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)

    pro_path = path[: -len(".kicad_pcb")] + ".kicad_pro"
    if write_project:
        generated = writer.project_dict(os.path.basename(pro_path))
        existing = None
        if os.path.isfile(pro_path):
            try:
                with open(pro_path, "r", encoding="utf-8") as fh:
                    existing = json.load(fh)
            except (OSError, ValueError):
                existing = None  # unreadable: fall back to writing ours
        merged = _merge_project(existing, generated) if existing else generated
        with open(pro_path, "w", encoding="utf-8") as fh:
            json.dump(merged, fh, indent=2)
    else:
        pro_path = ""

    return BoardResult(
        pcb_path=path,
        pro_path=pro_path,
        net_count=len(design.nets),
        footprint_count=len(design.components),
        warnings=writer.warnings,
    )
