from datetime import time
from zoneinfo import ZoneInfo

import pytest

from sbahelper.config import Env, Settings


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch):
    def set_env(**values: str) -> Env:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return Env()

    return set_env


def test_blank_values_fall_back_to_defaults(env) -> None:
    reader = env(NAME="   ")
    assert reader.text("NAME", "default") == "default"
    assert reader.integer("NAME", 5) == 5
    assert reader.flag("NAME", True) is True
    assert reader.issues == []


@pytest.mark.parametrize("raw", ["0", "false", "NO", " off "])
def test_flag_reads_falsey_words(env, raw: str) -> None:
    assert env(FLAG=raw).flag("FLAG", True) is False


@pytest.mark.parametrize("raw", ["1", "true", "yes", "anything"])
def test_flag_treats_other_words_as_true(env, raw: str) -> None:
    assert env(FLAG=raw).flag("FLAG", False) is True


def test_integer_rejects_garbage_and_out_of_range_values(env) -> None:
    assert env(N="12").integer("N", 5) == 12
    reader = env(N="later")
    assert reader.integer("N", 5) == 5
    assert reader.issues == ["Invalid N='later', using 5."]
    assert env(N="9").integer("N", 6, high=6) == 6
    assert env(N="-1").integer("N", 6) == 6


def test_ids_skip_invalid_parts(env) -> None:
    reader = env(IDS="-100123, 42,,abc")
    assert reader.ids("IDS") == frozenset({-100123, 42})
    assert len(reader.issues) == 1


@pytest.mark.parametrize(("raw", "expected"), [("9:05", time(9, 5)), ("23:59", time(23, 59))])
def test_clock_accepts_hh_mm(env, raw: str, expected: time) -> None:
    assert env(AT=raw).clock("AT", time(20)) == expected


@pytest.mark.parametrize("raw", ["20", "20:00:00", "25:00", "20:60", "восемь"])
def test_clock_falls_back_on_invalid_values(env, raw: str) -> None:
    reader = env(AT=raw)
    assert reader.clock("AT", time(20)) == time(20)
    assert "using 20:00" in reader.issues[0]


def test_zone_falls_back_on_unknown_names(env) -> None:
    assert env(TZ="Asia/Tokyo").zone("TZ", "UTC") == ZoneInfo("Asia/Tokyo")
    reader = env(TZ="Nowhere/Nothing")
    assert reader.zone("TZ", "Europe/Tallinn") == ZoneInfo("Europe/Tallinn")
    assert reader.issues


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "MAX_FILE_SIZE_MB",
        "MAX_FILE_SIZE_BYTES",
        "STATS_WEEKLY_TIME",
        "TZ",
        "ADMIN_IDS",
        "LOG_LEVEL",
        "ACCESS_POLICY",
        "ALLOWED_CHAT_IDS",
    ):
        monkeypatch.delenv(name, raising=False)
    loaded = Settings.from_env()
    assert loaded.max_file_size_bytes == 45_000_000
    assert loaded.access_policy == "open"
    assert loaded.stats_time == time(20)
    assert loaded.tz == ZoneInfo("Europe/Tallinn")
    assert loaded.admin_ids == frozenset()
    assert loaded.log_level == "INFO"


def test_settings_collect_every_problem(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_FILE_SIZE_BYTES", "lots")
    monkeypatch.setenv("LOG_LEVEL", "chatty")
    monkeypatch.setenv("STATS_WEEKLY_WEEKDAY", "7")
    monkeypatch.delenv("ALLOWED_CHAT_IDS", raising=False)
    monkeypatch.delenv("ACCESS_POLICY", raising=False)
    loaded = Settings.from_env()
    assert (loaded.max_file_size_bytes, loaded.log_level, loaded.stats_weekday) == (
        45_000_000,
        "INFO",
        6,
    )
    assert len(loaded.issues) == 3


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("ACCESS_POLICY", "ALLOWED_CHAT_IDS", "MAX_FILE_SIZE_MB", "MAX_FILE_SIZE_BYTES"):
        monkeypatch.delenv(name, raising=False)

    def load(**values: str) -> Settings:
        for name, value in values.items():
            monkeypatch.setenv(name, value)
        return Settings.from_env()

    return load


def test_legacy_allowlist_is_kept(clean_env) -> None:
    loaded = clean_env(ALLOWED_CHAT_IDS="-1001, -1002")
    assert loaded.access_policy == "allowlist"
    assert loaded.chat_allowed(-1001) and not loaded.chat_allowed(-3)


def test_malformed_allowlist_fails_closed(clean_env) -> None:
    loaded = clean_env(ALLOWED_CHAT_IDS="-100abc, oops")
    assert loaded.access_policy == "allowlist"
    assert loaded.allowed_chat_ids == frozenset()
    assert not loaded.chat_allowed(-1001)
    assert any("allowlist is empty" in issue for issue in loaded.issues)


def test_partly_malformed_allowlist_keeps_the_valid_ids(clean_env) -> None:
    loaded = clean_env(ALLOWED_CHAT_IDS="-1001,x")
    assert loaded.allowed_chat_ids == frozenset({-1001})
    assert not loaded.chat_allowed(-1002)


def test_unknown_policy_fails_closed(clean_env) -> None:
    loaded = clean_env(ACCESS_POLICY="everyone")
    assert loaded.access_policy == "allowlist"
    assert not loaded.chat_allowed(-1001)


def test_explicit_open_policy(clean_env) -> None:
    loaded = clean_env(ACCESS_POLICY="open", ALLOWED_CHAT_IDS="-1")
    assert loaded.chat_allowed(-999)
    assert any("ignored" in issue for issue in loaded.issues)


def test_admin_private_chats_pass_an_allowlist(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("ADMIN_IDS", "42")
    loaded = clean_env(ACCESS_POLICY="allowlist")
    assert loaded.chat_allowed(42) and not loaded.chat_allowed(43)


@pytest.mark.parametrize(
    ("env_values", "expected"),
    [
        ({}, 45_000_000),
        ({"MAX_FILE_SIZE_BYTES": "40_000_000"}, 40_000_000),
        ({"MAX_FILE_SIZE_BYTES": "60000000"}, 45_000_000),  # above the cloud API: refused
        ({"MAX_FILE_SIZE_BYTES": "10"}, 45_000_000),
        ({"MAX_FILE_SIZE_MB": "30"}, 30_000_000),  # deprecated, read as 10^6 bytes
        ({"MAX_FILE_SIZE_MB": "50"}, 50_000_000),
        ({"MAX_FILE_SIZE_MB": "2000"}, 50_000_000),  # capped at the documented limit
    ],
)
def test_file_size_limit_is_bytes_and_capped(clean_env, env_values, expected) -> None:
    assert clean_env(**env_values).max_file_size_bytes == expected


def test_deprecated_megabytes_are_reported(clean_env) -> None:
    loaded = clean_env(MAX_FILE_SIZE_MB="50")
    assert any("deprecated" in issue for issue in loaded.issues)
