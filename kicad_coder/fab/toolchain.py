"""Locate a KiCad installation and shell out to ``kicad-cli``.

Nothing else in this library requires KiCad to be installed. This module is the
single place that knows how to find it, so the rest of the code can ask
:func:`find_toolchain` and degrade gracefully when the answer is "not here".
"""

from __future__ import annotations

import glob
import json
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

from ..errors import ToolchainError

__all__ = ["Toolchain", "find_toolchain", "run_cli"]

#: Newest first -- we prefer a modern KiCad when several are installed.
_KNOWN_VERSIONS = ("10.0", "9.0", "8.0", "7.0")


def _candidate_bin_dirs() -> List[str]:
    system = platform.system()
    out: List[str] = []

    env_root = os.environ.get("KICAD_HOME") or os.environ.get("KICAD_PATH")
    if env_root:
        out.append(os.path.join(env_root, "bin"))
        out.append(env_root)

    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", "")
        roots = [
            # Per-user installs come first: they are what the KiCad installer
            # now defaults to, and they are invisible to a Program Files or
            # uninstall-registry scan.
            os.path.join(local, "Programs") if local else "",
            local,
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            r"C:\KiCad",
            r"D:\KiCad",
        ]
        for root in roots:
            if not root:
                continue
            for ver in _KNOWN_VERSIONS:
                out.append(os.path.join(root, "KiCad", ver, "bin"))
            out.extend(sorted(glob.glob(os.path.join(root, "KiCad", "*", "bin")),
                              reverse=True))
    elif system == "Darwin":
        out.append("/Applications/KiCad/KiCad.app/Contents/MacOS")
        for ver in _KNOWN_VERSIONS:
            out.append("/Applications/KiCad/KiCad.app/Contents/MacOS")
        out.append("/opt/homebrew/bin")
        out.append("/usr/local/bin")
    else:
        out.extend(["/usr/bin", "/usr/local/bin", "/snap/bin",
                    "/var/lib/flatpak/exports/bin",
                    os.path.expanduser("~/.local/bin")])

    seen = set()
    uniq = []
    for d in out:
        if d and d not in seen:
            seen.add(d)
            uniq.append(d)
    return uniq


def _exe(name: str) -> str:
    return name + ".exe" if platform.system() == "Windows" else name


@dataclass
class Toolchain:
    """A discovered KiCad installation.

    ``available`` is False when nothing was found; every field is then empty and
    :meth:`require` raises a message that tells the user what to install.
    """

    kicad_cli: str = ""
    version: str = ""
    install_root: str = ""
    footprint_dirs: List[str] = field(default_factory=list)
    symbol_dirs: List[str] = field(default_factory=list)
    python_exe: str = ""

    @property
    def available(self) -> bool:
        return bool(self.kicad_cli)

    def require(self) -> "Toolchain":
        if not self.available:
            raise ToolchainError(
                "KiCad was not found on this system.\n"
                "kicad_coder can still build boards, BOMs and reviews without "
                "it, but DRC and fabrication exports need the kicad-cli "
                "executable.\n"
                "Install KiCad (https://www.kicad.org/download/), or set "
                "KICAD_HOME to the install directory, or pass "
                "kicad_cli=<path> explicitly."
            )
        return self

    def describe(self) -> str:
        if not self.available:
            return "KiCad: not found (board generation, BOM and review still work)"
        return (
            "KiCad %s at %s\n  kicad-cli: %s\n  footprint dirs: %s"
            % (
                self.version or "?",
                self.install_root or "?",
                self.kicad_cli,
                ", ".join(self.footprint_dirs) or "(none found)",
            )
        )


