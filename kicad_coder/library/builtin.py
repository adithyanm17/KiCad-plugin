"""Parametric footprints generated in pure Python.

KiCad ships thousands of excellent footprints, and when it is installed
:mod:`kicad_coder.library.footprints` uses those. But the library must also be
usable with no KiCad present -- for CI, for a container, for a first run before
anyone has installed anything. These generators cover the handful of packages
that appear in almost every design so a model can produce a real board on day
one.

Everything here emits standard ``.kicad_mod`` S-expression text, so the results
open in KiCad and can be copied into a ``.pretty`` directory unchanged.

Dimensions follow IPC-7351 "nominal" density (density level B).
"""

from __future__ import annotations

from typing import Dict, List, Tuple

__all__ = ["BUILTIN_LIBRARY", "generate", "list_builtin"]

# name -> (pad_w, pad_h, pad_gap_centre, body_w, body_h)
# pad_gap_centre is the centre-to-centre pad spacing in mm.
_CHIP: Dict[str, Tuple[float, float, float, float, float]] = {
    "0402": (0.60, 0.62, 0.94, 1.00, 0.50),
    "0603": (0.90, 0.95, 1.60, 1.60, 0.80),
    "0805": (1.05, 1.30, 1.90, 2.00, 1.25),
    "1206": (1.15, 1.80, 3.00, 3.20, 1.60),
    "1210": (1.15, 2.70, 3.00, 3.20, 2.50),
}

_CHIP_PREFIX = {
    "R": ("Resistor_SMD", "R"),
    "C": ("Capacitor_SMD", "C"),
    "L": ("Inductor_SMD", "L"),
    "LED": ("LED_SMD", "LED"),
    "D": ("Diode_SMD", "D"),
}

_IMPERIAL_TO_METRIC = {
    "0402": "1005Metric",
    "0603": "1608Metric",
    "0805": "2012Metric",
    "1206": "3216Metric",
    "1210": "3225Metric",
}


def _f(v: float) -> str:
    s = ("%.4f" % v).rstrip("0").rstrip(".")
    return s if s not in ("", "-") else "0"


def _silk_line(x1: float, y1: float, x2: float, y2: float,
               layer: str = "F.SilkS", width: float = 0.12) -> str:
    return (
        '(fp_line (start %s %s) (end %s %s) (stroke (width %s) (type solid)) '
        '(layer "%s"))' % (_f(x1), _f(y1), _f(x2), _f(y2), _f(width), layer)
    )


def _courtyard_rect(hw: float, hh: float) -> str:
    return (
        '(fp_rect (start %s %s) (end %s %s) (stroke (width 0.05) (type solid)) '
        '(fill none) (layer "F.CrtYd"))' % (_f(-hw), _f(-hh), _f(hw), _f(hh))
    )


def _text_items(ref_y: float, val_y: float) -> str:
    return (
        '(fp_text reference "REF**" (at 0 %s) (layer "F.SilkS")\n'
        '    (effects (font (size 0.8 0.8) (thickness 0.12)))\n'
        '  )\n'
        '  (fp_text value "VAL**" (at 0 %s) (layer "F.Fab")\n'
        '    (effects (font (size 0.8 0.8) (thickness 0.12)))\n'
        '  )' % (_f(ref_y), _f(val_y))
    )


