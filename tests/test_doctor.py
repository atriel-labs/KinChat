import plistlib
from types import SimpleNamespace as NS

import pytest

from kinchat import paths
from kinchat.config import Config
from kinchat.doctor import Deps, Doctor, report

SECRETS = {"anthropic-api-key": "sk-test", "dashboard-password": "pw", "graph-cert-key-b64": "cGVt",
           "imap-app-password": "app-pw", "ics-urls": "https://cal.example.com/PRIVATE-TOKEN.ics"}


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = NS(status_code=status)


class FakeGraph:
    def __init__(self, readable=("mia@example.com",)):
        self.readable = readable

    def _headers(self):
        return {"Authorization": "Bearer x"}

    def _get(self, url, params=None):
        user = url.split("/")[2]
        if user not in self.readable:
            raise HTTPError(403)
        return {"totalItemCount": 3}

    def calendar_snapshot(self, users, days):
        return "a@example.com:\n  (nothing scheduled)"


class FakeImap:
    def __init__(self, folders=("Sent",)):
        self.folders = folders

    def login(self, u, p):
        pass

    def select(self, mailbox, readonly=False):
        assert readonly
        return ("OK", [b"1"]) if mailbox.strip('"') in self.folders else ("NO", [b"no"])

    def logout(self):
        pass


class FakeSmtp:
    def __init__(self):
        self.sent = False

    def login(self, u, p):
        pass

    def send_message(self, m):
        self.sent = True

    def quit(self):
        pass


def fake_launchctl(loaded=()):
    def run(cmd, **kw):
        if cmd[1] == "print":
            return NS(returncode=0 if cmd[2].rsplit("/", 1)[1] in loaded else 113, stdout="", stderr="")
        return NS(returncode=0, stdout="", stderr="")
    return run


def fake_anthropic(agent_version=2, vaults=("vlt_1",)):
    def retrieve_vault(vid):
        if vid not in vaults:
            raise HTTPError(404)
        return NS(id=vid)
    beta = NS(agents=NS(retrieve=lambda aid: NS(id=aid, version=agent_version)),
              environments=NS(retrieve=lambda eid: NS(id=eid)), vaults=NS(retrieve=retrieve_vault))
    return lambda key: NS(models=NS(retrieve=lambda m: NS(id=m)), beta=beta)


@pytest.fixture
def cfg(tmp_path):
    c = Config()
    c.family_md = tmp_path / "family.md"
    c.family_md.write_text("# notes")
    g = c.graph
    g.tenant, g.client_id, g.cert_thumbprint, g.mailbox = "example.com", "id", "thumb", "mia@example.com"
    g.calendars = ["mia@example.com"]
    return c


def deps(tmp_path, **kw):
    base = dict(home=tmp_path / "home", secret=lambda a: SECRETS.get(a, ""), run=fake_launchctl(),
                chatdb=lambda path: NS(list_chats=lambda: [1, 2]), scriptable=lambda: 2, anthropic=fake_anthropic(),
                graph=lambda cfg, pem: FakeGraph(), imap=lambda c: FakeImap(), smtp=lambda c: FakeSmtp(),
                fetch=lambda url: "BEGIN:VCALENDAR\nEND:VCALENDAR\n", current_python=lambda: "/opt/python3")
    base.update(kw)
    return Deps(**base)


@pytest.fixture(autouse=True)
def secrets_env(monkeypatch):
    for account, value in SECRETS.items():
        monkeypatch.setenv("KINCHAT_" + account.upper().replace("-", "_"), value)


def by_name(results):
    return {r.name: r for r in results}


def test_default_config_all_good(cfg, tmp_path):
    d = Doctor(cfg, deps(tmp_path), probe_mailbox="someone@example.com")
    r = by_name(d.run())
    assert not d.failed
    assert r["graph scoped to the assistant's mailbox"].status == "ok" and "403" in r["graph scoped to the assistant's mailbox"].detail
    assert r["chat.db readable (Full Disk Access)"].detail == "2 chats"
    assert r["launchd agent"].status == "warn"  # not installed in the temp home


def test_second_mailbox_readable_fails_with_rbac_hint(cfg, tmp_path):
    d = Doctor(cfg, deps(tmp_path, graph=lambda c, p: FakeGraph(readable=("mia@example.com", "someone@example.com"))),
               probe_mailbox="someone@example.com")
    r = by_name(d.run())["graph scoped to the assistant's mailbox"]
    assert d.failed and r.status == "FAIL" and "RBAC" in r.hint


def test_probe_skipped_without_address(cfg, tmp_path):
    r = by_name(Doctor(cfg, deps(tmp_path)).run())["graph scoped to the assistant's mailbox"]
    assert r.status == "warn" and "--probe-mailbox" in r.hint


def test_missing_full_disk_access_and_key(cfg, tmp_path):
    def no_db(path):
        raise PermissionError("authorization denied")
    d = Doctor(cfg, deps(tmp_path, chatdb=no_db, secret=lambda a: "" if a == "anthropic-api-key" else "x"))
    r = by_name(d.run())
    assert r["chat.db readable (Full Disk Access)"].status == "FAIL" and "/opt/python3" in r["chat.db readable (Full Disk Access)"].hint
    assert r["keychain: anthropic-api-key"].status == "FAIL" and "kinchat setup" in r["keychain: anthropic-api-key"].hint


