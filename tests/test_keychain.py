import pytest
from fakebin import FakeBin
from test_migrate import make_old_install

from kinchat import keychain, migrate, paths


@pytest.fixture
def fake(tmp_path, monkeypatch):
    fb = FakeBin(tmp_path / "fake")
    monkeypatch.setenv("PATH", fb.path_env())
    return fb


def test_short_value_via_prompt_and_read_back(fake):
    keychain.store("acct", "s3cret: with spaces", service="svc")
    assert fake.keychain()["svc/acct"] == "s3cret: with spaces"
    (call,) = [c for c in fake.calls() if c[1] == "add-generic-password"]
    assert call[-1] == "-w" and "s3cret" not in " ".join(call)


def test_long_value_via_stdin_and_read_back(fake):
    value = "QUJD" * 1024  # 4096 characters, like a base64 Graph certificate key
    keychain.store("graph-cert-key-b64", value, service="svc")
    assert fake.keychain()["svc/graph-cert-key-b64"] == value
    assert all(value not in " ".join(c) for c in fake.calls())  # not on any argv
    urls = " ".join(f"https://cal.example.com/{i:04d}.ics" for i in range(30))
    keychain.store("ics-urls", urls, service="svc")
    assert fake.keychain()["svc/ics-urls"] == urls


def test_exit_without_prompting_is_an_error(fake):
    fake.mode("exit45")
    with pytest.raises(keychain.KeychainError, match="exited without asking|exit 45"):
        keychain.store("acct", "value", service="svc")
    assert "svc/acct" not in fake.keychain()


def test_exit_1_after_prompts_is_an_error(fake):
    fake.mode("exit1_after")
    with pytest.raises(keychain.KeychainError, match="exit 1"):
        keychain.store("acct", "value", service="svc")


def test_truncated_write_is_caught_by_read_back(fake):
    fake.mode("truncate")
    with pytest.raises(keychain.KeychainError, match="did not read back correctly"):
        keychain.store("acct", "a-longer-value", service="svc")
    with pytest.raises(keychain.KeychainError, match="1000 of 4096"):
        keychain.store("graph-cert-key-b64", "QUJD" * 1024, service="svc")


def test_read_back_compare_without_security():
    """The verify logic on its own, with fake runners: no subprocess, any OS."""
    stored = {}

    def runner(cmd, value):
        stored[cmd[cmd.index("-a") + 1]] = value[:-1]  # drops the last character
        return 0

    def run(cmd, **kw):
        acct = cmd[cmd.index("-a") + 1]
        return type("R", (), {"returncode": 0 if acct in stored else 44, "stdout": stored.get(acct, "") + "\n"})()

    with pytest.raises(keychain.KeychainError, match="5 of 6 characters"):
        keychain.store("acct", "abcdef", runner=runner, run=run)
    assert keychain.read("acct", "kinchat", run=run) == "abcde"


def test_quotes_rejected_in_long_values(fake):
    with pytest.raises(keychain.KeychainError, match="quotes"):
        keychain.store("acct", '"' + "x" * 600, service="svc")


def test_migrate_stops_before_touching_anything_if_a_copy_does_not_verify(tmp_path, fake):
    home = tmp_path / "home"
    make_old_install(home, fake)
    fake.mode("truncate")
    with pytest.raises(migrate.MigrateError, match="did not read back correctly.*old agent is still running"):
        migrate.migrate(home, python="/opt/old/python3.12", out=lambda *a: None)
    assert fake.loaded() == {paths.LEGACY_LABEL}  # the old bridge was never stopped
    assert paths.legacy_plist(home).exists() and not paths.plist(home).exists()
    assert fake.keychain()["ai.atriel.mia-bridge/anthropic-api-key"] == "sk-old-key"  # old items untouched
