"""Write secrets to the login Keychain with macOS `security`, without putting them on a command line.

Every write is read back and compared, so a truncated or failed write is an error, never a silent success.
`security` keeps at most 128 characters typed at its prompt, so long values are stored in numbered chunks.

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

from .secrets import CHUNK_MARK, SERVICE, read_item


class KeychainError(RuntimeError):
    pass


CHUNK = 100  # `security`'s password prompt keeps at most 128 characters; longer values are stored in chunks


def answer_prompts(cmd: list[str], value: str, timeout: float = 20.0) -> int:
    """Run cmd on a new pseudo-terminal, typing `value` at every prompt (a line ending in ":"). Returns the
    exit status; raises KeychainError if the command never prompted. The value is never in argv, never
    echoed and never returned or logged."""
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.execvp(cmd[0], cmd)
        finally:
            os._exit(127)
    try:
        attrs = termios.tcgetattr(fd)
        attrs[3] &= ~(termios.ECHO | termios.ICANON)  # no echo; no canonical-mode line limit (MAX_CANON)
        attrs[6][termios.VMIN], attrs[6][termios.VTIME] = 1, 0
        termios.tcsetattr(fd, termios.TCSANOW, attrs)
    except termios.error:
        pass
    deadline, pending, answered, status = time.monotonic() + timeout, b"", 0, None
    try:
        while True:
            if time.monotonic() > deadline:
                os.kill(pid, signal.SIGKILL)
                raise KeychainError(f"{cmd[0]} did not finish within {timeout:.0f}s")
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                reaped, st = os.waitpid(pid, os.WNOHANG)
                if reaped:
                    status = st  # keep it: the final waitpid below can't get it again
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
    if status is None:
        try:
            _, status = os.waitpid(pid, 0)
        except ChildProcessError as e:
            raise KeychainError(f"lost track of {cmd[0]}; its result is unknown") from e
    code = os.waitstatus_to_exitcode(status)
    if code == 0 and answered == 0:
        raise KeychainError(f"{cmd[0]} exited without asking for the password")
    return code


def _store_one(account: str, value: str, service: str, runner) -> None:
    code = runner(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"], value)
    if code != 0:
        raise KeychainError(f"could not store Keychain item {service}/{account} (security exit {code})")


def store(account: str, value: str, service: str = SERVICE, runner=answer_prompts, run=subprocess.run) -> None:
    """Create or update (-U) the generic password `service`/`account`, then read it back and compare.

    The value is typed at `security`'s own prompt through a pty, which takes at most 128 characters. A
    longer value (the base64 Graph key, a list of calendar URLs) is split into items "<account>#1".."#N" of
    at most CHUNK characters each, and the item itself holds "kinchat-chunks:N"; secrets.get reassembles
    it. Nothing is ever on argv, /usr/bin/security creates every item, and the read-back catches truncation
    or any silent failure."""
    if "\n" in value or "\r" in value:
        raise KeychainError(f"{account}: the value must be a single line")
    if value.startswith(CHUNK_MARK):
        raise KeychainError(f"{account}: the value can't start with {CHUNK_MARK!r}")
    if len(value) <= CHUNK:
        _store_one(account, value, service, runner)
    else:
        chunks = [value[i:i + CHUNK] for i in range(0, len(value), CHUNK)]
        for i, chunk in enumerate(chunks, 1):
            _store_one(f"{account}#{i}", chunk, service, runner)
        _store_one(account, f"{CHUNK_MARK}{len(chunks)}", service, runner)
    stored = read_item(service, account, run=run)
    if stored != value:
        detail = "missing" if stored is None else f"{len(stored)} of {len(value)} characters"
        raise KeychainError(f"Keychain item {service}/{account} did not read back correctly ({detail})")


def read(account: str, service: str, run=subprocess.run) -> str | None:
    return read_item(service, account, run=run)
