"""Fabrication and documentation outputs via ``kicad-cli``.

Everything here shells out. The SWIG ``PLOT_CONTROLLER`` could do most of it
in-process, but that binds the caller to KiCad's bundled Python; ``kicad-cli``
works from any interpreter and its behaviour is stable across point releases.
"""

from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from ..errors import ToolchainError
from .toolchain import Toolchain, find_toolchain, run_cli

__all__ = [
    "ExportResult",
    "export_gerbers",
    "export_drill",
    "export_position",
    "export_step",
    "export_pdf",
    "export_fab_package",
]

#: A sane default plot set for a 2-layer board.
DEFAULT_LAYERS_2 = (
    "F.Cu,B.Cu,F.Paste,B.Paste,F.SilkS,B.SilkS,F.Mask,B.Mask,Edge.Cuts"
)


@dataclass
class ExportResult:
    kind: str
    output_dir: str
    files: List[str] = field(default_factory=list)
    archive: str = ""

    def summary(self) -> str:
        lines = ["%s -> %s (%d file(s))"
                 % (self.kind, self.output_dir, len(self.files))]
        for f in self.files[:12]:
            lines.append("  " + os.path.basename(f))
        if len(self.files) > 12:
            lines.append("  ... and %d more" % (len(self.files) - 12))
        if self.archive:
            lines.append("  archive: %s" % self.archive)
        return "\n".join(lines)


def _prepare(out_dir: str) -> str:
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    return out_dir


def _listing(out_dir: str, before: set) -> List[str]:
    after = set(os.listdir(out_dir))
    return sorted(os.path.join(out_dir, f) for f in (after - before))


def _copper_layer_names(count: int) -> List[str]:
    if count <= 1:
        return ["F.Cu"]
    return ["F.Cu"] + ["In%d.Cu" % i for i in range(1, count - 1)] + ["B.Cu"]


def default_plot_layers(copper_layers: int = 2) -> str:
    cu = _copper_layer_names(copper_layers)
    tech = ["F.Paste", "B.Paste", "F.SilkS", "B.SilkS",
            "F.Mask", "B.Mask", "Edge.Cuts"]
    return ",".join(cu + tech)


def export_gerbers(
    board_path: str,
    out_dir: str,
    layers: str = "",
    copper_layers: int = 2,
    toolchain: Optional[Toolchain] = None,
    use_protel_extensions: bool = False,
    subtract_soldermask: bool = False,
    timeout: int = 600,
) -> ExportResult:
    tool = toolchain or find_toolchain()
    tool.require()
    out_dir = _prepare(out_dir)
    before = set(os.listdir(out_dir))

    args = ["pcb", "export", "gerbers", "--output", out_dir,
            "--layers", layers or default_plot_layers(copper_layers)]
    if use_protel_extensions:
        args.append("--use-drill-file-origin")
    if subtract_soldermask:
        args.append("--subtract-soldermask")
    args.append(os.path.abspath(board_path))

    run_cli(tool, args, timeout=timeout)
    return ExportResult("gerbers", out_dir, _listing(out_dir, before))


def export_drill(
    board_path: str,
    out_dir: str,
    fmt: str = "excellon",
    toolchain: Optional[Toolchain] = None,
    map_format: str = "",
    timeout: int = 300,
) -> ExportResult:
    tool = toolchain or find_toolchain()
    tool.require()
    out_dir = _prepare(out_dir)
    before = set(os.listdir(out_dir))

    args = ["pcb", "export", "drill", "--output", out_dir,
            "--format", fmt, "--excellon-separate-th"]
    if map_format:
        args.extend(["--generate-map", "--map-format", map_format])
    args.append(os.path.abspath(board_path))

    run_cli(tool, args, timeout=timeout)
    return ExportResult("drill", out_dir, _listing(out_dir, before))


def export_position(
    board_path: str,
    out_path: str,
    fmt: str = "csv",
    side: str = "both",
    units: str = "mm",
    toolchain: Optional[Toolchain] = None,
    timeout: int = 300,
) -> ExportResult:
    tool = toolchain or find_toolchain()
    tool.require()
    out_path = os.path.abspath(out_path)
    _prepare(os.path.dirname(out_path))

    args = ["pcb", "export", "pos", "--output", out_path,
            "--format", fmt, "--side", side, "--units", units,
            os.path.abspath(board_path)]
    run_cli(tool, args, timeout=timeout)
    return ExportResult("position", os.path.dirname(out_path), [out_path])


def export_step(
    board_path: str,
    out_path: str,
    toolchain: Optional[Toolchain] = None,
    include_tracks: bool = False,
    timeout: int = 1200,
) -> ExportResult:
    tool = toolchain or find_toolchain()
    tool.require()
    out_path = os.path.abspath(out_path)
    _prepare(os.path.dirname(out_path))

    args = ["pcb", "export", "step", "--output", out_path, "--no-dnp"]
    if include_tracks:
        args.append("--include-tracks")
    args.append(os.path.abspath(board_path))
    run_cli(tool, args, timeout=timeout)
    return ExportResult("step", os.path.dirname(out_path), [out_path])


def export_pdf(
    board_path: str,
    out_path: str,
    layers: str = "F.Cu,F.SilkS,Edge.Cuts",
    toolchain: Optional[Toolchain] = None,
    black_and_white: bool = False,
    timeout: int = 300,
) -> ExportResult:
    tool = toolchain or find_toolchain()
    tool.require()
    out_path = os.path.abspath(out_path)
    _prepare(os.path.dirname(out_path))

    args = ["pcb", "export", "pdf", "--output", out_path, "--layers", layers]
    if black_and_white:
        args.append("--black-and-white")
    args.append(os.path.abspath(board_path))
    run_cli(tool, args, timeout=timeout)
    return ExportResult("pdf", os.path.dirname(out_path), [out_path])


def export_fab_package(
    board_path: str,
    out_dir: str,
    copper_layers: int = 2,
    toolchain: Optional[Toolchain] = None,
    archive: bool = True,
    include_position: bool = True,
) -> ExportResult:
    """Gerbers + drill (+ pick-and-place), optionally zipped for a fab house."""
    tool = toolchain or find_toolchain()
    tool.require()
    out_dir = _prepare(out_dir)
    before = set(os.listdir(out_dir))

    export_gerbers(board_path, out_dir, copper_layers=copper_layers,
                   toolchain=tool)
    export_drill(board_path, out_dir, toolchain=tool)
    if include_position:
        base = os.path.splitext(os.path.basename(board_path))[0]
        try:
            export_position(board_path, os.path.join(out_dir, base + "-pos.csv"),
                            toolchain=tool)
        except ToolchainError:
            pass  # a board with no placeable parts has no position file

    files = _listing(out_dir, before)

    archive_path = ""
    if archive and files:
        base = os.path.splitext(os.path.basename(board_path))[0]
        archive_path = os.path.join(out_dir, base + "-fab.zip")
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, os.path.basename(f))

    return ExportResult("fab package", out_dir, files, archive=archive_path)