def _chip(kind: str, size: str) -> str:
    pad_w, pad_h, pitch, body_w, body_h = _CHIP[size]
    lib, prefix = _CHIP_PREFIX[kind]
    name = "%s_%s_%s" % (prefix, size, _IMPERIAL_TO_METRIC[size])
    dx = pitch / 2.0
    hw = dx + pad_w / 2.0 + 0.25
    hh = max(pad_h, body_h) / 2.0 + 0.25
    silk_y = max(pad_h, body_h) / 2.0 + 0.15
    silk_x = body_w / 2.0

    pads = []
    for num, x in (("1", -dx), ("2", dx)):
        pads.append(
            '(pad "%s" smd roundrect (at %s 0) (size %s %s) '
            '(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))'
            % (num, _f(x), _f(pad_w), _f(pad_h))
        )

    silk = []
    if silk_x > dx - pad_w / 2.0:
        silk.append(_silk_line(-silk_x, -silk_y, silk_x, -silk_y))
        silk.append(_silk_line(-silk_x, silk_y, silk_x, silk_y))

    body = []
    if kind in ("LED", "D"):
        # cathode bar on Fab so orientation survives assembly review
        body.append(
            '(fp_line (start %s %s) (end %s %s) (stroke (width 0.1) '
            '(type solid)) (layer "F.Fab"))'
            % (_f(body_w / 2.0 - 0.2), _f(-body_h / 2.0),
               _f(body_w / 2.0 - 0.2), _f(body_h / 2.0))
        )

    return _wrap(
        name,
        "%s %s chip package, hand-solder friendly (IPC nominal)" % (prefix, size),
        "%s %s" % (prefix, size),
        pads + silk + body + [_courtyard_rect(hw, hh), _rect_fab(body_w, body_h)],
        ref_y=-(hh + 0.6),
        val_y=(hh + 0.6),
    ), lib


def _rect_fab(w: float, h: float) -> str:
    return (
        '(fp_rect (start %s %s) (end %s %s) (stroke (width 0.1) (type solid)) '
        '(fill none) (layer "F.Fab"))'
        % (_f(-w / 2), _f(-h / 2), _f(w / 2), _f(h / 2))
    )


def _sot23() -> Tuple[str, str]:
    pad_w, pad_h = 1.06, 0.65
    col_x, row_y = 1.0, 0.95
    positions = {"1": (-col_x, row_y), "2": (-col_x, -row_y), "3": (col_x, 0.0)}
    pads = [
        '(pad "%s" smd roundrect (at %s %s) (size %s %s) '
        '(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))'
        % (n, _f(x), _f(y), _f(pad_w), _f(pad_h))
        for n, (x, y) in positions.items()
    ]
    hw, hh = col_x + pad_w / 2 + 0.25, row_y + pad_h / 2 + 0.25
    extras = [
        _courtyard_rect(hw, hh),
        _rect_fab(1.3, 3.0),
        _silk_line(-0.75, -1.6, 0.75, -1.6),
        _silk_line(-0.75, 1.6, 0.75, 1.6),
    ]
    return _wrap("SOT-23", "SOT-23, 3 pin", "SOT-23",
                 pads + extras, ref_y=-(hh + 0.6), val_y=(hh + 0.6)), "Package_TO_SOT_SMD"


def _soic8() -> Tuple[str, str]:
    pitch, pad_w, pad_h, col_x = 1.27, 1.95, 0.6, 2.7
    pads: List[str] = []
    for i in range(4):
        y = (1.5 - i) * pitch
        pads.append(
            '(pad "%d" smd roundrect (at %s %s) (size %s %s) '
            '(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))'
            % (i + 1, _f(-col_x), _f(y), _f(pad_w), _f(pad_h))
        )
    for i in range(4):
        y = (i - 1.5) * pitch
        pads.append(
            '(pad "%d" smd roundrect (at %s %s) (size %s %s) '
            '(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25))'
            % (i + 5, _f(col_x), _f(y), _f(pad_w), _f(pad_h))
        )
    hw, hh = col_x + pad_w / 2 + 0.25, 2.55
    extras = [
        _courtyard_rect(hw, hh),
        _rect_fab(3.9, 4.9),
        # pin-1 dot
        '(fp_circle (center -2.6 2.9) (end -2.45 2.9) (stroke (width 0.2) '
        '(type solid)) (fill solid) (layer "F.SilkS"))',
        _silk_line(-1.95, -2.45, 1.95, -2.45),
        _silk_line(-1.95, 2.45, 1.95, 2.45),
    ]
    return _wrap("SOIC-8_3.9x4.9mm_P1.27mm", "8-lead SOIC, 1.27mm pitch",
                 "SOIC 8", pads + extras,
                 ref_y=-(hh + 0.6), val_y=(hh + 0.6)), "Package_SO"


