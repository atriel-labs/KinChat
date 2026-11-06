import itertools
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as NS

import anthropic
import httpx2
import pytest
import yaml
from conftest import CHAT

from kinchat import config as config_mod
from kinchat.backends.managed_agents import (
    POST_TO_CHAT_TOOL,
    STATE_BEGIN,
    ManagedAgentsBackend,
    setup,
    write_state,
)
from kinchat.llm import TaskRequest
from kinchat.outbound import PostStatus

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


def bad_request(msg="budget"):
    return anthropic.BadRequestError(msg, response=httpx2.Response(400, request=httpx2.Request("POST", "https://x")),
                                     body=None)


class FakeEvents:
    def __init__(self, client):
        self.c = client
        self.sent = []  # (session_id, event)
        self.lists = []
        self.reject_messages: set[str] = set()

    def send(self, session_id, events):
        for e in events:
            if e["type"] == "user.message" and session_id in self.reject_messages:
                raise bad_request("only settle events are accepted at the budget")
            self.sent.append((session_id, e))

    def list(self, session_id, page=None, **params):
        self.lists.append((session_id, page, params))
        evs = self.c.events[session_id]
        if params.get("types"):
            evs = [e for e in evs if e.type in params["types"]]
        if params.get("order") == "desc":
            evs = list(reversed(evs))
        if params.get("created_at_gte"):
            gte = datetime.fromisoformat(params["created_at_gte"])
            evs = [e for e in evs if e.processed_at >= gte]
        start = int(page or 0)
        size = params.get("limit") or 2
        size = min(size, 2)  # small pages so pagination is exercised
        chunk = evs[start:start + size]
        nxt = str(start + size) if start + size < len(evs) else None
        return NS(data=chunk, next_page=nxt)


class FakeClient:
    def __init__(self):
        self.ids = itertools.count(1)
        self.events = {}
        self.created_sessions = []
        self.archived = []
        self.agents_created, self.agents_updated, self.envs = [], [], []
        ev = FakeEvents(self)
        sessions = NS(create=self._create_session, archive=self.archived.append, events=ev)
        agents = NS(create=self._create_agent, update=self._update_agent, retrieve=lambda aid: NS(id=aid, version=3))
        environments = NS(create=self._create_env, update=self._update_env)
        self.beta = NS(sessions=sessions, agents=agents, environments=environments)
        self._t = T0

    def _create_session(self, **params):
        sid = f"sesn_{next(self.ids)}"
        self.created_sessions.append((sid, params))
        self.events[sid] = []
        return NS(id=sid)

    def _create_agent(self, **d):
        self.agents_created.append(d)
        return NS(id="agent_1", version=1)

    def _update_agent(self, agent_id, version, **d):
        self.agents_updated.append((agent_id, version, d))
        return NS(id=agent_id, version=version + 1)

    def _create_env(self, **d):
        self.envs.append(("create", d))
        return NS(id="env_1")

    def _update_env(self, env_id, **d):
        self.envs.append(("update", env_id, d))
        return NS(id=env_id)

    # --- helpers to script the agent ---
    def emit(self, sid, type, **fields):
        self._t += timedelta(seconds=1)
        e = NS(id=f"sevt_{next(self.ids)}", type=type, processed_at=self._t, **fields)
        self.events[sid].append(e)
        return e

    def tool_call(self, sid, task_id="T-0001", message="Booked for 7pm!", status="done", name="post_to_chat"):
        return self.emit(sid, "agent.custom_tool_use", name=name,
                         input={"task_id": task_id, "message": message, "status": status})

    def results(self, sid=None):
        return [(s, e) for s, e in self.beta.sessions.events.sent
                if e["type"] == "user.custom_tool_result" and (sid is None or s == sid)]

    def messages(self, sid):
        return [e["content"][0]["text"] for s, e in self.beta.sessions.events.sent
                if s == sid and e["type"] == "user.message"]


class FakeOutbound:
    def __init__(self, status=PostStatus.SENT):
        self.status = status
        self.posts = []

    def __call__(self, chat, text, urgent=False):
        self.posts.append((chat, text))
        return self.status


@pytest.fixture
def ma_cfg(cfg):
    cfg.tasks.backend = "managed_agents"
    m = cfg.managed_agents
    m.agent_id, m.agent_version, m.environment_id = "agent_1", 2, "env_1"
    m.vault_ids = ["vlt_1"]
    m.budget_usd = 2.5
    return cfg


def make(ma_cfg, store, status=PostStatus.SENT, clock=None):
    client, out = FakeClient(), FakeOutbound(status)
    backend = ManagedAgentsBackend(ma_cfg, store, client, out, clock=clock or (lambda: 1000.0))
    return backend, client, out


def req(summary="Book dinner Sat 7pm for 4", update=None):
    return TaskRequest(summary=summary, details="usual spot", requested_by="model guess", deadline=None,
                       update_of_task_id=update)


