from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_packaged_example_config_matches_the_root_copy():
    # Edit ./config.example.yaml, then: cp config.example.yaml kinchat/data/config.example.yaml
    assert (ROOT / "kinchat" / "data" / "config.example.yaml").read_text() == (ROOT / "config.example.yaml").read_text()
