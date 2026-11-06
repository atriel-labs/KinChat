"""Write secrets to the login Keychain with macOS `security`, without putting them on a command line.

Every write is read back and compared, so a truncated or failed write is an error, never a silent success.

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


PTY_MAX = 512  # longer values go through `security -i` on stdin (no terminal line limit)


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


def feed_stdin(cmd: list[str], line: str, timeout: float = 20.0) -> int:
    """Run cmd (security -i) with one command line on stdin. Returns the exit status."""
    r = subprocess.run(cmd, input=line + "\nquit\n", capture_output=True, text=True, timeout=timeout)
    return r.returncode


def store(account: str, value: str, service: str = SERVICE, runner=answer_prompts, stdin_runner=feed_stdin,
          run=subprocess.run) -> None:
    """Create or update (-U) the generic password `service`/`account`, then read it back and compare.

    Short values are typed at `security`'s own prompt through a pty. Longer ones (the base64 Graph key, a
    list of calendar URLs) go to `security -i` as a command on stdin, quoted. Either way nothing is on argv
    and /usr/bin/security creates the item. The read-back catches truncation or a silent failure."""
    if "\n" in value or "\r" in value:
        raise KeychainError(f"{account}: the value must be a single line")
    if len(value) <= PTY_MAX:
        code = runner(["security", "add-generic-password", "-U", "-s", service, "-a", account, "-w"], value)
    else:
        if '"' in value or "\\" in value:
            raise KeychainError(f"{account}: a value over {PTY_MAX} characters can't contain quotes or backslashes")
        code = stdin_runner(["security", "-i"],
                            f'add-generic-password -U -s "{service}" -a "{account}" -w "{value}"')
    if code != 0:
        raise KeychainError(f"could not store Keychain item {service}/{account} (security exit {code})")
    stored = read(account, service, run=run)
    if stored != value:
        detail = "missing" if stored is None else f"{len(stored)} of {len(value)} characters"
        raise KeychainError(f"Keychain item {service}/{account} did not read back correctly ({detail})")


def read(account: str, service: str, run=subprocess.run) -> str | None:
    r = run(["security", "find-generic-password", "-s", service, "-a", account, "-w"],
            capture_output=True, text=True)
    return r.stdout.rstrip("\n") if r.returncode == 0 else None
