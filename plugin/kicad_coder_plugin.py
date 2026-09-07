"""KiCad action plugin: review the open board and write its BOM.

Install by copying (or symlinking) the whole ``plugin`` directory, together
with the ``kicad_coder`` package, into KiCad's plugin folder (``<ver>`` is
your KiCad version, e.g. ``10.0``):

  Windows  %APPDATA%\\kicad\\<ver>\\scripting\\plugins\\
  Linux    ~/.local/share/kicad/<ver>/scripting/plugins/
  macOS    ~/Documents/KiCad/<ver>/scripting/plugins/

Then use Tools > External Plugins > Refresh, and the entries appear on the
toolbar. ``install.py`` in this directory does the copying for you.

The plugin deliberately does not modify the board. It reads, reports, and
writes files next to the project -- a review tool you can run at any time
without wondering what it changed.
"""

from __future__ import annotations

import os
import sys
import traceback

# Make the sibling kicad_coder package importable when this file is dropped
# into KiCad's plugin directory.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _candidate in (_HERE, os.path.dirname(_HERE)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import pcbnew  # noqa: E402  (only importable inside KiCad)


def _message(title: str, text: str, long_text: str = "") -> None:
    """Show a dialog if wx is available, else fall back to stdout."""
    try:
        import wx
        dlg = wx.MessageDialog(None, text, title, wx.OK | wx.ICON_INFORMATION)
        if long_text:
            try:
                dlg.SetExtendedMessage(long_text)
            except Exception:
                pass
        dlg.ShowModal()
        dlg.Destroy()
    except Exception:
        print("[%s] %s\n%s" % (title, text, long_text))


def _board_dir(board) -> str:
    path = board.GetFileName() or ""
    return os.path.dirname(path) if path else os.getcwd()


def _board_stem(board) -> str:
    path = board.GetFileName() or ""
    return os.path.splitext(os.path.basename(path))[0] or "board"


class ReviewBoardPlugin(pcbnew.ActionPlugin):
    """Run the deterministic design review against the open board."""

    def defaults(self) -> None:
        self.name = "kicad_coder: Review board"
        self.category = "Design review"
        self.description = (
            "Check the open board for missing decoupling, floating pins, "
            "density problems and net class issues."
        )
        self.show_toolbar_button = True
        self.icon_file_name = ""

    def Run(self) -> None:
        try:
            from kicad_coder.backends.pcbnew_backend import board_to_design
            from kicad_coder.library.footprints import FootprintLibrary
            from kicad_coder.review.rules import review
            from kicad_coder.ir.validate import validate

            board = pcbnew.GetBoard()
            design = board_to_design(board)
            library = FootprintLibrary()

            findings = review(design, library)
            structural = validate(design, library=library)

            lines = [design.summary(), ""]
            if structural.errors:
                lines.append("Structural errors: %d" % len(structural.errors))
                lines.extend("  " + str(i) for i in structural.errors[:10])
                lines.append("")
            if findings:
                lines.append("Review findings: %d" % len(findings))
                lines.extend(str(f) for f in findings)
            else:
                lines.append("Review found no issues.")

            report = "\n".join(lines)

            out = os.path.join(_board_dir(board), _board_stem(board) + "-review.txt")
            try:
                with open(out, "w", encoding="utf-8") as fh:
                    fh.write(report + "\n")
                saved = "Saved to %s" % out
            except OSError as exc:
                saved = "Could not save the report: %s" % exc

            _message("kicad_coder review",
                     "%d finding(s). %s" % (len(findings), saved),
                     report)
        except Exception as exc:
            _message("kicad_coder review failed", str(exc),
                     traceback.format_exc())


class GenerateBomPlugin(pcbnew.ActionPlugin):
    """Write a consolidated BOM from the open board."""

    def defaults(self) -> None:
        self.name = "kicad_coder: Generate BOM"
        self.category = "Fabrication outputs"
        self.description = (
            "Write a BOM consolidated by part, including MPN and manufacturer "
            "fields from the footprints."
        )
        self.show_toolbar_button = True
        self.icon_file_name = ""

    def Run(self) -> None:
        try:
            from kicad_coder.backends.pcbnew_backend import board_to_design
            from kicad_coder.fab.bom import bom_from_design, write_bom

            board = pcbnew.GetBoard()
            design = board_to_design(board)
            bom = bom_from_design(design)

            stem = os.path.join(_board_dir(board), _board_stem(board) + "-bom")
            csv_path = write_bom(bom, stem + ".csv")
            md_path = write_bom(bom, stem + ".md")

            missing = bom.missing_mpn()
            detail = bom.summary()
            if missing:
                detail += "\n\nParts without an MPN:\n  " + ", ".join(missing)

            _message("kicad_coder BOM",
                     "%d unique part(s) written to:\n%s\n%s"
                     % (bom.unique_parts, csv_path, md_path),
                     detail)
        except Exception as exc:
            _message("kicad_coder BOM failed", str(exc), traceback.format_exc())


class ExportDesignPlugin(pcbnew.ActionPlugin):
    """Export the open board as design IR JSON, for an LLM to work on."""

    def defaults(self) -> None:
        self.name = "kicad_coder: Export design IR"
        self.category = "Design review"
        self.description = (
            "Write the board's netlist, components and rules as JSON so a "
            "language model can review or modify it."
        )
        self.show_toolbar_button = True
        self.icon_file_name = ""

    def Run(self) -> None:
        try:
            import json
            from kicad_coder.backends.pcbnew_backend import board_to_design

            board = pcbnew.GetBoard()
            design = board_to_design(board)
            out = os.path.join(_board_dir(board), _board_stem(board) + "-ir.json")
            with open(out, "w", encoding="utf-8") as fh:
                json.dump(design.to_dict(), fh, indent=2)

            _message("kicad_coder export",
                     "Design IR written to:\n%s" % out,
                     design.summary())
        except Exception as exc:
            _message("kicad_coder export failed", str(exc),
                     traceback.format_exc())


ReviewBoardPlugin().register()
GenerateBomPlugin().register()
ExportDesignPlugin().register()
