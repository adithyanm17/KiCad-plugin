"""kicad_coder -- design, review and document PCBs from code or from an LLM.

The short version::

    from kicad_coder import Design, Component, FootprintLibrary, build

    lib = FootprintLibrary()
    d = Design(name="blinky")
    d.add_component(Component("R1", "330", "Resistor_SMD:R_0603_1608Metric"))
    d.add_component(Component("D1", "LED", "LED_SMD:LED_0603_1608Metric"))
    d.connect("LED_K", [("R1", "2"), ("D1", "1")])
    build(d, lib, "build/blinky.kicad_pcb")

To drive it with a language model, use :class:`kicad_coder.llm.DesignSession`
and one of the adapters in :mod:`kicad_coder.llm.adapters`.

Only the standard library is required. KiCad itself is optional: it is needed
for DRC and fabrication export, and for nothing else.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .errors import (BackendError, KiCadCoderError, LibraryError, ToolchainError,
                     ToolError, ValidationError)
from .ir.types import (BoardOutline, Component, Design, DesignRules, Net,
                       NetClass, Placement)
from .ir.validate import Issue, ValidationResult, validate

__all__ = [
    "__version__",
    # IR
    "Design", "Component", "Net", "NetClass", "DesignRules", "BoardOutline",
    "Placement",
    # validation and review
    "validate", "ValidationResult", "Issue", "review", "review_context",
    # library, placement, output
    "FootprintLibrary", "place", "write_board", "build",
    "bom_from_design", "bom_from_board", "write_bom",
    "find_toolchain", "run_drc",
    # llm
    "DesignSession", "system_prompt",
    # errors
    "KiCadCoderError", "ValidationError", "LibraryError", "BackendError",
    "ToolchainError", "ToolError",
]


def __getattr__(name):
    """Import the heavier pieces lazily.

    Keeps ``import kicad_coder`` cheap, and means a broken optional dependency
    only surfaces when you actually reach for that part of the library.
    """
    if name == "FootprintLibrary":
        from .library.footprints import FootprintLibrary
        return FootprintLibrary
    if name == "place":
        from .place.engine import place
        return place
    if name == "write_board":
        from .backends.board import write_board
        return write_board
    if name in ("review", "review_context"):
        from .review import rules
        return getattr(rules, name)
    if name in ("bom_from_design", "bom_from_board", "write_bom"):
        from .fab import bom
        return getattr(bom, name)
    if name == "find_toolchain":
        from .fab.toolchain import find_toolchain
        return find_toolchain
    if name == "run_drc":
        from .fab.drc import run_drc
        return run_drc
    if name == "DesignSession":
        from .llm.tools import DesignSession
        return DesignSession
    if name == "system_prompt":
        from .llm.prompts import system_prompt
        return system_prompt
    if name == "build":
        return _build
    raise AttributeError("module %r has no attribute %r" % (__name__, name))


def _build(design, library=None, path: str = "", version: str = "auto",
           seed: int = 0, bom_path: str = ""):
    """Validate, place, write the board and optionally the BOM, in one call.

    Returns the :class:`~kicad_coder.backends.board.BoardResult`. Raises
    :class:`ValidationError` if the design has blocking problems.
    """
    from .backends.board import write_board
    from .fab.bom import bom_from_design, write_bom
    from .library.footprints import FootprintLibrary
    from .place.engine import place

    library = library or FootprintLibrary()
    result = validate(design, library=library)
    if not result.ok:
        raise ValidationError(str(result))

    place(design, library, seed=seed)
    board = write_board(design, library, path or (design.name + ".kicad_pcb"),
                        version=version)
    if bom_path:
        write_bom(bom_from_design(design), bom_path)
    return board
