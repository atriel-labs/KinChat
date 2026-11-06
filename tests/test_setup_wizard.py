import itertools
import plistlib

import pytest
import yaml
from fakebin import FakeBin

from kinchat import config as config_mod
from kinchat import paths
from kinchat import setup_wizard as sw

SECRETS = {
    "anthropic-api-key": "sk-ant-TESTSECRET-0001",
    "dashboard-password": "dash-TESTSECRET-0002",
    "graph-cert-key-b64": "cGVtLVRFU1RTRUNSRVQtMDAwMw==",
    "imap-app-password": "imap-TESTSECRET-0004",
    "ics-urls": "https://cal.example.com/TESTSECRET-0005.ics https://cal.example.com/TESTSECRET-0006.ics",
}


def answers(tasks="none", calendar="none", **extra):
    a = {
        "assistant_name": "Robin", "aliases": ["Rob"], "owner": "Pat", "handles": ["robin@example.com"],
        "country": "gb", "timezone": "Europe/London", "transports": ["imessage"],
        "tasks": tasks, "calendar": calendar,
        "graph_tenant": "example.com", "graph_client_id": "00000000-0000-0000-0000-000000000000",
        "graph_cert_thumbprint": "ABCDEF", "graph_mailbox": "robin@example.com",
        "imap_host": "imap.example.com", "imap_username": "robin@example.com", "imap_smtp_host": "smtp.example.com",
        "calendar_owner_emails": ["pat@example.com"],
        "open_settings": False, "run_agent_setup": False,
        "secrets": dict(SECRETS),
    }
    a.update(extra)
    return a


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fb = FakeBin(tmp_path / "fake")
    monkeypatch.setenv("PATH", fb.path_env())
    return fb


def run(tmp_path, a, install=False, out=None):
    lines = out if out is not None else []
    p = sw.AnswersPrompter(a, out=lambda *x: lines.append(" ".join(map(str, x))))
    cfg = tmp_path / "home" / "Library" / "Application Support" / "kinchat" / "config.yaml"
    sw.run(p, cfg, home=tmp_path / "home", install=install, opener=lambda cmd: None)
    return cfg, lines


@pytest.mark.parametrize("tasks,calendar", list(itertools.product(sw.TASK_CHOICES, sw.CALENDAR_CHOICES)))
def test_every_backend_combination_loads(tmp_path, fake, tasks, calendar):
    cfg_path, _ = run(tmp_path, answers(tasks, calendar))
    cfg = config_mod.load(cfg_path)
    assert (cfg.assistant.name, cfg.assistant.aliases, cfg.assistant.tag) == ("Robin", ["Rob"], "ROBIN")
    assert (cfg.default_country, cfg.timezone, cfg.shadow_mode) == ("GB", "Europe/London", True)
    assert cfg.tasks.backend == ("mailbox" if tasks.startswith("mailbox") else tasks)
    assert cfg.calendar.source == calendar
    raw = yaml.safe_load(cfg_path.read_text())
    assert ("graph" in raw) == (tasks == "mailbox+graph" or calendar == "graph")
    assert ("imap" in raw) == (tasks == "mailbox+imap")
    assert ("managed_agents" in raw) == (tasks == "managed_agents")
    needed = {"anthropic-api-key", "dashboard-password"}
    needed |= {"graph-cert-key-b64"} if "graph" in raw else set()
    needed |= {"imap-app-password"} if "imap" in raw else set()
    needed |= {"ics-urls"} if calendar == "ics" else set()
    assert {k.split("/", 1)[1] for k in fake.keychain()} == needed  # only the secrets these backends use


