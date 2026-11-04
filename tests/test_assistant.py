import hashlib
import re
from zoneinfo import ZoneInfo

from conftest import CHAT, cls, incoming
from test_router import make
from test_tasks import desk, post_msg, req

from kinchat import config as config_mod
from kinchat.config import Assistant, Config
from kinchat.dashboard import create_app
from kinchat.llm import classifier_system, format_transcript, reply_system
from kinchat.router import CLOSING_RE, MIA_RE, SIT_OUT_ACK
from kinchat.settings import feature_rows
from kinchat.store import Message
from kinchat.tasks import POST_SUBJECT_RE, accept_result, post_subject_re

TZ = ZoneInfo("America/Denver")
BASE = "http://127.0.0.1:8770"


def sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


# --- defaults reproduce today's behaviour --------------------------------------

def test_default_prompts_are_unchanged():
    # Hashes of the prompts as rendered before the assistant became configurable.
    assert sha(classifier_system(["Alex", "Sam"])) == "43cc4f9a840ee0d8cd2638a10449626913c4ddb5709ed9538c5e3d9d81f4f32d"
    assert sha(reply_system(["Alex", "Sam"], "# notes")) == \
        "497cf8366afd5a241931e79910576b69c5439e4d9febbbf00796cef4855b618c"


def test_default_strings():
    a = Config().assistant
    assert (a.name, a.tag, a.aliases, a.handles) == ("Mia", "MIA", [], [])
    m = Message("g", CHAT, None, "Booked!", 0, True)
    assert format_transcript([m], TZ).endswith("] Mia: Booked!")
    assert MIA_RE.search("Mia, are we free?") and not MIA_RE.search("Ask Amiable")
    assert CLOSING_RE.match("thanks Mia!")
    assert POST_SUBJECT_RE.match("RE: [MIA-POST] T-0001")
    assert feature_rows()[0][1] == "Mia enabled (master switch)"


def test_default_dashboard_title(store, settings):
    page = create_app(settings, store, "pw", TZ, False).test_client().get("/login", base_url=BASE).get_data(True)
    assert "<title>Mia bridge</title>" in page


# --- a custom assistant -------------------------------------------------------

def robin(cfg):
    cfg.assistant = Assistant(name="Robin", aliases=["Rob"], owner="Pat", tag="ROBIN", handles=["robin@example.com"])
    return cfg


def test_custom_name_and_alias_are_cues(cfg, store, settings):
    r, _ = make(robin(cfg), store, settings, cls(route=False, conf=0.1))
    d = r.route(incoming("Robin, are we free Saturday?"))
    assert d.respond and d.reason == "mentions Robin"
    store.close_window(CHAT)
    assert r.route(incoming("rob are we free Sunday?")).respond
    store.close_window(CHAT)
    assert not r.route(incoming("Robert, are we free Monday?")).respond
    assert not r.route(incoming("Mia, are we free Tuesday?")).respond


def test_custom_name_controls_and_closing(cfg, store, settings):
    r, _ = make(robin(cfg), store, settings, cls())
    assert r.route(incoming("Rob, sit this one out")).ack == SIT_OUT_ACK
    assert r.route(incoming("Robin you're back")).respond
    r.route(incoming("Robin, what's for dinner?"))
    assert not r.route(incoming("thanks Rob!")).respond
    assert not store.window_active(CHAT)


def test_own_messages_use_custom_name(cfg, store, settings):
    r, _ = make(robin(cfg), store, settings, cls())
    r.route(incoming("On it", from_me=True, guid="own-1"))
    assert store.recent_messages(CHAT, 1)[0].sender == "Robin"


def test_custom_handles_are_not_strangers(cfg, store, settings):
    r, _ = make(robin(cfg), store, settings, cls(), members=("+13035550100", "+13035550101", "Robin@Example.com"))
    assert r.route(incoming("Robin, hi")).respond


def test_names_are_regex_escaped(cfg, store, settings):
    cfg.assistant = Assistant(name="R.J.")
    r, _ = make(cfg, store, settings, cls(route=False, conf=0.1))
    assert not r.route(incoming("RxJx, hi")).respond
    assert r.route(incoming("R.J. are we free?")).respond


def test_custom_prompts_and_transcript():
    c = classifier_system(["Alex"], "Robin")
    r = reply_system(["Alex"], "", "Robin", "Pat")
    assert "Mia" not in c and "Robin" in c and "route_to_mia" in c
    assert "Mia" not in r and "You are Robin" in r and "Pat reviews each one" in r
    m = Message("g", CHAT, None, "Booked!", 0, True)
    assert format_transcript([m], TZ, "Robin").endswith("] Robin: Booked!")


def test_custom_tag_in_task_mail(cfg, store):
    sent = []
    desk(robin(cfg), store, sent).hand_off(req(), CHAT, "Alex")
    assert sent[0][0].startswith("[ROBIN-CHAT] T-0001:")
    assert post_subject_re("ROBIN").match("Fwd: [ROBIN-POST] T-0001")
    assert accept_result(post_msg(), "mia@example.com", store, tag="ROBIN") is None  # [MIA-POST] is not ours
    msg = post_msg()
    msg["subject"] = "RE: [ROBIN-POST] T-0001"
    assert accept_result(msg, "mia@example.com", store, tag="ROBIN").task_id == "T-0001"


def test_custom_dashboard(store, settings):
    client = create_app(settings, store, "pw", TZ, False, assistant="Robin").test_client()
    client.post("/login", data={"password": "pw"}, base_url=BASE)
    page = client.get("/", base_url=BASE).get_data(as_text=True)
    assert "<title>Robin bridge</title>" in page and "Robin enabled (master switch)" in page
    assert not re.search(r"\bMia\b", page)


# --- config file ------------------------------------------------------------

def test_config_assistant_section(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("assistant:\n  name: Robin\n  aliases: [Rob]\n  tag: ROBIN\n  handles: [robin@example.com]\n")
    a = config_mod.load(p).assistant
    assert (a.name, a.aliases, a.tag, a.handles, a.owner) == ("Robin", ["Rob"], "ROBIN", ["robin@example.com"],
                                                             "the family organizer")
    assert a.names == ["Robin", "Rob"]


def test_config_without_handles_falls_back_to_mailbox(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("graph:\n  mailbox: mia@example.com\n")
    assert config_mod.load(p).assistant.handles == ["mia@example.com"]
