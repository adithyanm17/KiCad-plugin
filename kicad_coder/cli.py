"""Command line interface: ``python -m kicad_coder``.

Covers the things you want without writing a script -- checking the
environment, searching footprints, and running the build/validate/review/BOM
steps against a saved design IR.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from .backends.board import write_board
from .errors import KiCadCoderError
from .fab.bom import bom_from_board, bom_from_design, write_bom
from .fab.toolchain import cli_capabilities, find_toolchain
from .ir.types import Design
from .ir.validate import validate
from .library.footprints import FootprintLibrary
from .llm.adapters import PROVIDERS, for_provider
from .llm.tools import DesignSession
from .place.engine import place
from .review.rules import review, review_context


def _load(path: str) -> Design:
    with open(path, "r", encoding="utf-8") as fh:
        return Design.from_dict(json.load(fh))


def cmd_doctor(args) -> int:
    tool = find_toolchain(kicad_cli=args.kicad_cli or None)
    lib = FootprintLibrary(search_dirs=args.footprint_dir or (), toolchain=tool)
    print("kicad_coder environment")
    print("  python: %s" % sys.version.split()[0])
    print("  %s" % tool.describe().replace("\n", "\n  "))
    if tool.available:
        caps = cli_capabilities(tool)
        print("  kicad-cli features: %s" % ", ".join(
            "%s=%s" % (k, "yes" if v else "no") for k, v in caps.items()))
    print("  %s" % lib.status().replace("\n", "\n  "))
    try:
        import pcbnew  # noqa: F401
        print("  pcbnew module: importable (running inside KiCad's Python)")
    except ImportError:
        print("  pcbnew module: not importable (fine -- not required)")
    if not tool.available:
        print()
        print("  Board generation, validation, review and BOM all work now.")
        print("  DRC and fabrication export need KiCad installed.")
    return 0


def cmd_search(args) -> int:
    lib = FootprintLibrary(search_dirs=args.footprint_dir or ())
    hits = lib.search(args.query, limit=args.limit, library=args.library or "")
    if not hits:
        print("no matches for %r among %d footprints"
              % (args.query, len(lib.libids())))
        return 1
    for libid in hits:
        info = lib.describe(libid)
        print("%-58s %2d pads  %.1fx%.1f mm"
              % (libid, info["pad_count"], info["size_mm"][0], info["size_mm"][1]))
    return 0


def cmd_validate(args) -> int:
    design = _load(args.design)
    lib = FootprintLibrary(search_dirs=args.footprint_dir or ())
    res = validate(design, library=lib, strict=args.strict)
    print(res)
    return 0 if res.ok else 1


def cmd_review(args) -> int:
    design = _load(args.design)
    lib = FootprintLibrary(search_dirs=args.footprint_dir or ())
    findings = review(design, lib)
    if args.json:
        print(json.dumps({
            "findings": [f.to_dict() for f in findings],
            "context": review_context(design, lib),
        }, indent=2))
        return 0
    print("%d finding(s)" % len(findings))
    for f in findings:
        print(f)
    return 0


def cmd_build(args) -> int:
    design = _load(args.design)
    lib = FootprintLibrary(search_dirs=args.footprint_dir or ())

    res = validate(design, library=lib)
    if not res.ok:
        print(res)
        return 1

    placement = place(design, lib, strategy=args.strategy, seed=args.seed)
    print(placement.summary())

    out = args.output or os.path.join("build", design.name + ".kicad_pcb")
    board = write_board(design, lib, out, version=args.kicad_version)
    print(board.summary())

    bom = bom_from_design(design)
    bom_path = os.path.splitext(out)[0] + "-bom.csv"
    write_bom(bom, bom_path)
    print(bom.summary())
    print("  wrote %s" % bom_path)
    return 0


def cmd_bom(args) -> int:
    if args.source.endswith(".kicad_pcb"):
        bom = bom_from_board(args.source, include_dnp=not args.no_dnp)
    else:
        bom = bom_from_design(_load(args.source), include_dnp=not args.no_dnp)
    if args.output:
        write_bom(bom, args.output, fmt=args.format)
        print(bom.summary())
        print("  wrote %s" % args.output)
    else:
        print({"csv": bom.to_csv, "json": bom.to_json,
               "md": bom.to_markdown}[args.format]())
    return 0


def cmd_drc(args) -> int:
    from .fab.drc import run_drc
    tool = find_toolchain(kicad_cli=args.kicad_cli or None)
    if not tool.available:
        print(tool.describe())
        print("DRC requires KiCad. Install it or pass --kicad-cli.")
        return 2
    res = run_drc(args.board, toolchain=tool, schematic_parity=args.parity)
    if args.json:
        print(json.dumps(res.to_dict(), indent=2))
    else:
        print(res.summary())
    return 0 if res.ok else 1


def cmd_export(args) -> int:
    from .fab.exports import export_fab_package
    tool = find_toolchain(kicad_cli=args.kicad_cli or None)
    if not tool.available:
        print("Fabrication export requires KiCad.")
        return 2
    res = export_fab_package(args.board, args.output,
                             copper_layers=args.copper_layers, toolchain=tool)
    print(res.summary())
    return 0


def cmd_tools(args) -> int:
    session = DesignSession()
    payload = for_provider(args.provider, session.tools)
    print(json.dumps(payload, indent=2))
    return 0


def cmd_prompt(args) -> int:
    from .llm.prompts import system_prompt
    print(system_prompt(args.task))
    return 0


def cmd_call(args) -> int:
    """Execute one tool call from JSON -- useful for scripting and testing."""
    session = DesignSession(work_dir=args.work_dir)
    if args.design and os.path.isfile(args.design):
        session.design = _load(args.design)
    try:
        arguments = json.loads(args.arguments) if args.arguments else {}
    except ValueError as exc:
        print("arguments must be valid JSON: %s" % exc)
        return 2
    result = session.call(args.tool, arguments)
    print(result.content)
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="kicad_coder",
        description="Design, review and document PCBs from code or from an LLM.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    def add_lib_args(sp):
        sp.add_argument("--footprint-dir", action="append", default=[],
                        help="extra directory containing .pretty libraries "
                             "(repeatable)")

    d = sub.add_parser("doctor", help="report the detected environment")
    d.add_argument("--kicad-cli", default="", help="explicit kicad-cli path")
    add_lib_args(d)
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("search", help="search footprint libraries")
    s.add_argument("query")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--library", default="")
    add_lib_args(s)
    s.set_defaults(func=cmd_search)

    v = sub.add_parser("validate", help="validate a design IR JSON file")
    v.add_argument("design")
    v.add_argument("--strict", action="store_true")
    add_lib_args(v)
    v.set_defaults(func=cmd_validate)

    r = sub.add_parser("review", help="run the engineering review")
    r.add_argument("design")
    r.add_argument("--json", action="store_true")
    add_lib_args(r)
    r.set_defaults(func=cmd_review)

    b = sub.add_parser("build", help="place, generate the board, write the BOM")
    b.add_argument("design")
    b.add_argument("-o", "--output", default="")
    b.add_argument("--strategy", choices=["force", "grid"], default="force")
    b.add_argument("--seed", type=int, default=0)
    b.add_argument("--kicad-version", choices=["8.0", "9.0", "10.0", "auto"], default="auto")
    add_lib_args(b)
    b.set_defaults(func=cmd_build)

    m = sub.add_parser("bom", help="generate a BOM from an IR file or a board")
    m.add_argument("source", help="design .json or board .kicad_pcb")
    m.add_argument("-o", "--output", default="")
    m.add_argument("--format", choices=["csv", "json", "md"], default="csv")
    m.add_argument("--no-dnp", action="store_true")
    m.set_defaults(func=cmd_bom)

    c = sub.add_parser("drc", help="run KiCad DRC on a board")
    c.add_argument("board")
    c.add_argument("--json", action="store_true")
    c.add_argument("--parity", action="store_true",
                   help="also check schematic parity")
    c.add_argument("--kicad-cli", default="")
    c.set_defaults(func=cmd_drc)

    e = sub.add_parser("export", help="export gerbers, drill and placement")
    e.add_argument("board")
    e.add_argument("-o", "--output", default="fab")
    e.add_argument("--copper-layers", type=int, default=2)
    e.add_argument("--kicad-cli", default="")
    e.set_defaults(func=cmd_export)

    t = sub.add_parser("tools", help="print tool definitions for a provider")
    t.add_argument("--provider", default="openai",
                   help="openai, anthropic, gemini, bedrock, ollama, or any "
                        "OpenAI-compatible vendor name (deepseek, kimi, ...)")
    t.set_defaults(func=cmd_tools)

    pr = sub.add_parser("prompt", help="print the system prompt")
    pr.add_argument("--task", choices=["design", "review", "bom"],
                    default="design")
    pr.set_defaults(func=cmd_prompt)

    cl = sub.add_parser("call", help="execute one tool call")
    cl.add_argument("tool")
    cl.add_argument("arguments", nargs="?", default="{}",
                    help="JSON object of arguments")
    cl.add_argument("--design", default="", help="design IR to load first")
    cl.add_argument("--work-dir", default="build")
    cl.set_defaults(func=cmd_call)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except KiCadCoderError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