def _header(n: int, rows: int = 1, pitch: float = 2.54) -> Tuple[str, str]:
    drill, pad_d = 1.0, 1.7
    pads: List[str] = []
    idx = 1
    y0 = -(n - 1) * pitch / 2.0
    for i in range(n):
        for r in range(rows):
            x = r * pitch - (rows - 1) * pitch / 2.0
            y = y0 + i * pitch
            shape = "rect" if idx == 1 else "circle"
            pads.append(
                '(pad "%d" thru_hole %s (at %s %s) (size %s %s) '
                '(drill %s) (layers "*.Cu" "*.Mask"))'
                % (idx, shape, _f(x), _f(y), _f(pad_d), _f(pad_d), _f(drill))
            )
            idx += 1
    hw = (rows - 1) * pitch / 2.0 + pad_d / 2.0 + 0.25
    hh = (n - 1) * pitch / 2.0 + pad_d / 2.0 + 0.25
    name = "PinHeader_%dx%02d_P2.54mm_Vertical" % (rows, n)
    extras = [_courtyard_rect(hw, hh), _rect_fab(rows * pitch, n * pitch)]
    return _wrap(name, "Through-hole pin header, %dx%d, 2.54mm pitch" % (rows, n),
                 "pin header", pads + extras,
                 ref_y=-(hh + 0.6), val_y=(hh + 0.6)), "Connector_PinHeader_2.54mm"


def _testpoint() -> Tuple[str, str]:
    pads = ['(pad "1" smd circle (at 0 0) (size 1.5 1.5) '
            '(layers "F.Cu" "F.Mask"))']
    extras = [_courtyard_rect(1.0, 1.0)]
    return _wrap("TestPoint_Pad_D1.5mm", "SMD test point, 1.5mm pad", "test point",
                 pads + extras, ref_y=-1.6, val_y=1.6), "TestPoint"


def _mounting_hole(d: float = 3.2) -> Tuple[str, str]:
    pads = ['(pad "" np_thru_hole circle (at 0 0) (size %s %s) (drill %s) '
            '(layers "F.Cu" "B.Cu" "*.Mask"))' % (_f(d), _f(d), _f(d))]
    r = d / 2.0 + 0.5
    extras = [_courtyard_rect(r, r)]
    # Name matches KiCad's own library so a design stays portable between the
    # built-in fallback and a real KiCad install.
    return _wrap("MountingHole_%smm_M3" % _f(d),
                 "Non-plated mounting hole, %smm, for M3" % _f(d),
                 "mounting hole", pads + extras,
                 ref_y=-(r + 0.6), val_y=(r + 0.6)), "MountingHole"


def _wrap(name: str, descr: str, tags: str, items: List[str],
          ref_y: float, val_y: float) -> str:
    body = "\n  ".join(items)
    return (
        '(footprint "%s"\n'
        '  (version 20240108)\n'
        '  (generator "kicad_coder")\n'
        '  (layer "F.Cu")\n'
        '  (descr "%s")\n'
        '  (tags "%s")\n'
        '  %s\n'
        '  %s\n'
        ')\n' % (name, descr, tags, _text_items(ref_y, val_y), body)
    )


def _build_library() -> Dict[str, str]:
    """Return ``{"Lib:Name": kicad_mod_text}`` for every built-in footprint."""
    out: Dict[str, str] = {}

    for kind in ("R", "C", "L", "LED", "D"):
        for size in _CHIP:
            text, lib = _chip(kind, size)
            name = text.split('"', 2)[1]
            out["%s:%s" % (lib, name)] = text

    for maker in (_sot23, _soic8, _testpoint, _mounting_hole):
        text, lib = maker()
        out["%s:%s" % (lib, text.split('"', 2)[1])] = text

    for n in (2, 3, 4, 5, 6, 8, 10):
        for rows in (1, 2):
            text, lib = _header(n, rows)
            out["%s:%s" % (lib, text.split('"', 2)[1])] = text

    return out


BUILTIN_LIBRARY: Dict[str, str] = _build_library()


def generate(libid: str) -> str:
    """Return ``.kicad_mod`` text for a built-in footprint id."""
    if libid not in BUILTIN_LIBRARY:
        raise KeyError(libid)
    return BUILTIN_LIBRARY[libid]


def list_builtin() -> List[str]:
    return sorted(BUILTIN_LIBRARY)
