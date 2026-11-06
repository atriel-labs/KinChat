"""`kinchat migrate --from mia-bridge` and `kinchat migrate --rollback`.

Moving a live mia-bridge install to kinchat, safely:

- Nothing old is moved or deleted except the old launchd plist, which is renamed to *.disabled (and renamed
  back by --rollback). The old Application Support directory and the old Keychain items stay as they are.
- The old agent is stopped BEFORE state.db is copied, so nothing it writes is lost between copy and switch.
- state.db is copied with SQLite's backup API (it is in WAL mode; a file copy can miss recent rows), then the
  schema migrations, including the chat-ID namespacing, run on the copy only.
- Anything already in the kinchat directory that would be replaced is renamed to *.pre-migrate-<time> first.
- Every run is recorded in migration.json in the kinchat directory.

--rollback stops io.kinchat.bridge, renames its plist to *.disabled, restores the old plist and starts the
old agent. The kinchat directory and Keychain items are left in place; the old bridge never reads them.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import yaml

from . import keychain, launchd, paths
from .secrets import LEGACY_SERVICES, SERVICE
from .store import Store

LEGACY_SERVICE = LEGACY_SERVICES[0]
ACCOUNTS = ("anthropic-api-key", "dashboard-password", "graph-cert-key-b64", "imap-app-password", "ics-urls")
FILES = ("config.yaml", "family.md")


class MigrateError(RuntimeError):
    pass


def _stamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _keep_aside(path: Path, stamp: str) -> Path | None:
    """Rename an existing file (and a database's -wal/-shm) out of the way. Returns the new name."""
    if not path.exists():
        return None
    target = path.with_name(f"{path.name}.pre-migrate-{stamp}")
    for suffix in ("-wal", "-shm"):
        side = path.with_name(path.name + suffix)
        if side.exists():
            side.replace(target.with_name(target.name + suffix))
    path.replace(target)
    return target


def copy_database(src: Path, dst: Path) -> None:
    """Copy a (possibly WAL-mode, possibly in-use) SQLite database consistently."""
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dest = sqlite3.connect(dst)
        try:
            source.backup(dest)
        finally:
            dest.close()
    finally:
        source.close()


def fill_handles(config: Path) -> str | None:
    """Configs from before assistant.handles: record the old graph.mailbox as the assistant's handle."""
    text = config.read_text()
    raw = yaml.safe_load(text) or {}
    assistant = raw.get("assistant") or {}
    mailbox = (raw.get("graph") or {}).get("mailbox")
    if assistant.get("handles") or not mailbox:
        return None
    line = f"  handles: [{json.dumps(str(mailbox))}]   # added by kinchat migrate (was graph.mailbox)"
    if "assistant" in raw and isinstance(raw["assistant"], dict) and raw["assistant"]:
        lines = text.splitlines()
        i = next(n for n, x in enumerate(lines) if x.rstrip() == "assistant:")
        lines.insert(i + 1, line)
        text = "\n".join(lines) + "\n"
    else:
        text = text.rstrip("\n") + f"\n\nassistant:\n{line}\n"
    config.write_text(text)
    assert (yaml.safe_load(text)["assistant"]["handles"]) == [str(mailbox)]
    return str(mailbox)


def _record(home: Path, entry: dict) -> Path:
    path = paths.app_dir(home) / "migration.json"
    data = json.loads(path.read_text()) if path.exists() else {"runs": []}
    data["runs"].append(entry)
    path.write_text(json.dumps(data, indent=2) + "\n")
    return path


def migrate(home: Path | None = None, force: bool = False, python: str | None = None,
            run: Callable = subprocess.run, out=print) -> dict:
    home = home or Path.home()
    old_dir, new_dir = paths.legacy_app_dir(home), paths.app_dir(home)
    old_plist = paths.legacy_plist(home)
    agent_found = old_plist.exists() or launchd.is_loaded(paths.LEGACY_LABEL, run)
    if not agent_found and not force:
        raise MigrateError(f"no {paths.LEGACY_LABEL} launchd agent found (already migrated? see "
                           f"{new_dir / 'migration.json'}). Use --force to copy the files anyway.")
    if not old_dir.is_dir():
        raise MigrateError(f"{old_dir} does not exist; nothing to migrate")
    python = python or launchd.venv_python()
    stamp = _stamp()
    entry: dict = {"action": "migrate", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "from": str(old_dir),
                   "to": str(new_dir), "copied": [], "kept_aside": [], "keychain_copied": [], "warnings": []}
    new_dir.mkdir(parents=True, exist_ok=True)

    old_python = launchd.plist_python(old_plist) if old_plist.exists() else None
    moved = launchd.disable_legacy(home, run)  # stop the old bridge before copying its database
    if moved:
        entry["old_plist_moved_to"] = str(moved)
        out(f"stopped {paths.LEGACY_LABEL}; its plist is now {moved}")

    for name in FILES:
        src, dst = old_dir / name, new_dir / name
        if not src.exists():
            continue
        if dst.exists() and dst.read_bytes() != src.read_bytes():
            entry["kept_aside"].append(str(_keep_aside(dst, stamp)))
        if not dst.exists():
            shutil.copy2(src, dst)
        entry["copied"].append(name)
        out(f"copied {name}")

    old_db, new_db = old_dir / "state.db", new_dir / "state.db"
    if old_db.exists():
        aside = _keep_aside(new_db, stamp)
        if aside:
            entry["kept_aside"].append(str(aside))
        copy_database(old_db, new_db)
        store = Store(new_db)  # runs the schema migrations on the copy
        store._db.close()
        entry["copied"].append("state.db")
        out("copied state.db and upgraded the copy's schema")

    if (new_dir / "config.yaml").exists():
        handle = fill_handles(new_dir / "config.yaml")
        if handle:
            entry["handles_filled"] = handle
            out(f"set assistant.handles to {handle} (from graph.mailbox)")

    for account in ACCOUNTS:
        value = keychain.read(account, LEGACY_SERVICE, run=run)
        if value is None:
            continue
        if keychain.read(account, SERVICE, run=run) != value:
            keychain.store(account, value, SERVICE, run=run)
        entry["keychain_copied"].append(account)
    if entry["keychain_copied"]:
        out(f"copied Keychain items to service {SERVICE}: {', '.join(entry['keychain_copied'])} "
            f"(the {LEGACY_SERVICE} items are untouched)")

    for line in launchd.install(home, python, run=run):
        out(line)
    entry["new_plist"] = str(paths.plist(home))
    new_real = launchd.real_python(python)
    entry["python"] = {"old": old_python and os.path.realpath(old_python), "new": new_real}
    if old_python and os.path.realpath(old_python) != new_real:
        warning = (f"launchd now runs {new_real}, but Full Disk Access was probably granted to "
                   f"{os.path.realpath(old_python)}: add {new_real} in System Settings > Privacy & Security > "
                   "Full Disk Access, then `launchctl kickstart -k gui/$(id -u)/io.kinchat.bridge`")
        entry["warnings"].append(warning)
        out(f"WARNING: {warning}")
    record = _record(home, entry)
    out(f"recorded in {record}")
    out(f"to undo: kinchat migrate --rollback   (or by hand: {launchd.ROLLBACK_HINT})")
    return entry


def rollback(home: Path | None = None, run: Callable = subprocess.run, out=print) -> dict:
    home = home or Path.home()
    entry: dict = {"action": "rollback", "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "done": []}
    new_plist = paths.plist(home)
    run(["launchctl", "bootout", f"{launchd.domain()}/{paths.LABEL}"], capture_output=True, text=True)
    entry["done"].append(f"stopped {paths.LABEL}")
    if new_plist.exists():
        disabled = new_plist.with_name(new_plist.name + ".disabled")
        new_plist.replace(disabled)  # so it doesn't start again at the next login
        entry["done"].append(f"moved {new_plist} to {disabled}")
    old = paths.legacy_plist(home)
    old_disabled = old.with_name(old.name + ".disabled")
    if old_disabled.exists():
        old_disabled.replace(old)
        entry["done"].append(f"restored {old}")
    if not old.exists():
        for line in entry["done"]:
            out(line)
        raise MigrateError(f"no {old.name} or {old_disabled.name} to restore; the old agent was not started")
    launchd.bootstrap(old, run)
    entry["done"].append(f"started {paths.LEGACY_LABEL}")
    entry["done"].append(f"left {paths.app_dir(home)} and the {SERVICE} Keychain items in place (unused by the old bridge)")
    for line in entry["done"]:
        out(line)
    if paths.app_dir(home).exists():
        _record(home, entry)
    return entry
