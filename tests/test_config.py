import pytest

from kinchat import config as config_mod
from kinchat.config import system_timezone
from kinchat.settings import feature_rows, hour_label


def test_system_timezone_from_localtime_link(tmp_path):
    zone = tmp_path / "zoneinfo" / "Europe" / "London"
    zone.parent.mkdir(parents=True)
    zone.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(zone)
    assert system_timezone(str(link)) == "Europe/London"


def test_system_timezone_falls_back_to_tzname_then_utc(tmp_path, monkeypatch):
    missing = str(tmp_path / "nope")
    monkeypatch.setattr(config_mod.time, "tzname", ("Asia/Tokyo", "Asia/Tokyo"))
    assert system_timezone(missing) == "Asia/Tokyo"
    monkeypatch.setattr(config_mod.time, "tzname", ("MST", "MDT"))  # abbreviations, not IANA names
    assert system_timezone(missing) == "UTC"


def test_config_defaults_to_system_zone_and_us(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "system_timezone", lambda: "Europe/Berlin")
    p = tmp_path / "config.yaml"
    p.write_text("shadow_mode: true\n")
    cfg = config_mod.load(p)
    assert cfg.timezone == "Europe/Berlin" and cfg.default_country == "US"
    p.write_text("timezone: America/New_York\ndefault_country: gb\n")
    cfg = config_mod.load(p)
    assert cfg.timezone == "America/New_York" and cfg.default_country == "GB"


@pytest.mark.parametrize("yaml", ["default_country: FR\n", "timezone: Mars/Olympus\n", "tasks:\n  backend: pigeon\n",
                                  "tasks:\n  mail: fax\n", "chat:\n  transports: [carrier]\n"])
def test_config_rejects_bad_locale(tmp_path, yaml):
    p = tmp_path / "config.yaml"
    p.write_text(yaml)
    with pytest.raises(ValueError):
        config_mod.load(p)


def test_quiet_hours_label_follows_config():
    assert [hour_label(h) for h in (0, 7, 12, 22)] == ["12 AM", "7 AM", "12 PM", "10 PM"]
    default = dict((k, (label, off)) for k, label, _, off in feature_rows())
    assert default["quiet_hours"] == ("Quiet hours (10 PM–7 AM)", "Replies are held until 7 AM unless marked urgent")
    custom = dict((k, (label, off)) for k, label, _, off in feature_rows("Mia", 21, 6))
    assert custom["quiet_hours"] == ("Quiet hours (9 PM–6 AM)", "Replies are held until 6 AM unless marked urgent")


def test_example_config_loads():
    cfg = config_mod.load("config.example.yaml")
    assert (cfg.tasks.backend, cfg.tasks.mail, cfg.chat.transports) == ("mailbox", "graph", ["imessage"])
