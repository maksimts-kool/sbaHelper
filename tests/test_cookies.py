import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yt_dlp.cookies import YoutubeDLCookieJar

from sbahelper import cookies
from sbahelper.cookies import Cookie, CookieError

NETSCAPE = """# Netscape HTTP Cookie File
# comment

.tiktok.com\tTRUE\t/\tTRUE\t1893456000\tsessionid\tabc
#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t0\ttt_chain_token\txyz
.youtube.com\tTRUE\t/\tTRUE\t1893456000\tLOGIN_INFO\tlogin
.example.com\tTRUE\t/\tFALSE\t0\tempty\t
"""

COOKIE_EDITOR_JSON = [
    {
        "domain": ".tiktok.com",
        "name": "sessionid",
        "value": "abc",
        "path": "/",
        "secure": True,
        "httpOnly": True,
        "hostOnly": False,
        "expirationDate": 1893456000.25,
        "session": False,
    },
    {"domain": "www.youtube.com", "name": "PREF", "value": "hl=en", "hostOnly": True},
    {"domain": "google.com", "name": "SID", "value": "sid", "session": True},
]


# --------------------------------------------------------------------------- #
#  Parsing                                                                    #
# --------------------------------------------------------------------------- #


def test_netscape_file_is_parsed() -> None:
    parsed = cookies.parse(NETSCAPE)

    assert [c.name for c in parsed] == ["sessionid", "tt_chain_token", "LOGIN_INFO", "empty"]
    assert parsed[1].http_only and parsed[1].domain == ".tiktok.com"
    assert parsed[0].expires == 1893456000 and parsed[0].secure
    assert parsed[3].value == ""


def test_cookie_editor_json_is_parsed() -> None:
    parsed = cookies.parse(json.dumps(COOKIE_EDITOR_JSON))

    assert parsed[0] == Cookie(".tiktok.com", "sessionid", "abc", "/", True, 1893456000, True)
    assert parsed[1].domain == "www.youtube.com"  # host-only keeps its exact host
    assert parsed[2].domain == ".google.com" and parsed[2].expires == 0


def test_wrapped_json_and_bom_are_accepted() -> None:
    text = "\ufeff" + json.dumps({"url": "https://tiktok.com", "cookies": COOKIE_EDITOR_JSON})
    assert len(cookies.parse(text)) == 3


@pytest.mark.parametrize(
    ("text", "reason"),
    [
        ("", "no cookies"),
        ("# only comments\n", "no cookies"),
        ("not\ta cookie line\n", "line 1"),
        ("[{]", "invalid JSON"),
        ('{"cookies": 5}', "JSON list"),
        ('[{"value": "x"}]', "no name or domain"),
        (".tiktok.com\tTRUE\t/\tTRUE\tsoon\tsessionid\tabc", "bad expiry"),
    ],
)
def test_unreadable_files_are_rejected(text: str, reason: str) -> None:
    with pytest.raises(CookieError, match=reason):
        cookies.parse(text)


# --------------------------------------------------------------------------- #
#  Import                                                                     #
# --------------------------------------------------------------------------- #


def test_import_splits_by_platform_and_ytdlp_can_read_the_result(tmp_path: Path) -> None:
    counts = cookies.import_text(tmp_path, NETSCAPE)

    assert counts == {"tiktok": 2, "youtube": 1}
    assert not (tmp_path / "other.txt").exists()
    jar = YoutubeDLCookieJar(str(tmp_path / "tiktok.txt"))
    jar.load()
    assert {cookie.name for cookie in jar} == {"sessionid", "tt_chain_token"}
    assert oct((tmp_path / "tiktok.txt").stat().st_mode & 0o777) == "0o600"


def test_import_replaces_the_previous_file(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    cookies.import_text(tmp_path, ".tiktok.com\tTRUE\t/\tTRUE\t0\tnew\t1\n")

    assert [c.name for c in cookies.parse((tmp_path / "tiktok.txt").read_text())] == ["new"]
    assert (tmp_path / "youtube.txt").exists()  # untouched: the new file had no YouTube


def test_import_without_known_domains_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(CookieError, match="no TikTok or YouTube"):
        cookies.import_text(tmp_path, ".example.com\tTRUE\t/\tFALSE\t0\ta\tb\n")


def test_dropped_files_are_imported_once(tmp_path: Path) -> None:
    (tmp_path / "cookies.txt").write_text(NETSCAPE)
    (tmp_path / "youtubecookies.json").write_text(json.dumps(COOKIE_EDITOR_JSON[1:]))
    (tmp_path / "notes.txt").write_text("hello\tworld\n")

    cookies.import_dropped(tmp_path)

    names = sorted(path.name for path in tmp_path.iterdir())
    assert names == [
        "cookies.txt.imported",
        "notes.txt",  # unreadable: left alone
        "tiktok.txt",
        "youtube.txt",
        "youtubecookies.json.imported",
    ]
    youtube = cookies.parse((tmp_path / "youtube.txt").read_text())
    assert {c.name for c in youtube} == {"PREF", "SID"}  # the later file won


def test_missing_directory_is_fine(tmp_path: Path) -> None:
    cookies.import_dropped(tmp_path / "missing")


# --------------------------------------------------------------------------- #
#  Status                                                                     #
# --------------------------------------------------------------------------- #


def test_status_reports_login_and_expiry(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    now = datetime(2026, 9, 1, tzinfo=UTC)

    tiktok, youtube = cookies.status(tmp_path, now)

    assert (tiktok.platform, tiktok.count, tiktok.logged_in) == ("tiktok", 2, True)
    assert tiktok.login_expires == datetime.fromtimestamp(1893456000, UTC)
    assert youtube.logged_in and youtube.updated is not None


def test_status_of_expired_and_missing_files(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, ".tiktok.com\tTRUE\t/\tTRUE\t1000\tsessionid\tabc\n")

    tiktok, youtube = cookies.status(tmp_path)

    assert tiktok.count == 1 and not tiktok.logged_in and tiktok.login_expires is not None
    assert youtube.count == 0 and youtube.updated is None


# --------------------------------------------------------------------------- #
#  Session copies                                                             #
# --------------------------------------------------------------------------- #


def test_session_is_none_without_a_file(tmp_path: Path) -> None:
    with cookies.session(tmp_path, "tiktok") as path:
        assert path is None


def test_session_changes_are_saved_back(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    source = tmp_path / "tiktok.txt"

    with cookies.session(tmp_path, "tiktok") as path:
        assert path is not None and Path(path) != source
        Path(path).write_text("refreshed")

    assert source.read_text() == "refreshed"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tiktok.txt", "youtube.txt"]


def test_session_does_not_overwrite_freshly_uploaded_cookies(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    source = tmp_path / "tiktok.txt"

    with cookies.session(tmp_path, "tiktok") as path:
        Path(path).write_text("stale copy")
        source.write_text("new upload")
        os.utime(source, ns=(1, 1))

    assert source.read_text() == "new upload"


def test_failed_run_keeps_the_original(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    original = (tmp_path / "tiktok.txt").read_text()

    with pytest.raises(RuntimeError), cookies.session(tmp_path, "tiktok") as path:
        Path(path).write_text("half-written")
        raise RuntimeError

    assert (tmp_path / "tiktok.txt").read_text() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tiktok.txt", "youtube.txt"]
