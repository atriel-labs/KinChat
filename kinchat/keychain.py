"""Write secrets to the login Keychain with macOS `security` (reading is in secrets.py).

Values are passed to `security` as an argument, which is how `security add-generic-password -w` takes them
non-interactively. They never go to stdout, logs or files from here.
"""
from __future__ import annotations

import subprocess

from .secrets import SERVICE


class KeychainError(RuntimeError):
    pass


def store(account: str, value: str, service: str = SERVICE, run=subprocess.run) -> None:
    """Create or update (-U) the generic password `service`/`account`."""
    r = run(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w", value],
            capture_output=True, text=True)
    if r.returncode != 0:
        # stderr from `security` never contains the value; still, report only the account.
        raise KeychainError(f"could not store Keychain item {service}/{account} (security exit {r.returncode})")


def read(account: str, service: str, run=subprocess.run) -> str | None:
    r = run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None
