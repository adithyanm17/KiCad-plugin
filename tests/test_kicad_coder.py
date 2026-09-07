"""Test suite. Runs with pytest, or standalone via ``python tests/test_kicad_coder.py``.

Nothing here needs KiCad installed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_coder.backends import sexpr
from kicad_coder.backends.board import write_board
from kicad_coder.backends.layers import V8, V9, flip_layer
from kicad_coder.backends.sexpr import Sym
from kicad_coder.errors import ValidationError
from kicad_coder.fab.bom import bom_from_board, bom_from_design
from kicad_coder.ir.types import (BoardOutline, Component, Design, NetClass,
                                  Placement)
from kicad_coder.ir.validate import validate
from kicad_coder.library.builtin import BUILTIN_LIBRARY
from kicad_coder.library.footprints import FootprintLibrary
from kicad_coder.llm import adapters
from kicad_coder.llm.tools import DesignSession
from kicad_coder.place.engine import place
from kicad_coder.review.rules import review

LIB = FootprintLibrary()

R0603 = "Resistor_SMD:R_0603_1608Metric"
C0603 = "Capacitor_SMD:C_0603_1608Metric"
SOIC8 = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
HDR4 = "Connector_PinHeader_2.54mm:PinHeader_1x04_P2.54mm_Vertical"


# -- s-expression ---------------------------------------------------------


def test_sexpr_roundtrip():
    src = '(a "quoted \\"x\\"" bare 1 2.5 -0.75 (b (c "d")))'
    node = sexpr.parse(src)
    assert sexpr.dumps(sexpr.parse(sexpr.dumps(node))) == sexpr.dumps(node)


def test_sexpr_preserves_quoting():
    node = sexpr.parse('(layer "F.Cu")')
    assert isinstance(node[1], str) and not isinstance(node[1], Sym)
    node2 = sexpr.parse("(layer F.Cu)")
    assert isinstance(node2[1], Sym)
    assert sexpr.dumps(node) == '(layer "F.Cu")'
    assert sexpr.dumps(node2) == "(layer F.Cu)"


def test_sexpr_numbers():
    node = sexpr.parse("(at -0.7875 0 90)")
    assert node[1] == -0.7875 and node[2] == 0 and node[3] == 90
    assert sexpr.dumps(node) == "(at -0.7875 0 90)"


def test_sexpr_unbalanced_raises():
    for bad in ["(a", "a)", "(a (b)"]:
        try:
            sexpr.parse(bad)
        except ValueError:
            continue
        raise AssertionError("expected ValueError for %r" % bad)


# -- layers ---------------------------------------------------------------


def test_layer_numbering_differs_by_version():
    assert V8.copper_id("B.Cu") == 31
    assert V9.copper_id("B.Cu") == 2
    assert V9.copper_id("In1.Cu") == 4
    assert V9.id_of("Edge.Cuts") == 25
    assert V8.id_of("Edge.Cuts") == 44


def test_copper_names():
    assert V9.copper_names(2) == ["F.Cu", "B.Cu"]
    assert V9.copper_names(4) == ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


def test_flip_layer():
    assert flip_layer("F.SilkS") == "B.SilkS"
    assert flip_layer("B.Cu") == "F.Cu"
    assert flip_layer("Edge.Cuts") == "Edge.Cuts"


# -- IR -------------------------------------------------------------------


def test_component_validation():
    for bad_ref in ["1R", "", "R", "R1A"]:
        try:
            Component(bad_ref, "10k", R0603)
        except ValueError:
            continue
        raise AssertionError("accepted bad ref %r" % bad_ref)
    try:
        Component("R1", "10k", "NoColon")
    except ValueError:
        pass
    else:
        raise AssertionError("accepted footprint without a library prefix")


def test_power_net_detection():
    from kicad_coder.ir.types import Net
    assert Net("GND", [("R1", "1")]).is_ground()
    assert Net("+3V3", [("R1", "1")]).is_power()
    assert Net("VBUS", [("R1", "1")]).is_power()
    assert not Net("SDA", [("R1", "1")]).is_power()


def test_design_roundtrip():
    d = _demo_design()
    assert Design.from_dict(d.to_dict()).to_dict() == d.to_dict()


def test_next_ref():
    d = Design()
    d.add_component(Component("R1", "1k", R0603))
    d.add_component(Component("R3", "1k", R0603))
    assert d.next_ref("R") == "R2"
    assert d.next_ref("C") == "C1"


def test_remove_component_cleans_nets():
    d = _demo_design()
    d.remove_component("R1")
    assert d.component("R1") is None
    assert all("R1" not in n.refs for n in d.nets)


# -- validation -----------------------------------------------------------


def _demo_design() -> Design:
    d = Design(name="demo", description="test board")
    d.outline = BoardOutline(width_mm=40, height_mm=30)
    d.add_component(Component("U1", "MCU", SOIC8, mpn="X1"))
    d.add_component(Component("C1", "100n", C0603, near=["U1"]))
    d.add_component(Component("R1", "10k", R0603))
    d.add_component(Component("J1", "CONN", HDR4))
    d.connect("GND", [("U1", "4"), ("C1", "2"), ("J1", "4")])
    d.connect("+3V3", [("U1", "8"), ("C1", "1"), ("J1", "1"), ("R1", "1")])
    d.connect("SDA", [("U1", "1"), ("R1", "2"), ("J1", "2")])
    return d


def test_valid_design_passes():
    assert validate(_demo_design(), library=LIB).ok


def test_unknown_pad_is_error():
    d = _demo_design()
    d.connect("BAD", [("U1", "99"), ("R1", "2")])
    res = validate(d, library=LIB)
    assert not res.ok
    assert any(i.code == "unknown_pad" for i in res.errors)


def test_unknown_footprint_is_error():
    d = _demo_design()
    d.add_component(Component("C9", "1u", "Nope:Nope"))
    res = validate(d, library=LIB)
    assert any(i.code == "unknown_footprint" for i in res.errors)


def test_shorted_pin_is_error():
    d = _demo_design()
    d.connect("OTHER", [("U1", "4"), ("R1", "2")])
    res = validate(d, library=LIB)
    assert any(i.code == "pin_short" for i in res.errors)


def test_duplicate_ref_is_error():
    d = _demo_design()
    d.components.append(Component("R1", "1k", R0603))
    assert any(i.code == "duplicate_ref" for i in validate(d).errors)


def test_bad_via_geometry_is_error():
    d = _demo_design()
    d.rules.min_via_drill_mm = 0.8
    d.rules.min_via_diameter_mm = 0.6
    assert any(i.code == "bad_via" for i in validate(d).errors)


# -- review ---------------------------------------------------------------


def test_missing_decoupling_flagged_when_no_caps():
    d = Design(name="x")
    d.add_component(Component("U1", "MCU", SOIC8))
    d.connect("GND", [("U1", "4")])
    d.connect("+3V3", [("U1", "8"), ("U1", "1")])
    assert any(f.code == "missing_decoupling" for f in review(d, LIB))


def test_decoupling_satisfied_by_proper_cap():
    d = Design(name="x")
    d.add_component(Component("U1", "MCU", SOIC8))
    d.add_component(Component("C1", "100n", C0603))
    d.connect("GND", [("U1", "4"), ("C1", "2")])
    d.connect("+3V3", [("U1", "8"), ("C1", "1")])
    assert not any(f.code == "missing_decoupling" for f in review(d, LIB))


def test_no_ground_flagged():
    d = Design(name="x")
    d.add_component(Component("R1", "1k", R0603))
    d.add_component(Component("R2", "1k", R0603))
    d.connect("SIG", [("R1", "1"), ("R2", "1")])
    assert any(f.code == "no_ground" for f in review(d, LIB))


def test_overfull_board_flagged():
    d = _demo_design()
    d.outline = BoardOutline(width_mm=6, height_mm=6)
    assert any(f.code == "board_too_small" for f in review(d, LIB))


def test_cap_value_parsing():
    from kicad_coder.review.rules import _cap_value_farads
    assert abs(_cap_value_farads("100n") - 1e-7) < 1e-12
    assert abs(_cap_value_farads("4u7") - 4.7e-6) < 1e-12
    assert abs(_cap_value_farads("10uF") - 1e-5) < 1e-11
    assert _cap_value_farads("not a cap") == 0.0


# -- placement ------------------------------------------------------------


def test_placement_has_no_overlaps():
    d = _demo_design()
    res = place(d, LIB, seed=1)
    assert res.ok, res.summary()
    assert len(d.placements) == len(d.components)


def test_placement_is_deterministic():
    a, b = _demo_design(), _demo_design()
    place(a, LIB, seed=42)
    place(b, LIB, seed=42)
    assert {k: (v.x_mm, v.y_mm) for k, v in a.placements.items()} == \
           {k: (v.x_mm, v.y_mm) for k, v in b.placements.items()}


def test_placement_respects_lock():
    d = _demo_design()
    d.placements["J1"] = Placement("J1", 5.0, 5.0, locked=True)
    place(d, LIB, seed=1)
    assert (d.placements["J1"].x_mm, d.placements["J1"].y_mm) == (5.0, 5.0)


def test_placement_reports_impossible_board():
    d = _demo_design()
    d.outline = BoardOutline(width_mm=4, height_mm=4)
    res = place(d, LIB, seed=1)
    assert not res.ok and res.outside


# -- board generation -----------------------------------------------------


def test_board_generation_structure():
    d = _demo_design()
    place(d, LIB, seed=1)
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "b.kicad_pcb")
        res = write_board(d, LIB, out, version="9.0")
        assert os.path.isfile(res.pcb_path) and os.path.isfile(res.pro_path)

        board = sexpr.parse(open(res.pcb_path, encoding="utf-8").read())
        assert str(board[0]) == "kicad_pcb"
        assert sexpr.get(board, "version") == 20241229
        assert len(sexpr.find_all(board, "footprint")) == 4
        assert len(sexpr.find_all(board, "gr_line")) == 4  # rectangular outline

        nets = sexpr.find_all(board, "net")
        codes = [n[1] for n in nets if len(n) > 1 and isinstance(n[1], int)]
        assert 0 in codes and len(codes) == len(d.nets) + 1

        pro = json.load(open(res.pro_path, encoding="utf-8"))
        assert pro["board"]["design_settings"]["rules"]["min_track_width"] == \
            d.rules.min_track_mm


def test_board_pad_nets_are_assigned():
    d = _demo_design()
    place(d, LIB, seed=1)
    with tempfile.TemporaryDirectory() as tmp:
        res = write_board(d, LIB, os.path.join(tmp, "b.kicad_pcb"))
        board = sexpr.parse(open(res.pcb_path, encoding="utf-8").read())
        for fp in sexpr.find_all(board, "footprint"):
            ref = next(p[2] for p in sexpr.find_all(fp, "property")
                       if str(p[1]) == "Reference")
            expected = d.pads_of(ref)
            actual = {}
            for pad in sexpr.find_all(fp, "pad"):
                net = sexpr.find(pad, "net")
                if net is not None:
                    actual[sexpr.sexp_str(pad[1])] = sexpr.sexp_str(net[2])
            assert actual == expected, ref


def test_bottom_side_is_fully_flipped():
    d = _demo_design()
    d.component("R1").side = "bottom"
    place(d, LIB, seed=1)
    with tempfile.TemporaryDirectory() as tmp:
        res = write_board(d, LIB, os.path.join(tmp, "b.kicad_pcb"))
        board = sexpr.parse(open(res.pcb_path, encoding="utf-8").read())
        fp = next(f for f in sexpr.find_all(board, "footprint")
                  if any(str(p[1]) == "Reference" and p[2] == "R1"
                         for p in sexpr.find_all(f, "property")))
        assert sexpr.get(fp, "layer") == "B.Cu"
        for pad in sexpr.find_all(fp, "pad"):
            layers = [sexpr.sexp_str(x) for x in sexpr.find(pad, "layers")[1:]]
            assert not any(l.startswith("F.") for l in layers), layers


def test_rotation_is_board_frame_absolute():
    d = _demo_design()
    place(d, LIB, seed=1)
    d.placements["U1"] = Placement("U1", 20, 15, rotation_deg=90)
    with tempfile.TemporaryDirectory() as tmp:
        res = write_board(d, LIB, os.path.join(tmp, "b.kicad_pcb"))
        board = sexpr.parse(open(res.pcb_path, encoding="utf-8").read())
        fp = next(f for f in sexpr.find_all(board, "footprint")
                  if any(str(p[1]) == "Reference" and p[2] == "U1"
                         for p in sexpr.find_all(f, "property")))
        assert sexpr.find(fp, "at")[3] == 90
        for pad in sexpr.find_all(fp, "pad"):
            assert sexpr.find(pad, "at")[3] == 90


def test_invalid_design_refuses_to_build():
    d = _demo_design()
    d.connect("BAD", [("U1", "99"), ("R1", "2")])
    with tempfile.TemporaryDirectory() as tmp:
        try:
            write_board(d, LIB, os.path.join(tmp, "b.kicad_pcb"))
        except ValidationError:
            return
    raise AssertionError("an invalid design was allowed to generate a board")


def test_v8_and_v9_both_generate():
    d = _demo_design()
    place(d, LIB, seed=1)
    with tempfile.TemporaryDirectory() as tmp:
        for version, expected in (("8.0", 20240108), ("9.0", 20241229)):
            res = write_board(d, LIB, os.path.join(tmp, version + ".kicad_pcb"),
                              version=version)
            board = sexpr.parse(open(res.pcb_path, encoding="utf-8").read())
            assert sexpr.get(board, "version") == expected
            b_cu = [row for row in sexpr.find(board, "layers")[1:]
                    if row[1] == "B.Cu"][0]
            assert b_cu[0] == (31 if version == "8.0" else 2)


# -- footprint library ----------------------------------------------------


def test_builtin_footprints_all_parse():
    for libid, text in BUILTIN_LIBRARY.items():
        node = sexpr.parse(text)
        assert str(node[0]) == "footprint", libid
        assert sexpr.find_all(node, "pad"), libid


def test_search_ranks_exact_match_first():
    assert LIB.search("0603 resistor")[0] == R0603
    assert LIB.search("SOIC-8")[0] == SOIC8


def test_missing_footprint_suggests_alternatives():
    from kicad_coder.errors import LibraryError
    try:
        LIB.get("Resistor_SMD:R_0603_WRONG")
    except LibraryError as exc:
        assert "Did you mean" in str(exc)
        return
    raise AssertionError("expected LibraryError")


def test_pad_numbers_and_courtyard():
    fp = LIB.get(SOIC8)
    assert fp.pad_numbers == ["1", "2", "3", "4", "5", "6", "7", "8"]
    x0, y0, x1, y1 = fp.courtyard_bbox()
    assert x1 > x0 and y1 > y0


# -- BOM ------------------------------------------------------------------


def test_bom_consolidates_identical_parts():
    d = Design(name="x")
    for i in range(1, 4):
        d.add_component(Component("R%d" % i, "10k", R0603, mpn="RC0603-10K"))
    d.add_component(Component("R4", "1k", R0603, mpn="RC0603-1K"))
    bom = bom_from_design(d)
    assert len(bom.lines) == 2
    assert bom.lines[0].quantity == 3
    assert bom.total_parts == 4


def test_bom_excludes_marked_parts():
    d = Design(name="x")
    d.add_component(Component("R1", "10k", R0603))
    d.add_component(Component("H1", "M3", "MountingHole:MountingHole_3.2mm_M3",
                              exclude_from_bom=True))
    assert len(bom_from_design(d).lines) == 1


def test_bom_reports_missing_mpn():
    d = Design(name="x")
    d.add_component(Component("R1", "10k", R0603))
    assert bom_from_design(d).missing_mpn() == ["R1"]


def test_bom_from_board_matches_bom_from_design():
    d = _demo_design()
    place(d, LIB, seed=1)
    with tempfile.TemporaryDirectory() as tmp:
        res = write_board(d, LIB, os.path.join(tmp, "b.kicad_pcb"))
        a = bom_from_design(d)
        b = bom_from_board(res.pcb_path)
        refs_a = sorted(r for l in a.lines for r in l.references)
        refs_b = sorted(r for l in b.lines for r in l.references)
        assert refs_a == refs_b
        assert {l.mpn for l in a.lines} == {l.mpn for l in b.lines}


# -- LLM layer ------------------------------------------------------------


def test_all_providers_produce_payloads():
    session = DesignSession()
    for provider in ("openai", "anthropic", "gemini", "bedrock", "ollama",
                     "deepseek", "kimi", "mistral"):
        payload = adapters.for_provider(provider, session.tools)
        assert payload


def test_gemini_schema_has_no_forbidden_keys():
    session = DesignSession()
    decls = adapters.to_gemini(session.tools)[0]["function_declarations"]

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                assert k not in ("additionalProperties", "$schema", "const")
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(decls)
    assert len(decls) == len(session.tools)


def test_unknown_tool_returns_guidance_not_exception():
    r = DesignSession().call("nope", {})
    assert not r.ok and r.error_code == "unknown_tool"
    assert "Available tools" in r.content


def test_bad_footprint_gives_actionable_error():
    s = DesignSession()
    s.call("create_design", {"name": "t"})
    r = s.call("add_components", {"components": [
        {"ref": "R1", "value": "1k", "footprint": "Resistor_SMD:R_0603_WRONG"}]})
    assert "closest" in r.content.lower()


def test_full_session_flow():
    with tempfile.TemporaryDirectory() as tmp:
        s = DesignSession(work_dir=tmp)
        assert s.call("create_design", {"name": "t", "width_mm": 40,
                                        "height_mm": 30}).ok
        assert s.call("add_components", {"components": [
            {"ref": "U1", "value": "MCU", "footprint": SOIC8},
            {"ref": "C1", "value": "100n", "footprint": C0603, "near": ["U1"]},
        ]}).ok
        assert s.call("connect", {"nets": [
            {"name": "GND", "connections": [{"ref": "U1", "pad": "4"},
                                            {"ref": "C1", "pad": "2"}]},
            {"name": "+3V3", "connections": [{"ref": "U1", "pad": "8"},
                                             {"ref": "C1", "pad": "1"}]},
        ]}).ok
        assert s.call("validate_design", {}).ok
        assert s.call("place_components", {"seed": 1}).ok
        gen = s.call("generate_board", {})
        assert gen.ok and os.path.isfile(gen.data["pcb_path"])
        bom = s.call("generate_bom", {"format": "csv"})
        assert bom.ok and os.path.isfile(bom.data["path"])
        assert s.call("review_design", {}).ok


def test_drc_without_kicad_is_graceful():
    from kicad_coder.fab.toolchain import Toolchain
    s = DesignSession(toolchain=Toolchain())
    r = s.call("run_drc", {})
    assert not r.ok and r.error_code in ("no_toolchain", "missing_board")


# -- runner ---------------------------------------------------------------


def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    failed = []
    for name, fn in tests:
        try:
            fn()
            print("  ok    %s" % name)
        except Exception as exc:
            failed.append((name, exc))
            print("  FAIL  %s: %s" % (name, exc))
    print()
    print("%d passed, %d failed, %d total"
          % (len(tests) - len(failed), len(failed), len(tests)))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
