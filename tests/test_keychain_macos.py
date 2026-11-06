"""Against the real macOS `security`, in a throwaway keychain. Runs only where KINCHAT_KEYCHAIN_TEST=1 (CI's
macOS runners): it briefly makes the throwaway keychain the default, which you don't want on your own Mac."""
import os
import subprocess
import sys

import pytest

from kinchat import keychain

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or os.environ.get("KINCHAT_KEYCHAIN_TEST") != "1",
                                reason="needs macOS and KINCHAT_KEYCHAIN_TEST=1")


def sec(*args, check=True):
    return subprocess.run(["security", *args], capture_output=True, text=True, check=check)


@pytest.fixture
def throwaway_keychain(tmp_path):
    path = str(tmp_path / "kinchat-test.keychain-db")
    old_default = sec("default-keychain").stdout.strip().strip('"')
    old_list = [x.strip().strip('"') for x in sec("list-keychains", "-d", "user").stdout.splitlines() if x.strip()]
    sec("create-keychain", "-p", "test", path)
    sec("unlock-keychain", "-p", "test", path)
    sec("list-keychains", "-d", "user", "-s", path, *old_list)
    sec("default-keychain", "-s", path)
    yield path
    sec("default-keychain", "-s", old_default, check=False)
    sec("list-keychains", "-d", "user", "-s", *old_list, check=False)
    sec("delete-keychain", path, check=False)


def test_store_answers_the_real_prompt_and_reads_back(throwaway_keychain):
    value = "kinchat-test-value: with spaces & symbols !$'\""
    keychain.store("test-account", value, service="kinchat-test")
    assert keychain.read("test-account", "kinchat-test") == value
    keychain.store("test-account", "second value", service="kinchat-test")  # -U updates in place
    assert keychain.read("test-account", "kinchat-test") == "second value"


@pytest.mark.parametrize("value", [
    "QUJD" * 1024,                                                              # 4096-char base64 (Graph key)
    " ".join(f"https://cal.example.com/feed-{i:04d}.ics" for i in range(100)),  # a long ICS URL list, spaces
    "p" * keychain.CHUNK,                                                       # the longest single item
    "q" * (keychain.CHUNK + 1),                                                 # the shortest chunked one
], ids=["base64-4096", "urls-with-spaces", "one-item-limit", "two-chunks"])
def test_long_values_round_trip_exactly(throwaway_keychain, value):
    keychain.store("long-account", value, service="kinchat-test")  # raises if the read-back differs
    assert keychain.read("long-account", "kinchat-test") == value


def test_prompt_really_truncates_at_128(throwaway_keychain):
    """Why chunks exist: what the prompt silently does to a longer value (caught by the read-back)."""
    with pytest.raises(keychain.KeychainError, match="128 of 200"):
        keychain._store_one("raw", "r" * 200, "kinchat-test", keychain.answer_prompts)
        stored = keychain.read("raw", "kinchat-test")
        raise keychain.KeychainError(f"read back {len(stored)} of 200")
