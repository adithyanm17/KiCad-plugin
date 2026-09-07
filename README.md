# kicad-coder

A Python library for designing, reviewing and documenting printed circuit
boards — usable directly, as an LLM tool set for any model provider, as a
Claude Code skill, or as a KiCad plugin.

**No required dependencies.** The core is standard library only, so it imports
cleanly inside KiCad's bundled Python. **KiCad is optional** — it is needed for
DRC and fabrication export, and for nothing else. Board generation, validation,
review and BOM all work on a machine with no KiCad installed.

```bash
python -m kicad_coder doctor      # what's available here
python examples/blinky.py         # build a real board, start to finish
python examples/agent_loop.py     # watch a scripted "model" drive the tools
python tests/test_kicad_coder.py  # 47 tests, no KiCad needed
```

## The core idea

Language models are good at circuits and bad at geometry. So the model never
emits geometry. It writes a typed intermediate representation — components,
nets, rules, intent — and deterministic code turns that into a board.

```
model  →  Design IR (validated)  →  placement solver  →  .kicad_pcb
                  ↑                                          ↓
                  └────────── review + DRC findings ←─────────┘
```

Every model output passes through schema validation before it touches a board.
That boundary is what separates this from a demo.

## Quick start

```python
from kicad_coder import Design, Component, BoardOutline, NetClass, FootprintLibrary
from kicad_coder import validate, review, place, write_board
from kicad_coder.fab.bom import bom_from_design, write_bom

lib = FootprintLibrary()

d = Design(name="sensor", description="I2C temperature sensor")
d.outline = BoardOutline(width_mm=30, height_mm=20)
d.rules.net_classes.append(NetClass(name="Power", track_width_mm=0.5))

d.add_component(Component("U1", "TMP102", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm",
                          mpn="TMP102AIDRLR", manufacturer="TI"))
d.add_component(Component("C1", "100n", "Capacitor_SMD:C_0603_1608Metric",
                          near=["U1"]))

d.connect("GND",  [("U1", "4"), ("C1", "2")], net_class="Power")
d.connect("+3V3", [("U1", "8"), ("C1", "1")], net_class="Power")

print(validate(d, library=lib))
for finding in review(d, lib):
    print(finding)

place(d, lib, seed=0)
write_board(d, lib, "build/sensor.kicad_pcb")
write_bom(bom_from_design(d), "build/sensor-bom.csv")
```

## Using it with a language model

The tool definitions are provider-neutral. No vendor SDK is imported anywhere
in the library.

```python
from kicad_coder.llm.tools import DesignSession
from kicad_coder.llm.adapters import to_anthropic, to_openai, to_gemini
from kicad_coder.llm.prompts import system_prompt

session = DesignSession(work_dir="build")

tools = to_anthropic(session.tools)   # Claude: Opus, Sonnet, Haiku, Fable
tools = to_openai(session.tools)      # OpenAI, DeepSeek, Kimi, Mistral, Groq,
                                      # Together, OpenRouter, vLLM, Ollama /v1
tools = to_gemini(session.tools)      # Google Gemini

result = session.call("add_components", {"components": [...]})
print(result.content)                 # feed straight back to the model
```

`session.call` never raises. A bad footprint id comes back as *"not found;
closest: Capacitor_SMD:C_0603_1608Metric"*, which models correct on the next
turn. That single property does more for reliability than any prompt wording.

`examples/agent_loop.py` has a working loop for each provider — about a dozen
lines each. Copy the branch you need.

Print the payloads without writing code:

```bash
python -m kicad_coder tools --provider gemini
python -m kicad_coder prompt --task review
```

### The 22 tools

| Group | Tools |
|---|---|
| Setup | `create_design`, `set_board_outline`, `set_design_rules`, `add_net_class` |
| Parts | `search_footprints`, `get_footprint`, `add_components`, `remove_component` |
| Wiring | `connect`, `remove_net` |
| Layout | `place_components`, `set_placement` |
| Checking | `validate_design`, `review_design`, `describe_design`, `add_note` |
| Output | `generate_board`, `generate_bom`, `run_drc`, `export_fab` |
| State | `save_design`, `load_design` |

