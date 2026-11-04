import json

from conftest import CHAT

from mia_bridge.llm import TaskRequest
from mia_bridge.tasks import ResultPoller, TaskDesk, accept_result, parse_result_body

MIA = "mia@example.com"


def post_msg(task_id="T-0001", sender=MIA, internal=True, body="Booked for 7pm!\nSTATUS: done", mid="g1"):
    headers = [{"name": "X-MS-Exchange-Organization-AuthAs", "value": "Internal"}] if internal else []
    addr = {"emailAddress": {"address": sender}}
    return {"id": mid, "subject": f"RE: [MIA-POST] {task_id}", "from": addr, "sender": addr,
            "internetMessageHeaders": headers, "uniqueBody": {"content": body}}


class FakeGraph:
    mailbox = MIA

    def __init__(self, msgs):
        self.msgs = {m["id"]: m for m in msgs}

    def inbox_delta(self, link, since):
        return [{"id": k, "subject": m["subject"]} for k, m in self.msgs.items()], "delta-2"

    def get_message_for_post(self, mid):
        return self.msgs[mid]


def desk(cfg, store, sent):
    return TaskDesk(cfg, store, lambda subj, body: sent.append((subj, body)) or "<id@x>")


def req(summary="Book dinner Sat 7pm for 4", update=None):
    return TaskRequest(summary=summary, details="usual spot", requested_by="model guess", deadline=None,
                       update_of_task_id=update)


def payload(body):
    return json.loads(body.split("```json\n")[1].split("```")[0])


def test_hand_off_sends_tagged_email(cfg, store):  # acceptance 4
    sent = []
    t = desk(cfg, store, sent).hand_off(req(), CHAT, "Alex")
    assert t.task_id == "T-0001" and t.status == "open" and t.chat_guid == CHAT and t.requested_by == "Alex"
    subj, body = sent[0]
    assert subj == "[MIA-CHAT] T-0001: Book dinner Sat 7pm for 4"
    p = payload(body)
    assert p["task_id"] == "T-0001" and p["update_of_task_id"] is None and p["requested_by"] == "Alex"


def test_update_reuses_task_id(cfg, store):  # acceptance 7
    sent = []
    d = desk(cfg, store, sent)
    d.hand_off(req(), CHAT, "Alex")
    t = d.hand_off(req("Move dinner to 7:30", update="T-0001"), CHAT, "Alex")
    assert t.task_id == "T-0001" and t.summary == "Move dinner to 7:30"
    assert payload(sent[1][1])["update_of_task_id"] == "T-0001"


def test_update_cannot_cross_chats(cfg, store):
    sent = []
    d = desk(cfg, store, sent)
    d.hand_off(req(), CHAT, "Alex")
    t = d.hand_off(req("change it", update="T-0001"), "iMessage;-;+13035550101", "Sam")
    assert t.task_id == "T-0002"


def test_shadow_mode_sends_no_email(cfg, store):
    cfg.shadow_mode = True
    sent = []
    assert desk(cfg, store, sent).hand_off(req(), CHAT, "Alex") is None and sent == []


def test_parse_body_strips_notes_and_status():
    text, status = parse_result_body("Booked at 7:30!\n\nSTATUS: done\n---\ninternal notes")
    assert text == "Booked at 7:30!" and status == "done"
    text, status = parse_result_body("x" * 900)
    assert len(text) == 600 and status is None


def test_acceptance_rules(cfg, store):  # acceptance 6
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    assert accept_result(post_msg(), MIA, store).status == "done"
    assert accept_result(post_msg(sender="evil@example.com"), MIA, store) is None
    assert accept_result(post_msg(internal=False), MIA, store) is None
    assert accept_result(post_msg(task_id="T-0099"), MIA, store) is None
    store.mark_posted("g1", "T-0001")
    assert accept_result(post_msg(), MIA, store) is None


def test_result_posted_once_to_task_chat(cfg, store):  # acceptance 5
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    posts = []
    p = ResultPoller(cfg, store, FakeGraph([post_msg()]), lambda chat, text: posts.append((chat, text)))
    assert p.poll_once() == 1
    assert p.poll_once() == 0
    assert posts == [(CHAT, "Booked for 7pm!")]
    assert store.get_task("T-0001").status == "done"
    assert p.last_ok is not None


def test_needs_input_keeps_task_open(cfg, store):
    store.create_task("T-0001", CHAT, "s", None, "Alex")
    posts = []
    ResultPoller(cfg, store, FakeGraph([post_msg(body="7 or 7:30?\nSTATUS: needs_input")]),
                 lambda c, t: posts.append(t)).poll_once()
    assert store.task_is_open("T-0001") and posts == ["7 or 7:30?"]