def _detect_footprint_dirs(install_root: str) -> List[str]:
    """Stock + user footprint library directories, in search order."""
    dirs: List[str] = []

    for var in ("KICAD10_FOOTPRINT_DIR", "KICAD9_FOOTPRINT_DIR",
                "KICAD8_FOOTPRINT_DIR", "KICAD7_FOOTPRINT_DIR",
                "KICAD_FOOTPRINT_DIR"):
        v = os.environ.get(var)
        if v and os.path.isdir(v):
            dirs.append(v)

    if install_root:
        for rel in (
            os.path.join("share", "kicad", "footprints"),
            os.path.join("Contents", "SharedSupport", "footprints"),
            "footprints",
        ):
            p = os.path.join(install_root, rel)
            if os.path.isdir(p):
                dirs.append(p)

    for p in (
        "/usr/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
        "/var/lib/flatpak/app/org.kicad.KiCad/current/active/files/share/kicad/footprints",
        os.path.expanduser("~/Documents/KiCad/10.0/footprints"),
        os.path.expanduser("~/Documents/KiCad/9.0/footprints"),
        os.path.expanduser("~/Documents/KiCad/8.0/footprints"),
    ):
        if os.path.isdir(p):
            dirs.append(p)

    seen = set()
    return [d for d in dirs if not (d in seen or seen.add(d))]


def find_toolchain(kicad_cli: Optional[str] = None,
                   extra_bin_dirs: Sequence[str] = ()) -> Toolchain:
    """Locate KiCad. Never raises -- check :attr:`Toolchain.available`."""
    cli = ""

    if kicad_cli and os.path.isfile(kicad_cli):
        cli = kicad_cli
    else:
        found = shutil.which("kicad-cli")
        if found:
            cli = found
        else:
            for d in list(extra_bin_dirs) + _candidate_bin_dirs():
                cand = os.path.join(d, _exe("kicad-cli"))
                if os.path.isfile(cand):
                    cli = cand
                    break

    if not cli:
        return Toolchain()

    bindir = os.path.dirname(cli)
    install_root = os.path.dirname(bindir) if os.path.basename(bindir) == "bin" else bindir

    version = ""
    try:
        proc = subprocess.run(
            [cli, "--version"], capture_output=True, text=True, timeout=30
        )
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (
            proc.stdout or proc.stderr
        ) else ""
    except (OSError, subprocess.SubprocessError, IndexError):
        pass

    python_exe = ""
    for cand in (os.path.join(bindir, _exe("python")),
                 os.path.join(bindir, _exe("python3"))):
        if os.path.isfile(cand):
            python_exe = cand
            break

    return Toolchain(
        kicad_cli=cli,
        version=version,
        install_root=install_root,
        footprint_dirs=_detect_footprint_dirs(install_root),
        python_exe=python_exe,
    )


def run_cli(
    tool: Toolchain,
    args: Sequence[str],
    timeout: int = 600,
    check: bool = True,
    ok_returncodes: Sequence[int] = (0,),
) -> subprocess.CompletedProcess:
    """Run ``kicad-cli <args>``.

    ``ok_returncodes`` exists because some subcommands signal findings rather
    than failure -- ``pcb drc --exit-code-violations`` returns 5 when it finds
    violations, which is a successful run with a non-empty result.
    """
    tool.require()
    cmd = [tool.kicad_cli] + [str(a) for a in args]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise ToolchainError("kicad-cli timed out after %ss: %s" % (timeout, " ".join(cmd))) from exc
    except OSError as exc:
        raise ToolchainError("could not run kicad-cli: %s" % exc) from exc

    if check and proc.returncode not in ok_returncodes:
        raise ToolchainError(
            "kicad-cli failed (exit %d)\n  command: %s\n  stderr: %s"
            % (proc.returncode, " ".join(cmd), (proc.stderr or "").strip()[:2000])
        )
    return proc


def cli_capabilities(tool: Toolchain) -> Dict[str, bool]:
    """Probe which subcommands this kicad-cli build supports."""
    caps = {"pcb_drc": False, "sch_bom": False, "pcb_export_step": False,
            "pcb_export_ipc2581": False}
    if not tool.available:
        return caps
    try:
        pcb = run_cli(tool, ["pcb", "--help"], timeout=60, check=False).stdout
        sch = run_cli(tool, ["sch", "--help"], timeout=60, check=False).stdout
        exp = run_cli(tool, ["pcb", "export", "--help"], timeout=60, check=False).stdout
    except ToolchainError:
        return caps
    caps["pcb_drc"] = "drc" in pcb
    caps["sch_bom"] = "bom" in sch
    caps["pcb_export_step"] = "step" in exp
    caps["pcb_export_ipc2581"] = "ipc2581" in exp
    return caps
