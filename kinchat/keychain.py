"""Write secrets to the login Keychain with macOS `security`, without putting them on a command line.

`security add-generic-password ... -w <value>` would show the value to every process on the Mac (`ps`) while
it runs. Instead `-w` is given last with no value, which makes `security` prompt for the password on its
terminal, and we answer that prompt through a pseudo-terminal we control. The item is created by
/usr/bin/security itself, exactly as if typed by hand, so the bridge's later
`security find-generic-password -w` reads stay silent. (An item written from Python with SecItemAdd, as
the keyring package does, would trust the Python binary instead, which would prompt or fail when
/usr/bin/security reads it from the launchd agent, and again after every Python upgrade.)
"""
from __future__ import annotations

import os
import pty
import select
import signal
import subprocess
import termios
import time

from .secrets import SERVICE


class KeychainError(RuntimeError):
    pass


def answer_prompts(cmd: list[str], value: str, timeout: float = 20.0) -> int:
    """Run cmd on a new pseudo-terminal, typing `value` at every prompt (a line ending in ":"). Returns the
    exit status. The value is never in argv, never echoed and never returned or logged."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(cmd[0], cmd)
        finally:
            os._exit(127)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~termios.ECHO  # don't echo what we type back to ourselves
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    deadline, pending, answered = time.monotonic() + timeout, b"", 0
    try:
        while True:
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                raise KeychainError(f"{cmd[0]} did not finish within {timeout:.0f}s")
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                if os.waitpid(pid, os.WNOHANG) != (0, 0):
                    break
                continue
            try:
                data = os.read(fd, 1024)
            except OSError:  # EIO: the child closed the terminal
                break
            if not data:
                break
            pending = (pending + data)[-256:]
            if pending.rstrip().endswith(b":"):
                if answered >= 2:  # asked a third time: the two entries didn't match or were refused
                    os.kill(pid, signal.SIGKILL)
                    raise KeychainError(f"{cmd[0]} kept asking for the password")
                os.write(fd, value.encode() + b"\n")
                answered += 1
                pending = b""
    finally:
        os.close(fd)
    try:
        _, status = os.waitpid(pid, 0)
    except ChildProcessError:
        return 0
    return os.waitstatus_to_exitcode(status)


def store(account: str, value: str, service: str = SERVICE, runner=answer_prompts) -> None:
    """Create or update (-U) the generic password `service`/`account`."""
    if "\n" in value or "\r" in value:
        raise KeychainError(f"{account}: the value must be a single line (it is typed at a prompt)")
    code = runner(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"], value)
    if code != 0:
        raise KeychainError(f"could not store Keychain item {service}/{account} (security exit {code})")


def read(account: str, service: str, run=subprocess.run) -> str | None:
    r = run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None
