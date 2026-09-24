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
