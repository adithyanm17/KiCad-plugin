---
name: pcb-design
description: Design, review and document printed circuit boards with KiCad. Use when the user wants to create a PCB, generate a netlist or board file, review an existing board for design problems, produce a bill of materials, run DRC, or export gerbers. Triggers on "design a PCB", "make a board", "review my PCB", "generate a BOM", "run DRC", "export gerbers", "kicad", ".kicad_pcb".
---

# PCB design with kicad_coder

Design boards by describing them, not by drawing them. You supply components,
nets and constraints; the library computes geometry, writes real KiCad files,
and reports what is wrong.

## Setup

Run this first to see what is available:

```bash
python -m kicad_coder doctor
```

Board generation, validation, review and BOM work with no KiCad installed.
DRC and fabrication export need `kicad-cli` on the system.

## The one rule that matters

**Never write geometry.** Do not emit coordinates, track paths, or pad
positions. Describe the design; call `place_components` for positions. Routing
is out of scope entirely — the boards produced here have footprints, nets and
a ground pour, and are routed afterwards in KiCad or by an autorouter.

## Workflow

### 1. Find footprints before using them

An invented footprint id fails validation and costs a turn. Always check:

```bash
python -m kicad_coder search "0603 resistor"
python -m kicad_coder search "SOIC-8"
```

### 2. Write the design as a Python script

```python
from kicad_coder.ir.types import Design, Component, BoardOutline, NetClass
from kicad_coder.library.footprints import FootprintLibrary
from kicad_coder.place.engine import place
from kicad_coder.backends.board import write_board
from kicad_coder.ir.validate import validate
from kicad_coder.review.rules import review
from kicad_coder.fab.bom import bom_from_design, write_bom

lib = FootprintLibrary()

d = Design(name="sensor", description="I2C temperature sensor")
d.outline = BoardOutline(width_mm=35, height_mm=25)
d.rules.net_classes.append(NetClass(name="Power", track_width_mm=0.5))

d.add_component(Component("U1", "TMP102", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                          mpn="TMP102AIDRLR", manufacturer="TI"))
d.add_component(Component("C1", "100n", "Capacitor_SMD:C_0603_1608Metric",
                          near=["U1"]))

d.connect("GND",  [("U1", "4"), ("C1", "2")], net_class="Power")
d.connect("+3V3", [("U1", "8"), ("C1", "1")], net_class="Power")

print(validate(d, library=lib))          # fix every error before continuing
for f in review(d, lib): print(f)
place(d, lib, seed=0)
write_board(d, lib, "build/sensor.kicad_pcb")
write_bom(bom_from_design(d), "build/sensor-bom.csv")
```

Then run it. Alternatively save the IR as JSON and use the CLI:

```bash
python -m kicad_coder build design.json -o build/board.kicad_pcb
```

### 3. Validate, then review

`validate` finds structural errors — bad footprints, invalid pad numbers,
shorted pins. These block board generation; fix all of them.

`review` finds engineering problems — missing decoupling, floating pins,
density, net class choices. These are advisory but usually worth acting on.

### 4. DRC and fabrication output

```bash
python -m kicad_coder drc build/board.kicad_pcb --json
python -m kicad_coder export build/board.kicad_pcb -o build/fab
```

Expect unconnected-item violations until the board is routed. That is normal
for an unrouted board, not a defect in the design.

## Conventions to follow without being asked

- `GND` for ground; `+3V3`, `+5V`, `VBUS` for supplies. The library detects
  power nets by name and uses that for review checks and the ground pour.
- Reference prefixes: R, C, L, D, U, Q, J, SW, Y, TP, H.
- One 100nF decoupling capacitor per IC supply pin, tagged `near=["U1"]`, plus
  a 1–10uF bulk capacitor per rail.
- A `Power` net class at 0.4–0.8 mm for supply rails.
- Mounting holes on boards over roughly 20×20 mm.
- Fill in `mpn` and `manufacturer` when you know a real part. **Never invent a
  part number** — leave it empty and say so instead.

## Reviewing an existing board

```python
from kicad_coder.fab.bom import bom_from_board
bom = bom_from_board("existing.kicad_pcb")
```

With KiCad installed, `kicad_coder.backends.pcbnew_backend.load_board_file`
pulls a full board into the IR for review.

## Honest limits — state these rather than working around them

- **No routing and no autorouter.** Export Specctra DSN and use Freerouting,
  or route by hand.
- **No schematic.** The IR is the netlist. There is no ERC and no symbol
  library.
- **No simulation.** Signal integrity, thermal behaviour and impedance are
  outside what this can see.
- **Pin functions are not known.** The library knows pad *numbers*, not what
  they do. Getting pin assignments right against a datasheet is the user's
  judgement, and you should say so when it matters.

When reviewing, ground every claim in what `review_design` actually returns.
A review that invents concerns to appear thorough is worse than a short one.
