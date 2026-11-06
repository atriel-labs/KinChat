import pytest

from kinchat.llm import Classification, Reply, TaskRequest
from kinchat.simulate import SimulationError, Simulator


class FakeLLM:
    """Routes anything that mentions dinner to the assistant; replies with a task when asked to book."""

    def __init__(self):
        self.replies = 0

    def classify(self, history, newest, tasks, names, tz):
        for_us = "dinner" in newest.text.lower()
        return Classification(route_to_mia=for_us, confidence=0.9, intent="question" if for_us else "not_for_mia",
                              action_needed=False, task_summary=None, reason="talks about dinner" if for_us else "chat",
                              close_window=False)

    def reply(self, history, tasks, calendar, family_md, cls, names, can_hand_off, tz):
        self.replies += 1
        last = history[-1].text.lower()
        task = None
        if "book" in last:
            task = TaskRequest(summary="Book dinner for 3 on Saturday", details="", requested_by="?", deadline=None,
                               update_of_task_id=None)
        assert "Swim practice" in calendar and "Jordan" in family_md  # the fake calendar and family notes
        return Reply(message="On it." if task else "You're free Saturday evening.", task=task)


@pytest.fixture
def sim(no_secrets):
    out = []
    s = Simulator(llm=FakeLLM(), out=out.append)
    s.output = out
    yield s
    s.close()


def test_mention_gets_a_reply_and_why_explains(sim):
    assert sim.say("Mia, are we free Saturday?") == ["You're free Saturday evening."]
    assert sim.why().startswith("cue -> respond: mentions Mia")
    assert sim.say("we should get the car washed") == []  # the window is open, but the classifier says no
    assert "not for Mia" in sim.why()


def test_switch_sender_and_dm(sim):
    sim.handle_line("/as sam")
    assert sim.sender == "Sam"
    sim.handle_line("/dm")
    assert sim.say("what's for dinner?") == ["You're free Saturday evening."]
    assert sim.why().startswith("cue -> respond: direct message")
    sim.handle_line("/group")
    assert sim.chat_native == "family"
    sim.handle_line("/as Nobody")
    assert "unknown person" in sim.output[-1]


def test_task_handoff_is_echoed(sim):
    assert sim.say("Mia, can you book dinner?") == ["On it."]
    assert any(line.startswith("  [task handed off: T-0001 Book dinner for 3 on Saturday (for Alex)]")
               for line in sim.output)


def test_script_mode(no_secrets):
    out = []
    s = Simulator(llm=FakeLLM(), out=out.append)
    s.script(["# comment", "Mia, dinner Saturday?", "/as Jordan", "ok cool"])
    s.close()
    text = "\n".join(out)
    assert "> [Alex @ family] Mia, dinner Saturday?" in text and "Mia: You're free Saturday evening." in text
    assert "  · cue -> respond: mentions Mia" in text
    assert "> [Jordan @ family] ok cool" in text and "(no reply)" in text


def test_requires_an_api_key(no_secrets):
    with pytest.raises(SimulationError, match="needs an Anthropic API key"):
        Simulator(out=lambda *a: None)


def test_cli_without_key(no_secrets, tmp_path, capsys):
    from kinchat.__main__ import main
    assert main(["simulate", "--config", str(tmp_path / "none.yaml")]) == 2
    assert "KINCHAT_ANTHROPIC_API_KEY" in capsys.readouterr().err
