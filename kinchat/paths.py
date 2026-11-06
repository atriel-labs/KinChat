"""Where kinchat keeps things for a given home directory. Functions, not constants, so tests can use a temp HOME."""
from __future__ import annotations

from pathlib import Path

LABEL = "io.kinchat.bridge"
LEGACY_LABEL = "ai.atriel.mia-bridge"


def library(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library"


def app_dir(home: Path | None = None) -> Path:
    return library(home) / "Application Support" / "kinchat"


def legacy_app_dir(home: Path | None = None) -> Path:
    return library(home) / "Application Support" / "mia-bridge"


def log_dir(home: Path | None = None) -> Path:
    return library(home) / "Logs" / "kinchat"


def plist(home: Path | None = None) -> Path:
    return library(home) / "LaunchAgents" / f"{LABEL}.plist"


def legacy_plist(home: Path | None = None) -> Path:
    return library(home) / "LaunchAgents" / f"{LEGACY_LABEL}.plist"


def fda_record(home: Path | None = None) -> Path:
    """The interpreter Full Disk Access was granted to, recorded at install so doctor can spot a change."""
    return app_dir(home) / "full-disk-access-python"
