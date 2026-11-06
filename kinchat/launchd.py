"""The launchd agent that keeps the bridge running: render its plist, load it, and retire the pre-rename agent."""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from . import paths


def venv_python() -> str:
    """The interpreter path launchd should run: the venv's python, not its symlink target."""
    return sys.executable


def real_python(python: str | None = None) -> str:
    """What macOS checks Full Disk Access against: the real binary behind the venv's symlink."""
    return os.path.realpath(python or venv_python())


def plist_dict(python: str, home: Path, workdir: Path) -> dict:
    logs = paths.log_dir(home)
    return {
        "Label": paths.LABEL,
        "ProgramArguments": [python, "-m", "kinchat", "run"],
        "WorkingDirectory": str(workdir),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "launchd.out.log"),
        "StandardErrorPath": str(logs / "launchd.err.log"),
    }


def plist_python(plist_path: Path) -> str | None:
    try:
        with open(plist_path, "rb") as f:
            return plistlib.load(f)["ProgramArguments"][0]
    except (OSError, KeyError, IndexError, plistlib.InvalidFileException):
        return None


def domain() -> str:
    return f"gui/{os.getuid()}"


def is_loaded(label: str, run=subprocess.run) -> bool:
    return run(["launchctl", "print", f"{domain()}/{label}"], capture_output=True, text=True).returncode == 0


def bootout(plist_path: Path, run=subprocess.run) -> None:
    run(["launchctl", "bootout", domain(), str(plist_path)], capture_output=True, text=True)


def bootstrap(plist_path: Path, run=subprocess.run) -> None:
    r = run(["launchctl", "bootstrap", domain(), str(plist_path)], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap {plist_path.name} failed: {r.stderr.strip() or r.returncode}")


def disable_legacy(home: Path, run=subprocess.run) -> Path | None:
    """Stop the pre-rename agent and move its plist aside (never deleted). Returns where it went."""
    old = paths.legacy_plist(home)
    if not old.exists():
        return None
    bootout(old, run)
    moved = old.with_name(old.name + ".disabled")
    old.replace(moved)
    return moved


def install(home: Path | None = None, python: str | None = None, workdir: Path | None = None,
            run=subprocess.run) -> list[str]:
    """Write and (re)load io.kinchat.bridge, retiring the legacy agent first. Returns what it did."""
    home = home or Path.home()
    python = python or venv_python()
    done = []
    moved = disable_legacy(home, run)
    if moved:
        done.append(f"stopped {paths.LEGACY_LABEL} and moved its plist to {moved}")
    target = paths.plist(home)
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.log_dir(home).mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as f:
        plistlib.dump(plist_dict(python, home, workdir or home), f)
    bootout(target, run)
    bootstrap(target, run)
    done.append(f"loaded {paths.LABEL} from {target}")
    record = paths.fda_record(home)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(real_python(python) + "\n")
    return done


ROLLBACK_HINT = ("launchctl bootout gui/$(id -u)/io.kinchat.bridge; "
                 "mv ~/Library/LaunchAgents/ai.atriel.mia-bridge.plist.disabled ~/Library/LaunchAgents/ai.atriel.mia-bridge.plist; "
                 "launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.atriel.mia-bridge.plist")