def test_hand_off_creates_budgeted_session_with_vaults(ma_cfg, store):
    b, c, _ = make(ma_cfg, store)
    t = b.hand_off(req(), CHAT, "Alex")
    (sid, params), = c.created_sessions
    assert t.task_id == "T-0001" and t.session_id == sid and t.backend == "managed_agents"
    assert params["budget"] == {"type": "limit", "max_list_cost": {"amount": "250", "currency": "USD"}}
    assert params["vault_ids"] == ["vlt_1"] and params["environment_id"] == "env_1"
    assert params["agent"] == {"type": "agent", "id": "agent_1", "version": 2}
    (msg,) = c.messages(sid)
    assert '"task_id": "T-0001"' in msg and '"requested_by": "Alex"' in msg and "chat_context" in msg


def test_update_reuses_session(ma_cfg, store):
    b, c, _ = make(ma_cfg, store)
    b.hand_off(req(), CHAT, "Alex")
    t = b.hand_off(req("Make it 7:30", update="T-0001"), CHAT, "Alex")
    assert len(c.created_sessions) == 1 and t.summary == "Make it 7:30"
    msgs = c.messages(t.session_id)
    assert len(msgs) == 2 and msgs[1].startswith("Update to T-0001") and '"update_of_task_id": "T-0001"' in msgs[1]
    # another chat can't amend it
    other = b.hand_off(req("change it", update="T-0001"), "imessage:iMessage;-;+18005550101", "Sam")
    assert other.task_id == "T-0002" and len(c.created_sessions) == 2