## As a Claude Code skill

`skill/SKILL.md` is a complete skill definition. Copy the `skill` directory
into `.claude/skills/pcb-design/`, or point your skills path at it. It
activates on "design a PCB", "review my board", "generate a BOM", "run DRC".

## As a KiCad plugin

```bash
python plugin/install.py          # copy into KiCad's plugin folder
python plugin/install.py --link   # symlink instead, for development
python plugin/install.py --list   # show candidate directories
```

Then *Tools → External Plugins → Refresh*. Three entries appear:

- **Review board** — runs the design review against the open board
- **Generate BOM** — writes a consolidated CSV and Markdown BOM
- **Export design IR** — dumps the board as JSON for a model to work on

The plugin never modifies your board. It reads and reports.

## CLI

```bash
python -m kicad_coder doctor                       # environment report
python -m kicad_coder search "0603 resistor"       # find footprints
python -m kicad_coder validate design.json
python -m kicad_coder review design.json --json
python -m kicad_coder build design.json -o build/board.kicad_pcb
python -m kicad_coder bom board.kicad_pcb --format md
python -m kicad_coder drc board.kicad_pcb --json   # needs KiCad
python -m kicad_coder export board.kicad_pcb       # needs KiCad
python -m kicad_coder call add_components '{"components":[...]}'
```

## What it checks

**Validation** (blocks board generation): unknown footprints, pad numbers that
don't exist on the chosen footprint, pins shorted across two nets, duplicate
references, unknown net classes, impossible via geometry, tracks below the
process minimum, parts outside the outline.

**Review** (advisory, 14 checks): missing decoupling per IC supply pin, missing
bulk capacitance per rail, no ground net, ICs with many floating pins, power
rails left on the Default net class, board density against courtyard area,
2-layer boards that have outgrown the stackup, connectors with no ground,
missing mounting holes and test points, double-sided assembly cost,
auto-generated net names, DNP parts still wired in.

`review_design` also returns a structured facts block — supply rails, IC pin
counts, fanout, net classes. Hand that to a model and it writes an accurate
review instead of inventing one.

## Footprints

With KiCad installed, all of its `.pretty` libraries are indexed automatically.
Without it, 43 built-in parametric footprints cover the common cases: chip
passives 0402–1210 (R/C/L/LED/D), SOT-23, SOIC-8, 1×N and 2×N 2.54 mm headers,
test points and mounting holes. They are generated as standard `.kicad_mod`
S-expressions at IPC nominal density.

```python
FootprintLibrary(search_dirs=["/path/to/my-libs"])   # your own .pretty dirs
```

## File format notes

Boards are written as S-expressions directly. `version="auto"` (the default)
matches the installed KiCad so the board opens without a migration prompt;
pass `"8.0"`, `"9.0"` or `"10.0"` to pin it.

| Target | Format | `B.Cu` | Nets |
|---|---|---|---|
| KiCad 8 | `20240108` | 31 | `(net 1 "GND")` + numbered table |
| KiCad 9 | `20241229` | 2 | `(net 1 "GND")` + numbered table |
| KiCad 10 | `20260206` | 2 | `(net "GND")`, **no net table** |

Three details that are easy to get wrong, all verified against the KiCad
parser source and against boards KiCad itself wrote:

- **KiCad 9 renumbered the copper layers.** `B.Cu` is 2 in KiCad 9 and 31 in
  KiCad 8; inner layers are 4, 6, 8… versus 1, 2, 3…. The layer table must
  match the declared format version.
- **Pad and text angles are board-frame absolute**, while pad *positions* stay
  in unrotated footprint-local coordinates. A footprint rotated by R writes
  each pad at `local_angle + R`.
- **KiCad 10 dropped the numbered net table.** Nets now come into being from
  the names on pads, zones and tracks: `(net "GND")` rather than
  `(net 1 "GND")`. The parser still reads the old shape — its own comment
  calls it "legacy files (pre-10.0)".

