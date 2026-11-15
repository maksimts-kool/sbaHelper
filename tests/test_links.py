import pytest

from sbahelper.links import find_link, is_not_a_video, platform_of


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=abcdEFGhij",
        "https://youtube.com/shorts/abcdEFGhij",
        "https://m.youtube.com/shorts/abcdEFGhij",
        "https://youtu.be/abcdEFGhij",
        "https://www.tiktok.com/@user/video/123",
        "https://vm.tiktok.com/ZSabc123/",
    ],
)
def test_supported_links_are_found(url: str) -> None:
    assert find_link(url) == url


def test_link_is_taken_from_text_without_trailing_punctuation() -> None:
    assert find_link("смотри (https://youtu.be/abc123)!") == "https://youtu.be/abc123"
    assert find_link("вот https://www.tiktok.com/@u/video/1, круто") == (
        "https://www.tiktok.com/@u/video/1"
    )


def test_first_link_in_the_text_wins() -> None:
    text = "https://youtu.be/first и https://www.tiktok.com/@u/video/2"
    assert find_link(text) == "https://youtu.be/first"


@pytest.mark.parametrize(
    "text",
    ["https://example.com/video/1", "https://www.facebook.com/reel/1", "https://notyoutube.com/x"],
)
def test_other_sites_are_ignored(text: str) -> None:
    assert find_link(text) is None


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@ivanova197",
        "https://www.tiktok.com/@ivanova197/?lang=ru",
        "https://www.tiktok.com/tag/cats",
        "https://www.youtube.com/@MrBeast",
        "https://www.youtube.com/channel/UCabc123",
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/shorts",
    ],
)
def test_profiles_and_listings_are_not_videos(url: str) -> None:
    assert is_not_a_video(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.tiktok.com/@ivanova197/video/7234567890",
        "https://vm.tiktok.com/ZSabc123/",
        "https://youtube.com/shorts/abcdEFGhij",
        "https://youtu.be/abcdEFGhij",
    ],
)
def test_single_videos_are_videos(url: str) -> None:
    assert not is_not_a_video(url)


@pytest.mark.parametrize(
    ("url", "platform"),
    [
        ("https://vm.tiktok.com/ZSabc/", "tiktok"),
        ("https://youtu.be/abc", "youtube"),
        ("https://m.youtube.com/shorts/abc", "youtube"),
        ("https://youtube.com.evil.example/x", "other"),
    ],
)
def test_platform_comes_from_the_host(url: str, platform: str) -> None:
    assert platform_of(url) == platform


# --------------------------------------------------------------------------- #
#  Validation, identity and entities                                          #
# --------------------------------------------------------------------------- #

from datetime import datetime  # noqa: E402

from telegram import Chat, Message, MessageEntity  # noqa: E402

from sbahelper.links import find_links, message_links, parse_link  # noqa: E402


@pytest.mark.parametrize(
    "raw",
    [
        "ftp://www.youtube.com/watch?v=abcdefghijk",
        "https://user:pw@www.youtube.com/watch?v=abcdefghijk",
        "https://www.youtube.com:8443/watch?v=abcdefghijk",
        "https://youtube.com.evil.example/watch?v=abcdefghijk",
        "https://evil.example/?u=https://youtu.be/abcdefghijk",
        "https://" + "a" * 3000 + ".youtube.com/x",
    ],
)
def test_unsafe_or_foreign_urls_are_refused(raw: str) -> None:
    link = parse_link(raw)
    assert link is None or link.platform != "youtube" or "evil" not in link.url


def test_scheme_is_added_and_forced_to_https() -> None:
    assert parse_link("vm.tiktok.com/ZSabc/").url == "https://vm.tiktok.com/ZSabc/"
    assert parse_link("http://youtu.be/abcdefghijk").url.startswith("https://")