def test_imap_checks(cfg, tmp_path):
    cfg.tasks.mail, cfg.calendar.source = "imap", "none"
    cfg.imap.host, cfg.imap.username, cfg.imap.sent_folder = "imap.example.com", "a@example.com", "Sent Items"
    smtp = FakeSmtp()
    d = Doctor(cfg, deps(tmp_path, imap=lambda c: FakeImap(("Sent Items",)), smtp=lambda c: smtp))
    r = by_name(d.run())
    assert r["IMAP login + Sent folder"].status == "ok" and r["SMTP login"].status == "ok" and not smtp.sent
    assert "graph token" not in r  # Graph isn't used
    d = Doctor(cfg, deps(tmp_path, imap=lambda c: FakeImap(("Sent",))))
    assert by_name(d.run())["IMAP login + Sent folder"].status == "FAIL"


def test_ics_checks_never_show_the_url(cfg, tmp_path):
    cfg.calendar.source, cfg.tasks.backend = "ics", "none"
    out = []
    d = Doctor(cfg, deps(tmp_path))
    report(d.run(), out=out.append)
    text = "\n".join(out)
    assert "calendar feed 1: 0 events" in text and "PRIVATE-TOKEN" not in text
    assert "recurring events" in text


def test_ics_feed_error_hides_url(cfg, tmp_path):
    cfg.calendar.source, cfg.tasks.backend = "ics", "none"

    def boom(url):
        raise ConnectionError("network unreachable")
    out = []
    report(Doctor(cfg, deps(tmp_path, fetch=boom)).run(), out=out.append)
    text = "\n".join(out)
    assert "FAIL  calendar feed 1: ConnectionError" in text and "PRIVATE-TOKEN" not in text


def test_managed_agents_checks(cfg, tmp_path):
    cfg.tasks.backend, cfg.calendar.source = "managed_agents", "none"
    ma = cfg.managed_agents
    ma.agent_id, ma.agent_version, ma.environment_id, ma.vault_ids = "agent_1", 2, "env_1", ["vlt_1", "vlt_gone"]
    r = by_name(Doctor(cfg, deps(tmp_path, anthropic=fake_anthropic(agent_version=3))).run())
    assert r["managed agent"].status == "FAIL" and "version 3" in r["managed agent"].detail
    assert r["managed environment"].status == "ok"
    assert r["vault vlt_1"].status == "ok" and r["vault vlt_gone"].status == "FAIL"
    ma.agent_id = ""
    assert by_name(Doctor(cfg, deps(tmp_path)).run())["managed agent"].hint == "run `kinchat agent setup`"


def write_plist(home, python):
    p = paths.plist(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(plistlib.dumps({"Label": paths.LABEL, "ProgramArguments": [str(python), "-m", "kinchat", "run"]}))


def test_launchd_python_changed_warns(cfg, tmp_path):
    home = tmp_path / "home"
    old, new = tmp_path / "python3.12", tmp_path / "python3.13"
    for f in (old, new):
        f.write_text("")
    link = tmp_path / "venv-python"
    link.symlink_to(new)
    write_plist(home, link)
    paths.fda_record(home).parent.mkdir(parents=True)
    paths.fda_record(home).write_text(str(old) + "\n")
    r = by_name(Doctor(cfg, deps(tmp_path, run=fake_launchctl({paths.LABEL}))).run())
    assert r["Full Disk Access binary"].status == "warn" and str(new) in r["Full Disk Access binary"].hint
    paths.fda_record(home).write_text(str(new))
    r = by_name(Doctor(cfg, deps(tmp_path, run=fake_launchctl({paths.LABEL}))).run())
    assert r["Full Disk Access binary"].status == "ok" and r["launchd agent"].status == "ok"


def test_legacy_and_new_both_loaded_fails(cfg, tmp_path):
    d = Doctor(cfg, deps(tmp_path, run=fake_launchctl({paths.LABEL, paths.LEGACY_LABEL})))
    r = by_name(d.run())
    assert d.failed and r["only one bridge running"].status == "FAIL"
    r = by_name(Doctor(cfg, deps(tmp_path, run=fake_launchctl({paths.LEGACY_LABEL}))).run())
    assert r["launchd agent"].status == "warn" and "migrate" in r["launchd agent"].hint


def test_check_is_an_alias(cfg, tmp_path, monkeypatch):
    import yaml

    from kinchat import __main__ as cli
    calls = []
    monkeypatch.setattr(cli, "cmd_doctor", lambda c, probe: calls.append(probe) or 0)
    monkeypatch.setattr(cli, "_setup_logging", lambda v: None)
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump({"timezone": "UTC"}))
    assert cli.main(["check", "--config", str(f)]) == 0
    assert cli.main(["doctor", "--config", str(f), "--probe-mailbox", "x@example.com"]) == 0
    assert calls == [None, "x@example.com"]
