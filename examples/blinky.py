"""End-to-end example: describe a board in the IR, then build everything.

Run it from the project root::

    python examples/blinky.py

It writes ``build/blinky.kicad_pcb``, ``build/blinky.kicad_pro`` and
``build/blinky-bom.csv``. No KiCad installation is required; if KiCad *is*
installed, the DRC step at the end runs too.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kicad_coder.backends.board import write_board
from kicad_coder.fab.bom import bom_from_design, write_bom
from kicad_coder.ir.types import BoardOutline, Component, Design, NetClass
from kicad_coder.ir.validate import validate
from kicad_coder.library.footprints import FootprintLibrary
from kicad_coder.place.engine import place
from kicad_coder.review.rules import review


def build() -> Design:
    """An ATtiny85 blinking an LED, powered from a 2-pin header."""
    d = Design(name="blinky", description="ATtiny85 LED blinker")
    d.outline = BoardOutline(width_mm=30, height_mm=20)
    d.rules.net_classes.append(
        NetClass(name="Power", track_width_mm=0.5, clearance_mm=0.25)
    )

    d.add_component(Component(
        "U1", "ATtiny85", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
        mpn="ATTINY85-20SU", manufacturer="Microchip",
        description="8-bit AVR MCU, 8KB flash"))
    d.add_component(Component(
        "C1", "100n", "Capacitor_SMD:C_0603_1608Metric",
        mpn="CL10B104KB8NNNC", manufacturer="Samsung", near=["U1"],
        description="Decoupling capacitor, X7R 50V"))
    d.add_component(Component(
        "C2", "10u", "Capacitor_SMD:C_0805_2012Metric",
        mpn="CL21A106KAYNNNE", manufacturer="Samsung",
        description="Bulk capacitor"))
    d.add_component(Component(
        "R1", "330", "Resistor_SMD:R_0603_1608Metric",
        mpn="RC0603FR-07330RL", manufacturer="Yageo",
        description="LED series resistor, 1%"))
    d.add_component(Component(
        "D1", "LED_RED", "LED_SMD:LED_0603_1608Metric",
        mpn="150060RS75000", manufacturer="Wurth"))
    d.add_component(Component(
        "J1", "PWR_IN", "Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical",
        mpn="61300211121", manufacturer="Wurth"))
    d.add_component(Component(
        "H1", "M3", "MountingHole:MountingHole_3.2mm_M3", exclude_from_bom=True))
    d.add_component(Component(
        "H2", "M3", "MountingHole:MountingHole_3.2mm_M3", exclude_from_bom=True))

    d.connect("GND", [("U1", "4"), ("C1", "2"), ("C2", "2"),
                      ("D1", "2"), ("J1", "2")], net_class="Power")
    d.connect("+5V", [("U1", "8"), ("C1", "1"), ("C2", "1"),
                      ("J1", "1")], net_class="Power")
    d.connect("LED_A", [("U1", "5"), ("R1", "1")])
    d.connect("LED_K", [("R1", "2"), ("D1", "1")])
    return d


def main() -> int:
    lib = FootprintLibrary()
    print(lib.status())
    print()

    design = build()
    print(design.summary())
    print()

    result = validate(design, library=lib)
    print(result)
    if not result.ok:
        return 1
    print()

    findings = review(design, lib)
    print("Review: %d finding(s)" % len(findings))
    for f in findings:
        print(f)
    print()

    placement = place(design, lib, seed=7)
    print(placement.summary())
    print()

    board = write_board(design, lib, "build/blinky.kicad_pcb")  # version="auto"
    print(board.summary())
    print()

    bom = bom_from_design(design)
    path = write_bom(bom, "build/blinky-bom.csv")
    print(bom.summary())
    print("  wrote %s" % path)
    print()
    print(bom.to_markdown())

    from kicad_coder.fab.toolchain import find_toolchain
    tool = find_toolchain()
    if tool.available:
        from kicad_coder.fab.drc import run_drc
        print()
        print(run_drc(board.pcb_path, toolchain=tool).summary())
    else:
        print()
        print("KiCad not installed: skipping DRC.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