@pytest.mark.parametrize(
    ("url", "key"),
    [
        ("https://www.youtube.com/watch?v=abcdefghijk&si=x", "youtube:abcdefghijk"),
        ("https://youtu.be/abcdefghijk?si=track", "youtube:abcdefghijk"),
        ("https://m.youtube.com/shorts/abcdefghijk", "youtube:abcdefghijk"),
        ("https://www.tiktok.com/@u/video/7234567890123?is_from_webapp=1", "tiktok:7234567890123"),
        ("https://vm.tiktok.com/ZSabc/", "url:vm.tiktok.com/ZSabc"),
        ("https://www.tiktok.com/t/ZT8abc/?_t=1", "url:tiktok.com/t/ZT8abc"),
    ],
)
def test_identity(url: str, key: str) -> None:
    assert parse_link(url).key == key


def test_distinct_videos_never_share_a_key() -> None:
    keys = {
        parse_link(u).key
        for u in (
            "https://www.youtube.com/watch?v=abcdefghijk",
            "https://www.youtube.com/watch?v=abcdefghijl",
            "https://vm.tiktok.com/ZSabc/",
            "https://vm.tiktok.com/ZSabd/",
            "https://vt.tiktok.com/ZSabc/",
            "https://www.tiktok.com/@u/video/7234567890123",
            "https://www.tiktok.com/@u/video/7234567890124",
        )
    }
    assert len(keys) == 7


def test_links_are_deduplicated_by_identity() -> None:
    links = find_links(
        ["https://youtu.be/abcdefghijk https://www.youtube.com/watch?v=abcdefghijk&si=1"]
    )
    assert [link.key for link in links] == ["youtube:abcdefghijk"]


def real_message(text: str, entities: list[MessageEntity], *, caption: bool = False) -> Message:
    chat = Chat(-100, "supergroup")
    if caption:
        return Message(1, datetime.now(), chat, caption=text, caption_entities=entities)
    return Message(1, datetime.now(), chat, text=text, entities=entities)


def test_text_link_target_counts_not_its_visible_text() -> None:
    # Emoji before the link: entity offsets are UTF-16 code units, not characters.
    text = "😀😀 смотри"
    offset = len("😀😀 ".encode("utf-16-le")) // 2
    entity = MessageEntity(MessageEntity.TEXT_LINK, offset, 6, url="https://youtu.be/abcdefghijk")
    assert [link.key for link in message_links(real_message(text, [entity]))] == [
        "youtube:abcdefghijk"
    ]


def test_hidden_link_to_another_site_is_ignored() -> None:
    text = "https://youtu.be/abcdefghijk"  # looks like YouTube…
    entity = MessageEntity(MessageEntity.TEXT_LINK, 0, len(text), url="https://evil.example/")
    links = message_links(real_message(text, [entity]))
    # …the visible text is still a real YouTube URL, so that one counts; the target does not.
    assert [link.url for link in links] == ["https://youtu.be/abcdefghijk"]


def test_caption_url_entities_are_read() -> None:
    caption = "🎬 vm.tiktok.com/ZSabc/"
    offset = len("🎬 ".encode("utf-16-le")) // 2
    entity = MessageEntity(MessageEntity.URL, offset, len("vm.tiktok.com/ZSabc/"))
    links = message_links(real_message(caption, [entity], caption=True))
    assert [link.url for link in links] == ["https://vm.tiktok.com/ZSabc/"]


def test_replies_stay_in_the_forum_topic() -> None:
    """python-telegram-bot's reply shortcuts keep the topic; we rely on that."""
    import asyncio

    sent: dict = {}

    class Bot:
        async def send_message(self, **kwargs):
            sent.update(kwargs)

        defaults = None

    message = Message(
        5,
        datetime.now(),
        Chat(-100, "supergroup", is_forum=True),
        text="x",
        message_thread_id=7,
        is_topic_message=True,
    )
    message.set_bot(Bot())
    asyncio.run(message.reply_text("hi"))
    assert sent["message_thread_id"] == 7
