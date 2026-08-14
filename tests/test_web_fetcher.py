import types

import httpx
import respx

from telegram_net.config import SourceConfig
from telegram_net.fetchers.web_fetcher import WebFetcher

SOURCE = SourceConfig(channel_id="x", type="website", name="Test", handle_or_url="https://example.com/feed.xml")


def _fake_parsed(entries):
    return types.SimpleNamespace(entries=entries)


def test_direct_rss_feed_returns_new_entries(monkeypatch):
    entries = [
        {"id": "1", "title": "Новина 1", "summary": "<p>Текст 1</p>", "link": "https://example.com/1"},
        {"id": "2", "title": "Новина 2", "summary": "Текст 2", "link": "https://example.com/2"},
    ]
    monkeypatch.setattr(
        "telegram_net.fetchers.web_fetcher.feedparser.parse", lambda url: _fake_parsed(entries)
    )
    fetcher = WebFetcher()

    items, cursor = fetcher.fetch_new(SOURCE, cursor=None)

    assert len(items) == 2
    assert {i.external_id for i in items} == {"1", "2"}
    assert set(cursor.split(",")) == {"1", "2"}


def test_rss_skips_already_seen_ids(monkeypatch):
    entries = [
        {"id": "1", "title": "Новина 1", "summary": "Текст 1", "link": "https://example.com/1"},
        {"id": "2", "title": "Новина 2", "summary": "Текст 2", "link": "https://example.com/2"},
    ]
    monkeypatch.setattr(
        "telegram_net.fetchers.web_fetcher.feedparser.parse", lambda url: _fake_parsed(entries)
    )
    fetcher = WebFetcher()

    items, cursor = fetcher.fetch_new(SOURCE, cursor="1")

    assert len(items) == 1
    assert items[0].external_id == "2"


def test_rss_extracts_media_from_media_content_and_enclosures(monkeypatch):
    entries = [
        {
            "id": "1",
            "title": "З фото",
            "summary": "текст",
            "link": "https://example.com/1",
            "media_content": [{"url": "https://example.com/1.jpg", "medium": "image"}],
        },
        {
            "id": "2",
            "title": "З відео-вкладенням",
            "summary": "текст",
            "link": "https://example.com/2",
            "enclosures": [{"href": "https://example.com/2.mp4", "type": "video/mp4"}],
        },
    ]
    monkeypatch.setattr(
        "telegram_net.fetchers.web_fetcher.feedparser.parse", lambda url: _fake_parsed(entries)
    )
    fetcher = WebFetcher()

    items, _ = fetcher.fetch_new(SOURCE, cursor=None)
    by_id = {i.external_id: i for i in items}

    assert by_id["1"].media[0].type == "photo"
    assert by_id["2"].media[0].type == "video"


@respx.mock
def test_no_feed_falls_back_to_og_tags_single_page(monkeypatch):
    monkeypatch.setattr(
        "telegram_net.fetchers.web_fetcher.feedparser.parse", lambda url: _fake_parsed([])
    )
    html = """
    <html><head>
      <meta property="og:title" content="Заголовок новини" />
      <meta property="og:description" content="Опис новини" />
      <meta property="og:image" content="https://example.com/cover.jpg" />
    </head><body></body></html>
    """
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=html))
    source = SourceConfig(channel_id="x", type="website", name="Test", handle_or_url="https://example.com/feed.xml")
    fetcher = WebFetcher(http_client=httpx.Client())

    items, cursor = fetcher.fetch_new(source, cursor=None)

    assert len(items) == 1
    assert "Заголовок новини" in items[0].text
    assert items[0].media[0].url == "https://example.com/cover.jpg"
    assert cursor == items[0].external_id


@respx.mock
def test_unchanged_single_page_returns_no_new_items(monkeypatch):
    monkeypatch.setattr(
        "telegram_net.fetchers.web_fetcher.feedparser.parse", lambda url: _fake_parsed([])
    )
    html = """
    <html><head>
      <meta property="og:title" content="Той самий заголовок" />
    </head></html>
    """
    respx.get("https://example.com/feed.xml").mock(return_value=httpx.Response(200, text=html))
    source = SourceConfig(channel_id="x", type="website", name="Test", handle_or_url="https://example.com/feed.xml")
    fetcher = WebFetcher(http_client=httpx.Client())

    _, cursor = fetcher.fetch_new(source, cursor=None)
    items_second_run, cursor2 = fetcher.fetch_new(source, cursor=cursor)

    assert items_second_run == []
    assert cursor2 == cursor


@respx.mock
def test_autodiscovers_feed_link_in_html_head(monkeypatch):
    call_log = []

    def fake_parse(url):
        call_log.append(url)
        if url == "https://example.com/":
            return _fake_parsed([])
        return _fake_parsed([{"id": "1", "title": "Новина", "summary": "", "link": "https://example.com/1"}])

    monkeypatch.setattr("telegram_net.fetchers.web_fetcher.feedparser.parse", fake_parse)
    html = """
    <html><head>
      <link rel="alternate" type="application/rss+xml" href="/rss.xml" />
    </head></html>
    """
    respx.get("https://example.com/").mock(return_value=httpx.Response(200, text=html))
    source = SourceConfig(channel_id="x", type="website", name="Test", handle_or_url="https://example.com/")
    fetcher = WebFetcher(http_client=httpx.Client())

    items, _ = fetcher.fetch_new(source, cursor=None)

    assert len(items) == 1
    assert "https://example.com/rss.xml" in call_log
