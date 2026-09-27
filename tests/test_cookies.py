import http.cookiejar
import json
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


def ytdlp_run(path: str, **changes: str | None) -> None:
    """What yt-dlp does to its copy: load it, apply Set-Cookie changes, save on close."""
    jar = YoutubeDLCookieJar(path)
    jar.load()
    for name, value in changes.items():
        if value is None:
            jar.clear(".tiktok.com", "/", name)
            continue
        jar.set_cookie(
            http.cookiejar.Cookie(
                0, name, value, None, False, ".tiktok.com", True, True, "/", True,
                True, 1893456000, False, None, None, {},
            )
        )  # fmt: skip
    jar.save()


def tiktok_values(directory: Path) -> dict[str, str]:
    return {c.name: c.value for c in cookies.parse((directory / "tiktok.txt").read_text())}


def test_session_changes_are_saved_back(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)

    with cookies.session(tmp_path, "tiktok") as path:
        assert path is not None and Path(path).parent == tmp_path
        ytdlp_run(path, sessionid="rotated", new="1")

    assert tiktok_values(tmp_path) == {"sessionid": "rotated", "tt_chain_token": "xyz", "new": "1"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tiktok.txt", "youtube.txt"]


def test_saved_file_keeps_the_httponly_marker_ytdlp_drops(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)

    with cookies.session(tmp_path, "tiktok") as path:
        ytdlp_run(path, sessionid="rotated")

    assert (
        "#HttpOnly_.tiktok.com\tTRUE\t/\tTRUE\t0\ttt_chain_token\txyz"
        in (tmp_path / "tiktok.txt").read_text()
    )


def test_failed_run_still_saves_rotated_cookies(tmp_path: Path) -> None:
    # A rejected or failed link still talked to the site, which may have rotated the
    # session; dropping that rotation leaves a stale session in the file.
    cookies.import_text(tmp_path, NETSCAPE)

    with pytest.raises(RuntimeError), cookies.session(tmp_path, "tiktok") as path:
        ytdlp_run(path, sessionid="rotated")
        raise RuntimeError

    assert tiktok_values(tmp_path)["sessionid"] == "rotated"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["tiktok.txt", "youtube.txt"]


def test_parallel_runs_keep_each_others_changes(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)

    with (
        cookies.session(tmp_path, "tiktok") as first,
        cookies.session(tmp_path, "tiktok") as second,
    ):
        ytdlp_run(first, sessionid="rotated-by-first")
        ytdlp_run(second, tt_chain_token=None, other="2")

    assert tiktok_values(tmp_path) == {"sessionid": "rotated-by-first", "other": "2"}


def test_session_does_not_overwrite_freshly_uploaded_cookies(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)

    with cookies.session(tmp_path, "tiktok") as path:
        ytdlp_run(path, sessionid="old-session")
        cookies.import_text(tmp_path, ".tiktok.com\tTRUE\t/\tTRUE\t0\tsessionid\tnew-upload\n")

    assert tiktok_values(tmp_path) == {"sessionid": "new-upload"}


def test_half_written_copy_is_ignored(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    original = (tmp_path / "tiktok.txt").read_text()

    with cookies.session(tmp_path, "tiktok") as path:
        Path(path).write_text("half-written")

    assert (tmp_path / "tiktok.txt").read_text() == original


# --------------------------------------------------------------------------- #
#  Health                                                                     #
# --------------------------------------------------------------------------- #


def test_health_is_reported_until_new_cookies_arrive(tmp_path: Path) -> None:
    cookies.import_text(tmp_path, NETSCAPE)
    assert cookies.status(tmp_path)[1].health is None

    cookies.record("youtube", False, "Sign in to confirm you're not a bot")
    youtube = cookies.status(tmp_path)[1]
    assert youtube.health is not None and not youtube.health.ok
    assert youtube.logged_in  # the file alone still looks fine

    cookies.record("tiktok", True)
    cookies.import_text(tmp_path, NETSCAPE)  # a new upload: old results no longer apply
    assert [item.health for item in cookies.status(tmp_path)] == [None, None]