An existing `.kicad_pro` is **merged**, not replaced: only the design rules and
net classes this library owns are overwritten, so component classes, tuning
profiles and your own settings survive a rebuild.

Design rules and net classes go in the `.kicad_pro` project file, which is
written alongside the board — KiCad 7+ reads them from there, so a board file
alone would be checked against defaults.

## Limits — stated plainly

- **No routing, no autorouter.** Boards come out with footprints, nets and a
  ground pour. Route in KiCad, or export Specctra DSN to Freerouting. Expect
  unconnected-item DRC violations until you do; that is not a defect.
- **No schematic.** The IR *is* the netlist. No ERC, no symbol libraries.
- **No simulation.** Signal integrity, thermal behaviour and impedance are out
  of scope.
- **Pin numbers, not pin functions.** The library knows a SOIC-8 has pads 1–8.
  It does not know pad 4 is ground on your specific part. Checking pin
  assignments against the datasheet remains a human job.
- **Zones are emitted unfilled.** Filling needs `pcbnew`; press `B` in KiCad,
  or fill via a plugin.

## Verified against KiCad 10.0.6

Both example boards were generated and then checked with the real
`kicad-cli pcb drc`. Result: **zero errors and zero warnings**, apart from the
expected `unconnected_items` — one per pin-pair that still needs routing
(9 for `blinky`, 13 for the agent-loop board). Gerbers, both drill files, the
job file and the pick-and-place CSV all export and zip correctly.

Getting there caught four real bugs that only a live KiCad could expose, all
now fixed and covered by tests:

| Bug | Symptom |
|---|---|
| `fp_circle` courtyard read as a bounding box | `end` is a point *on* the circle, so mounting holes got a zero-height box and parts were placed on top of them |
| Courtyard assumed centred on the footprint origin | A 2-pin header's courtyard sits 1.28 mm off centre → `courtyards_overlap` |
| Overlap check compared origins, not courtyard centres | False-positive overlaps on off-centre footprints |
| Library `attr` flags overwritten | Adding `smd` to a TestPoint made the board copy differ from the library → `lib_footprint_mismatch` |

Two more were caught by the 15,450-footprint library itself: search ranked
`R_0201_0603Metric` above `R_0603_1608Metric` for "0603 resistor" (an 0201
imperial part *is* 0603 metric — a model would have picked a part a third of
the intended size), and placement ignored the ~1.2 mm that reference
designators extend past the courtyard, clipping silkscreen at the board edge.

Boards are written in KiCad 9 format by default, which KiCad 10 opens and
migrates cleanly. Pass `version="8.0"` for wider backward compatibility.

## Layout

```
kicad_coder/
  ir/            typed IR + validation      types.py, validate.py
  library/       footprint discovery        footprints.py, builtin.py
  place/         placement solver           engine.py
  backends/      file emitters              sexpr.py, layers.py, board.py,
                                            pcbnew_backend.py
  fab/           KiCad toolchain            toolchain.py, drc.py, exports.py,
                                            bom.py
  review/        design review              rules.py
  llm/           tools + adapters           tools.py, adapters.py, prompts.py
  cli.py
skill/SKILL.md   Claude Code skill
plugin/          KiCad action plugin + installer
examples/        blinky.py, agent_loop.py
tests/           47 tests, no KiCad required
```

## Roadmap

The natural next steps, roughly in order of value:

1. **Specctra DSN export** → Freerouting round trip, closing the routing gap.
2. **Parts API integration** (Nexar/Octopart, DigiKey) to validate MPNs and
   pull stock, price and lifecycle into the BOM.
3. **KiCad 9 IPC API backend** (`kipy`) alongside the S-expression writer — it
   is pip-installable and version-stable, unlike the SWIG bindings.
4. **Circuit blocks**: reusable parameterised sub-circuits (LDO, USB-C sink,
   crystal, level shifter) so a model composes known-good designs.
5. **Schematic emission** (`.kicad_sch`) so the netlist round-trips into
   Eeschema and `kicad-cli sch erc` becomes available.

## Licence

MIT.
