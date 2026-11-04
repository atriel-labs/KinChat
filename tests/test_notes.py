import re
import time
from zoneinfo import ZoneInfo

import pytest
from conftest import CHAT

from mia_bridge.dashboard import create_app
from mia_bridge.settings import NOTES_HEADING, Notes

BASE = "http://127.0.0.1:8770"


@pytest.fixture
def family_md(tmp_path):
    p = tmp_path / "family.md"
    p.write_text("# Family notes\n\n- Pat: (mom) call her Mom.\n")
    return p


@pytest.fixture
def notes(store, settings, family_md):
    return Notes(store, settings, family_md)


def test_suggest_dedups_against_pending_and_file(notes):
    assert notes.suggest("Jordan is allergic to shellfish.", CHAT, "Alex")
    assert not notes.suggest("jordan is allergic to  shellfish.", CHAT, "Alex")  # same, other spacing/case
    assert not notes.suggest("Pat: (mom) call her Mom.", CHAT, "Alex")          # already in family.md
    assert not notes.suggest("   ", CHAT, "Alex")
    assert [n.text for n in notes.pending()] == ["Jordan is allergic to shellfish."]


def test_approve_appends_under_heading_with_edits(notes, family_md, settings):
    notes.suggest("Jordan is allergic to shellfish.", CHAT, "Alex")
    n = notes.pending()[0]
    notes.approve(n.id, "Jordan is allergic to shellfish and lobster.")
    text = family_md.read_text()
    assert text.startswith("# Family notes")
    assert NOTES_HEADING in text and "- Jordan is allergic to shellfish and lobster. (added" in text
    assert notes.pending() == []
    assert any(a["item"] == "note approved" for a in settings.audit_log())
    # A second approval lands in the same section, before a heading the owner added afterwards.
    family_md.write_text(family_md.read_text() + "\n## Pets\n- Rufus the dog\n")
    notes.suggest("Max's birthday is May 4.", CHAT, "Pat")
    notes.approve(notes.pending()[0].id, "")
    text = family_md.read_text()
    assert text.count(NOTES_HEADING) == 1
    assert text.index("Max's birthday") < text.index("## Pets")


def test_reject_never_touches_file(notes, family_md):
    before = family_md.read_text()
    notes.suggest("Alex hates cilantro.", CHAT, "Jordan")
    notes.reject(notes.pending()[0].id)
    assert family_md.read_text() == before and notes.pending() == []
    assert notes.suggest("Alex hates cilantro.", CHAT, "Jordan")  # a rejected note can be suggested again


def test_unreviewed_notes_pruned_with_chat_history(notes, store):
    notes.suggest("Old suggestion.", CHAT, "Alex")
    store._q("UPDATE notes SET created_ts = ?", (time.time() - 40 * 86400,))
    store.prune_messages(30)
    assert notes.pending() == []


def test_dashboard_approve_and_reject(store, settings, notes, family_md):
    client = create_app(settings, store, "pw", ZoneInfo("America/Denver"), False, notes).test_client()
    client.post("/login", data={"password": "pw"}, base_url=BASE)
    page = client.get("/", base_url=BASE).get_data(as_text=True)
    csrf = re.search(r'name="csrf" value="([^"]+)"', page).group(1)
    notes.suggest("Jordan prefers window seats.", CHAT, "Alex")
    notes.suggest("Mia is great.", CHAT, "Alex")
    assert "Jordan prefers window seats." in client.get("/", base_url=BASE).get_data(as_text=True)
    first, second = notes.pending()
    client.post(f"/notes/{first.id}", data={"csrf": csrf, "action": "approve", "text": first.text}, base_url=BASE)
    client.post(f"/notes/{second.id}", data={"csrf": csrf, "action": "reject"}, base_url=BASE)
    assert "Jordan prefers window seats." in family_md.read_text()
    assert "Mia is great." not in family_md.read_text()
    assert client.post(f"/notes/{second.id}", data={"action": "approve"}, base_url=BASE).status_code == 403
