import pytest

from kinchat import secrets


@pytest.fixture
def keychain(monkeypatch):
    items = {}
    monkeypatch.setattr(secrets, "_keychain", lambda service, account: items.get((service, account)))
    for var in ("KINCHAT_DASHBOARD_PASSWORD", "MIA_BRIDGE_DASHBOARD_PASSWORD"):
        monkeypatch.delenv(var, raising=False)
    return items


def test_env_var_wins_then_legacy_prefix(keychain, monkeypatch):
    keychain[("kinchat", "dashboard-password")] = "from-keychain"
    monkeypatch.setenv("MIA_BRIDGE_DASHBOARD_PASSWORD", "legacy-env")
    assert secrets.get("dashboard-password") == "legacy-env"
    monkeypatch.setenv("KINCHAT_DASHBOARD_PASSWORD", "env")
    assert secrets.get("dashboard-password") == "env"


def test_keychain_service_then_legacy_service(keychain):
    keychain[("ai.atriel.mia-bridge", "dashboard-password")] = "old"
    assert secrets.get("dashboard-password") == "old"
    keychain[("kinchat", "dashboard-password")] = "new"
    assert secrets.get("dashboard-password") == "new"


def test_missing_secret_names_current_service(keychain):
    with pytest.raises(RuntimeError, match="kinchat/dashboard-password"):
        secrets.get("dashboard-password")
