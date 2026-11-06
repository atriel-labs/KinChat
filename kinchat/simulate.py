"""`kinchat simulate`: chat with the assistant in the terminal, with no Mac, Messages or mailbox needed.

It runs the real router and replier (Claude API calls: an Anthropic API key is required) over the in-memory
fake transport. State lives in a temporary state.db that is thrown away at the end. The fake family is
Alex, Sam and Jordan in an approved group; task hand-offs are echoed instead of sent, and the calendar is a
small made-up snapshot.

Commands: /as <name> (switch sender), /dm (one-on-one with the sender), /group, /why (the last routing
decision), /help, /quit. `--script file` reads lines from a file instead and prints each decision.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta
from pathlib import Path

from .backends.fake import FakeTransport
from .config import Config
from .health import Health
from .llm import TaskRequest
from .store import Task

FAMILY = {"Alex": "+18005550100", "Sam": "+18005550101", "Jordan": "+18005550102"}
GROUP = "family"
FAMILY_MD = """# Family notes (simulated)

## People
- Alex: likes to be called Al.
- Sam: vegetarian.
- Jordan: 12, swim practice on Tuesdays.

## Standing preferences
- Dinner reservations: party of 3 unless told otherwise; prefer 6:30-7:30pm.
"""
HELP = "commands: /as <Alex|Sam|Jordan>, /dm, /group, /why, /help, /quit"


class SimulationError(RuntimeError):
    pass


def fake_calendar(now: datetime) -> str:
    day = lambda n, h, m=0: (now + timedelta(days=n)).replace(hour=h, minute=m)  # noqa: E731
    fmt = lambda d: d.strftime("%a %b %d %H:%M")  # noqa: E731
    return ("family@example.com:\n"
            f"  {fmt(day(1, 17))}-18:00: Swim practice @ Example Pool [busy]\n"
            f"  {fmt(day(2, 19))}-21:00: Dinner with friends [busy]\n"
            f"  {(now + timedelta(days=4)).strftime('%a %b %d')} (all day): School holiday [free]")


class EchoTasks:
    """A task backend that shows the hand-off instead of sending it."""

    name = "simulated"
    accepts_tasks = True

    def __init__(self, store, out):
        self.store, self.out = store, out
        self.health = Health("tasks", "Tasks (simulated)")
        self.health.ok()

    def hand_off(self, req: TaskRequest, chat: str, requested_by: str) -> Task | None:
        existing = self.store.get_task(req.update_of_task_id.upper()) if req.update_of_task_id else None
        if existing and existing.chat_guid == chat:
            self.store.update_task(existing.task_id, summary=req.summary, details=req.details)
            task_id = existing.task_id
        else:
            task_id = self.store.next_task_id()
            self.store.create_task(task_id, chat, req.summary, req.details, requested_by, backend=self.name)
        self.out(f"  [task handed off: {task_id} {req.summary} (for {requested_by})]")
        return self.store.get_task(task_id)

    def poll(self) -> int:
        return 0

    def close(self, task: Task) -> None:
        pass


class Simulator:
    def __init__(self, base: Config | None = None, llm=None, out=print, workdir: Path | None = None):
        from .app import Bridge

        self.out = out
        self._tmp = None if workdir else tempfile.TemporaryDirectory(prefix="kinchat-sim-")
        work = Path(workdir or self._tmp.name)
        cfg = base or Config()
        cfg.shadow_mode = False
        cfg.chat.transports = ["fake"]
        cfg.tasks.backend = "none"
        cfg.calendar.source = "none"
        cfg.db_path = work / "state.db"
        cfg.family_md = work / "family.md"
        cfg.family_md.write_text(FAMILY_MD)
        self.cfg = cfg
        self.bridge = Bridge(cfg)
        if llm is not None:
            self.bridge.llm = llm
        if self.bridge.llm is None:
            self.close()
            raise SimulationError(
                "kinchat simulate runs the real router and replier, so it needs an Anthropic API key: set "
                "KINCHAT_ANTHROPIC_API_KEY or store it with `security add-generic-password -U -s kinchat "
                "-a anthropic-api-key -w '...'`.")
        b = self.bridge
        self.fake: FakeTransport = b.transports.get("fake")
        b.tasks = EchoTasks(b.store, out)
        b.calendar = fake_calendar(datetime.now(cfg.tz))
        s = b.settings
        for name, handle in FAMILY.items():
            s.add_contact(name, [handle], one_on_one=True, can_assign=True, actor="simulate")
        group = self.fake.add_group(GROUP, list(FAMILY.values()))
        s.register_pending(group, "Family", list(FAMILY.values()))
        s.set_chat_mode(group, "respond", actor="simulate")
        s.set_feature("quiet_hours", False, actor="simulate")  # always answer, whatever the time
        s.set_feature("one_on_one", True, actor="simulate")
        self.sender = "Alex"
        self.dm = False
        self.last_guid: str | None = None

    def close(self) -> None:
        if self._tmp:
            self._tmp.cleanup()
            self._tmp = None

    @property
    def chat_native(self) -> str:
        return f"dm-{self.sender.lower()}" if self.dm else GROUP

    @property
    def prompt(self) -> str:
        return f"[{self.sender}{' -> ' + self.cfg.assistant.name if self.dm else ' @ family'}] > "

    # --- input ----------------------------------------------------------------------

    def handle_line(self, line: str) -> bool:
        """Process one line; returns False on /quit."""
        line = line.strip()
        if not line:
            return True
        if line.startswith("/"):
            return self._command(line)
        self.say(line)
        return True

    def _command(self, line: str) -> bool:
        cmd, _, arg = line.partition(" ")
        cmd = cmd.lower()
        if cmd in ("/quit", "/exit"):
            return False
        if cmd == "/as":
            name = next((n for n in FAMILY if n.lower() == arg.strip().lower()), None)
            if name is None:
                self.out(f"  unknown person; one of {', '.join(FAMILY)}")
            else:
                self.sender = name
                self.out(f"  now speaking as {name}")
        elif cmd == "/dm":
            self.dm = True
            self.out(f"  one-on-one chat between {self.sender} and {self.cfg.assistant.name}")
        elif cmd == "/group":
            self.dm = False
            self.out("  back in the family group")
        elif cmd == "/why":
            self.out("  " + self.why())
        elif cmd == "/help":
            self.out("  " + HELP)
        else:
            self.out(f"  unknown command; {HELP}")
        return True

    def why(self) -> str:
        if not self.last_guid:
            return "nothing sent yet"
        rows = self.bridge.store.decisions_for(self.last_guid)
        if not rows:
            return "no routing decision was logged (e.g. the message was filtered before routing)"
        d = rows[-1]
        cls = ""
        if d["confidence"] is not None:
            verdict = "for" if d["cls_route"] else "not for"
            cls = f"; classifier: {verdict} {self.cfg.assistant.name}, {d['intent']}, {d['confidence']:.2f}"
        return f"{d['step']} -> {d['route']}: {d['reason']}{cls}"

    def say(self, text: str) -> list[str]:
        """Send `text` as the current sender; prints and returns the assistant's replies."""
        b = self.bridge
        before = len(self.fake.sent)
        inc = self.fake.deliver(self.chat_native, FAMILY[self.sender], text)
        self.last_guid = inc.guid
        while b.poll_once(self.fake):  # our replies come back as echoes; keep going until it's quiet
            pass
        b.outbound.tick()
        while b.poll_once(self.fake):
            pass
        replies = [t for _, t in self.fake.sent[before:]]
        for r in replies:
            self.out(f"{self.cfg.assistant.name}: {r}")
        return replies

    # --- modes ----------------------------------------------------------------------

    def interactive(self, inp=input) -> None:
        self.out(f"Simulated family chat with {self.cfg.assistant.name}. Family: {', '.join(FAMILY)}. {HELP}")
        while True:
            try:
                line = inp(self.prompt)
            except EOFError:
                break
            if not self.handle_line(line):
                break

    def script(self, lines) -> None:
        for raw in lines:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if line.startswith("/"):
                self.out(f"{line}")
                self.handle_line(line)
                continue
            self.out(f"> {self.prompt.strip()[:-1].strip()} {line}")
            replies = self.say(line)
            self.out(f"  · {self.why()}")
            if not replies:
                self.out("  (no reply)")