def test_post_to_chat_accept_and_close(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.tool_call(sid, message="Booked for 7pm!", status="done")
    assert b.poll() == 1
    assert out.posts == [(CHAT, "Booked for 7pm!")]
    (_, result), = c.results(sid)
    assert result["content"][0]["text"] == "sent" and result["is_error"] is False
    assert store.get_task("T-0001").status == "done"
    c.tool_call(sid, message="again")  # after done the session is no longer polled
    assert b.poll() == 0 and len(c.results()) == 1


def test_post_to_chat_needs_input_keeps_polling(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.tool_call(sid, message="7 or 7:30?", status="needs_input")
    assert b.poll() == 1 and store.get_task("T-0001").status == "needs_input"
    c.tool_call(sid, message="Booked for 7:30.", status="done")
    assert b.poll() == 1 and [t for _, t in out.posts] == ["7 or 7:30?", "Booked for 7:30."]


@pytest.mark.parametrize("status,text,is_error", [
    (PostStatus.HELD, "held until quiet hours end", False),
    (PostStatus.REFUSED, "refused: the chat doesn't accept messages from the assistant right now", True),
    (PostStatus.SHADOW, "not sent: the bridge is in shadow mode (logged only)", False),
    (PostStatus.FAILED, "failed: the message could not be sent", True),
])
def test_post_to_chat_reports_post_status(ma_cfg, store, status, text, is_error):
    b, c, _ = make(ma_cfg, store, status=status)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.tool_call(sid, status="needs_input")
    b.poll()
    (_, result), = c.results(sid)
    assert (result["content"][0]["text"], result["is_error"]) == (text, is_error)


def test_post_to_chat_wrong_task_and_wrong_session(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    s1 = b.hand_off(req(), CHAT, "Alex").session_id
    s2 = b.hand_off(req("Buy milk"), CHAT, "Alex").session_id
    c.tool_call(s1, task_id="T-0002")      # T-0002 belongs to s2
    c.tool_call(s1, task_id="T-0099")      # no such task
    c.tool_call(s1, task_id="T-0001", status="maybe")
    c.tool_call(s1, name="send_email")
    b.poll()
    texts = [(e["content"][0]["text"], e["is_error"]) for _, e in c.results(s1)]
    assert texts == [("rejected: wrong session", True), ("rejected: task not open", True),
                     ("rejected: status must be one of done, needs_input, failed", True),
                     ("rejected: unknown tool send_email", True)]
    assert out.posts == [] and store.get_task("T-0002").status == "open" and s2


def test_message_truncated(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.tool_call(sid, message="x" * 900, status="needs_input")
    b.poll()
    assert len(out.posts[0][1]) == 600 and out.posts[0][1].endswith("…")


def test_event_id_dedup_and_cursor(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    for _ in range(3):
        c.emit(sid, "agent.message", content=[])
    c.tool_call(sid, message="one", status="needs_input")
    c.tool_call(sid, message="two", status="needs_input")
    assert b.poll() == 2
    assert b.poll() == 0 and [t for _, t in out.posts] == ["one", "two"] and len(c.results()) == 2
    last = c.beta.sessions.events.lists[-1][2]
    assert last["order"] == "asc" and "created_at_gte" in last  # resumes from the cursor
    assert any(page for _, page, _ in c.beta.sessions.events.lists)  # walked more than one page


def test_unattended_permission_asks_are_denied(ma_cfg, store):
    b, c, _ = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    e = c.emit(sid, "agent.tool_use", evaluated_permission="ask", name="bash")
    b.poll()
    assert (sid, {"type": "user.tool_confirmation", "tool_use_id": e.id, "result": "deny"}) in c.beta.sessions.events.sent


def test_budget_reached_notice_then_new_session(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.emit(sid, "session.status_idle", stop_reason=NS(type="budget_reached"))
    b.poll()
    b.poll()
    assert out.posts == [(CHAT, "Book dinner Sat 7pm for 4 hit its spending cap; reply 'continue' to start a fresh attempt")]
    assert store.get_task("T-0001").status == "paused_budget"
    t = b.hand_off(req("continue the booking", update="T-0001"), CHAT, "Alex")
    assert t.status == "open" and t.session_id != sid and len(c.created_sessions) == 2
    assert "continues an earlier attempt" in c.messages(t.session_id)[0]


def test_budget_400_on_update(ma_cfg, store):
    b, c, out = make(ma_cfg, store)
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.emit(sid, "session.status_idle", stop_reason=NS(type="budget_reached"))
    c.beta.sessions.events.reject_messages.add(sid)
    b.hand_off(req("Make it 7:30", update="T-0001"), CHAT, "Alex")
    assert store.get_task("T-0001").status == "paused_budget" and len(out.posts) == 1
    c.events[sid].clear()  # a 400 for another reason is not mistaken for the budget
    store.update_task("T-0001", status="open")
    with pytest.raises(anthropic.BadRequestError):
        b.hand_off(req("again", update="T-0001"), CHAT, "Alex")


def test_archive_after_done(ma_cfg, store):
    now = [1000.0]
    b, c, _ = make(ma_cfg, store, clock=lambda: now[0])
    sid = b.hand_off(req(), CHAT, "Alex").session_id
    c.tool_call(sid, status="done")
    b.poll()
    assert c.archived == []
    store._q("UPDATE tasks SET updated_ts = ?", (now[0] - 25 * 3600,))  # finished more than 24 h ago
    b.poll()
    b.poll()
    assert c.archived == [sid]  # archived once


def test_not_set_up_or_shadow(cfg, store):
    cfg.tasks.backend = "managed_agents"
    b = ManagedAgentsBackend(cfg, store, FakeClient(), FakeOutbound())
    assert b.health.state == "failed" and "kinchat agent setup" in b.health.status.last_error
    assert b.hand_off(req(), CHAT, "Alex") is None and b.poll() == 0


def test_setup_creates_then_updates_in_place(cfg):
    cfg.managed_agents.mcp_servers = [{"name": "notes", "url": "https://mcp.example.com/sse"}]
    c = FakeClient()
    state = setup(c, cfg)
    assert state == {"agent_id": "agent_1", "agent_version": 1, "environment_id": "env_1"}
    d = c.agents_created[0]
    assert POST_TO_CHAT_TOOL in d["tools"] and {"type": "mcp_toolset", "mcp_server_name": "notes"} in d["tools"]
    assert d["mcp_servers"] == [{"type": "url", "name": "notes", "url": "https://mcp.example.com/sse"}]
    assert "post_to_chat" in d["system"] and d["model"] == "claude-opus-5-5"
    cfg.managed_agents.agent_id, cfg.managed_agents.environment_id = "agent_1", "env_1"
    state = setup(c, cfg)
    assert len(c.agents_created) == 1 and c.agents_updated[0][:2] == ("agent_1", 3)  # optimistic version
    assert state["agent_version"] == 4 and c.envs[-1][0] == "update"


def test_state_written_into_config_and_loaded(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("# my comment\ntasks:\n  backend: managed_agents\n")
    write_state(p, {"agent_id": "agent_1", "agent_version": 1, "environment_id": "env_1"})
    write_state(p, {"agent_id": "agent_1", "agent_version": 2, "environment_id": "env_1"})
    text = p.read_text()
    assert text.startswith("# my comment") and text.count(STATE_BEGIN) == 1
    assert yaml.safe_load(text)["managed_agents_state"]["agent_version"] == 2
    ma = config_mod.load(p).managed_agents
    assert (ma.agent_id, ma.agent_version, ma.environment_id) == ("agent_1", 2, "env_1")


def test_bridge_builds_managed_agents_backend(no_secrets, bare_cfg, monkeypatch):
    from kinchat.app import Bridge
    bare_cfg.tasks.backend = "managed_agents"
    b = Bridge(bare_cfg)
    assert b.tasks.name == "managed_agents" and "anthropic-api-key" in b.tasks.health.status.last_error
    monkeypatch.setenv("KINCHAT_ANTHROPIC_API_KEY", "sk-test")
    bare_cfg.managed_agents.agent_id, bare_cfg.managed_agents.environment_id = "agent_1", "env_1"
    b = Bridge(bare_cfg)
    assert b.tasks.ready and b.tasks.health.state == "degraded"
    b._heartbeat()  # no heartbeat without the mailbox backend