def test_secrets_reach_keychain_and_nowhere_else(tmp_path, fake):
    cfg_path, lines = run(tmp_path, answers("mailbox+imap", "ics"), install=True)
    kc = fake.keychain()
    assert kc["kinchat/anthropic-api-key"] == SECRETS["anthropic-api-key"]
    assert kc["kinchat/ics-urls"] == SECRETS["ics-urls"].replace(" ", "\n")  # one URL per line
    adds = [c for c in fake.calls("security") if c[1] == "add-generic-password"]
    assert adds and all(c[2:6] == ["-U", "-s", "kinchat", "-a"] for c in adds)
    output = "\n".join(lines)
    written = "\n".join(p.read_text(errors="ignore") for p in (tmp_path / "home").rglob("*") if p.is_file())
    for value in SECRETS.values():
        for piece in value.split():
            assert piece not in output and piece not in written


def test_existing_config_is_diffed_and_backed_up(tmp_path, fake):
    cfg_path, _ = run(tmp_path, answers())
    original = cfg_path.read_text()
    _, lines = run(tmp_path, answers(assistant_name="Robyn", overwrite=False))
    assert cfg_path.read_text() == original and "Left the existing config unchanged." in lines
    assert any("-  name: Robin" in line and "+  name: Robyn" in line for line in lines)  # the diff was shown
    run(tmp_path, answers(assistant_name="Robyn", overwrite=True))
    assert "name: Robyn" in cfg_path.read_text()
    assert cfg_path.with_name("config.yaml.bak").read_text() == original


def test_existing_secret_kept_unless_replaced(tmp_path, fake):
    fake.set_secret("kinchat", "anthropic-api-key", "already-there")
    run(tmp_path, answers())
    assert fake.keychain()["kinchat/anthropic-api-key"] == "already-there"
    run(tmp_path, answers(**{"replace_anthropic-api-key": True}))
    assert fake.keychain()["kinchat/anthropic-api-key"] == SECRETS["anthropic-api-key"]


def test_install_writes_plist_and_loads_it(tmp_path, fake):
    home = tmp_path / "home"
    legacy = paths.legacy_plist(home)
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(plistlib.dumps({"Label": paths.LEGACY_LABEL, "ProgramArguments": ["/old/python"]}))
    fake.load(paths.LEGACY_LABEL)
    _, lines = run(tmp_path, answers(), install=True)
    with open(paths.plist(home), "rb") as f:
        pl = plistlib.load(f)
    assert pl["Label"] == "io.kinchat.bridge" and pl["ProgramArguments"][1:] == ["-m", "kinchat", "run"]
    assert fake.loaded() == {"io.kinchat.bridge"}  # the old agent was booted out
    assert not legacy.exists() and legacy.with_name(legacy.name + ".disabled").exists()
    assert paths.fda_record(home).read_text().strip() in "\n".join(lines)  # the binary needing Full Disk Access


def test_comments_come_from_config_example(tmp_path, fake):
    cfg_path, _ = run(tmp_path, answers("managed_agents", "ics"))
    text = cfg_path.read_text()
    comments = sw.example_comments()
    for key in ("assistant", "managed_agents"):
        assert comments[key] in text
    assert "# Only for tasks.backend: managed_agents" in comments["managed_agents"]


def test_bad_answers_are_reported(tmp_path, fake):
    with pytest.raises(sw.SetupError, match="tasks must be one of"):
        run(tmp_path, answers(tasks="pigeon"))
    with pytest.raises(sw.SetupError, match="timezone"):
        run(tmp_path, answers(timezone="Mars/Olympus"))
    a = answers()
    a["secrets"].pop("dashboard-password")
    with pytest.raises(sw.SetupError, match="no secret for 'dashboard-password'"):
        run(tmp_path, a)


def test_cli_answers_file(tmp_path, fake, monkeypatch):
    from kinchat.__main__ import main
    af = tmp_path / "answers.yaml"
    af.write_text(yaml.safe_dump(answers()))
    cfg = tmp_path / "out" / "config.yaml"
    monkeypatch.setattr(sw.sys, "platform", "linux")
    assert main(["setup", "--answers", str(af), "--config", str(cfg), "--no-install"]) == 0
    assert config_mod.load(cfg).assistant.name == "Robin"
    assert main(["setup", "--answers", str(tmp_path / "missing.yaml"), "--config", str(cfg)]) == 2
