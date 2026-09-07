"""Install the plugin into KiCad's scripting directory.

Run from the project root::

    python plugin/install.py            # copy
    python plugin/install.py --link     # symlink, for development
    python plugin/install.py --uninstall

Copies both this ``plugin`` directory's entry point and the ``kicad_coder``
package, because KiCad's plugin loader only adds its own plugin folder to
``sys.path``.
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KNOWN_VERSIONS = ("10.0", "9.0", "8.0", "7.0")


def plugin_dirs() -> list:
    """Candidate KiCad scripting plugin directories, newest version first."""
    system = platform.system()
    out = []
    for ver in KNOWN_VERSIONS:
        if system == "Windows":
            base = os.environ.get("APPDATA", "")
            if base:
                out.append(os.path.join(base, "kicad", ver, "scripting", "plugins"))
        elif system == "Darwin":
            out.append(os.path.expanduser(
                "~/Documents/KiCad/%s/scripting/plugins" % ver))
        else:
            out.append(os.path.expanduser(
                "~/.local/share/kicad/%s/scripting/plugins" % ver))
    return out


def resolve_target(explicit: str = "") -> str:
    if explicit:
        return os.path.abspath(explicit)
    for d in plugin_dirs():
        if os.path.isdir(os.path.dirname(d)):
            return d
    return plugin_dirs()[0]


def _remove(path: str) -> None:
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def install(target: str, link: bool) -> int:
    os.makedirs(target, exist_ok=True)

    entry_src = os.path.join(PROJECT_ROOT, "plugin", "kicad_coder_plugin.py")
    entry_dst = os.path.join(target, "kicad_coder_plugin.py")
    pkg_src = os.path.join(PROJECT_ROOT, "kicad_coder")
    pkg_dst = os.path.join(target, "kicad_coder")

    for dst in (entry_dst, pkg_dst):
        _remove(dst)

    if link:
        try:
            os.symlink(entry_src, entry_dst)
            os.symlink(pkg_src, pkg_dst, target_is_directory=True)
        except OSError as exc:
            print("could not create symlinks (%s)" % exc)
            print("On Windows this needs Developer Mode or an elevated shell.")
            print("Falling back to copying.")
            return install(target, link=False)
    else:
        shutil.copy2(entry_src, entry_dst)
        shutil.copytree(pkg_src, pkg_dst,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))

    print("Installed to %s" % target)
    print("  %s" % entry_dst)
    print("  %s" % pkg_dst)
    print()
    print("In KiCad: Tools > External Plugins > Refresh Plugins.")
    print("Three entries appear: Review board, Generate BOM, Export design IR.")
    return 0


def uninstall(target: str) -> int:
    removed = []
    for name in ("kicad_coder_plugin.py", "kicad_coder"):
        path = os.path.join(target, name)
        if os.path.exists(path) or os.path.islink(path):
            _remove(path)
            removed.append(path)
    if removed:
        print("Removed:")
        for r in removed:
            print("  %s" % r)
    else:
        print("Nothing installed at %s" % target)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--target", default="",
                   help="plugin directory (auto-detected by default)")
    p.add_argument("--link", action="store_true",
                   help="symlink instead of copying, so edits take effect live")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--list", action="store_true",
                   help="show candidate plugin directories and exit")
    args = p.parse_args()

    if args.list:
        for d in plugin_dirs():
            print("%s  %s" % ("exists " if os.path.isdir(d) else "missing", d))
        return 0

    target = resolve_target(args.target)
    return uninstall(target) if args.uninstall else install(target, args.link)


if __name__ == "__main__":
    raise SystemExit(main())
