"""Fake `security` and `launchctl` executables for tests. They record each call and keep state in a directory."""
from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

SCRIPT = r'''#!{python}
import json, os, sys
from pathlib import Path
state = Path({state!r})
name = Path(sys.argv[0]).name
args = sys.argv[1:]
with open(state / "calls.jsonl", "a") as f:
    f.write(json.dumps([name] + args) + "\n")
def opt(flag):
    return args[args.index(flag) + 1] if flag in args else None
if name == "security":
    kc = state / "keychain.json"
    items = json.loads(kc.read_text()) if kc.exists() else {{}}
    key = f"{{opt('-s')}}/{{opt('-a')}}"
    if args[0] == "find-generic-password":
        if key in items:
            print(items[key]); sys.exit(0)
        print("security: The specified item could not be found in the keychain.", file=sys.stderr); sys.exit(44)
    if args[0] == "add-generic-password":
        items[key] = opt("-w"); kc.write_text(json.dumps(items)); sys.exit(0)
    sys.exit(1)
if name == "launchctl":
    loaded = state / "loaded.json"
    labels = set(json.loads(loaded.read_text())) if loaded.exists() else set()
    def label_of(target):
        import plistlib
        try:
            with open(target, "rb") as f:
                return plistlib.load(f)["Label"]
        except Exception:
            return Path(target).stem
    cmd = args[0]
    if cmd == "print":
        sys.exit(0 if args[1].rsplit("/", 1)[1] in labels else 113)
    if cmd == "bootstrap":
        labels.add(label_of(args[2]))
    elif cmd == "bootout":
        target = args[-1]
        labels.discard(target.rsplit("/", 1)[1] if target.startswith("gui/") else label_of(target))
    loaded.write_text(json.dumps(sorted(labels)))
    sys.exit(0)
sys.exit(1)
'''


class FakeBin:
    def __init__(self, root: Path):
        self.state = root / "state"
        self.bin = root / "bin"
        self.state.mkdir(parents=True)
        self.bin.mkdir()
        for name in ("security", "launchctl"):
            p = self.bin / name
            p.write_text(SCRIPT.format(python=sys.executable, state=str(self.state)))
            p.chmod(p.stat().st_mode | stat.S_IEXEC)

    def path_env(self) -> str:
        return f"{self.bin}{os.pathsep}{os.environ.get('PATH', '')}"

    def calls(self, name: str | None = None) -> list[list[str]]:
        f = self.state / "calls.jsonl"
        rows = [json.loads(line) for line in f.read_text().splitlines()] if f.exists() else []
        return [r for r in rows if name is None or r[0] == name]

    def keychain(self) -> dict:
        f = self.state / "keychain.json"
        return json.loads(f.read_text()) if f.exists() else {}

    def set_secret(self, service: str, account: str, value: str) -> None:
        items = self.keychain()
        items[f"{service}/{account}"] = value
        (self.state / "keychain.json").write_text(json.dumps(items))

    def loaded(self) -> set[str]:
        f = self.state / "loaded.json"
        return set(json.loads(f.read_text())) if f.exists() else set()

    def load(self, label: str) -> None:
        labels = self.loaded() | {label}
        (self.state / "loaded.json").write_text(json.dumps(sorted(labels)))
