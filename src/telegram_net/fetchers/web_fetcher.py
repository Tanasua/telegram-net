"""Джерела-сайти: RSS/Atom, якщо є; інакше — одна знімок-сторінка за OG-тегами.

Автовиявлення фіда і OG-парсинг покривають типовий випадок. Сайти з нетиповою
розміткою (JS-рендер, кастомна структура без RSS і без OG) потребуватимуть
окремого per-site скрапера — свідомо не намагаюся вгадати це наперед для всіх
23 райцентрів, це легше зробити предметно, коли конкретний сайт підключають.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import feedparser
import httpx
from bs4 import BeautifulSoup

from telegram_net.config import SourceConfig
from telegram_net.dedup import content_hash
from telegram_net.fetchers.base import FetchedItem, FetchedMedia

_MAX_CURSOR_IDS = 200


class WebFetcher:
    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=15, follow_redirects=True)

    def fetch_new(self, source: SourceConfig, cursor: str | None) -> tuple[list[FetchedItem], str | None]:
        url = source.handle_or_url

        direct = feedparser.parse(url)
        if direct.entries:
            return self._entries_to_result(direct.entries, url, cursor)

        try:
            resp = self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError:
            return [], cursor

        soup = BeautifulSoup(resp.text, "lxml")
        feed_link = _find_feed_link(soup, url)
        if feed_link:
            parsed = feedparser.parse(feed_link)
            return self._entries_to_result(parsed.entries, feed_link, cursor)

        return self._single_page_from_soup(soup, url, cursor)

    def _entries_to_result(
        self, entries: list[Any], feed_url: str, cursor: str | None
    ) -> tuple[list[FetchedItem], str | None]:
        seen_ids = set(cursor.split(",")) if cursor else set()
        new_ids = set(seen_ids)
        new_items: list[FetchedItem] = []

        for entry in entries:
            entry_id = entry.get("id") or entry.get("link")
            if not entry_id or entry_id in seen_ids:
                continue
            new_ids.add(entry_id)
            new_items.append(
                FetchedItem(
                    external_id=entry_id,
                    text=_entry_text(entry),
                    url=entry.get("link", feed_url),
                    published_at=_parse_entry_date(entry),
                    media=_extract_rss_media(entry),
                )
            )

        capped_cursor = ",".join(list(new_ids)[-_MAX_CURSOR_IDS:])
        return new_items, capped_cursor or cursor

    def _single_page_from_soup(
        self, soup: BeautifulSoup, url: str, cursor: str | None
    ) -> tuple[list[FetchedItem], str | None]:
        title = _meta(soup, "og:title") or (soup.title.string.strip() if soup.title and soup.title.string else "")
        description = _meta(soup, "og:description") or ""
        image = _meta(soup, "og:image")
        text = f"{title}\n\n{description}".strip()

        snapshot_hash = content_hash(text)
        if snapshot_hash == cursor:
            return [], cursor

        media = (FetchedMedia(type="photo", url=image),) if image else ()
        item = FetchedItem(external_id=snapshot_hash, text=text, url=url, published_at=None, media=media)
        return [item], snapshot_hash


def _find_feed_link(soup: BeautifulSoup, base_url: str) -> str | None:
    link = soup.find("link", rel="alternate", type=lambda t: bool(t) and ("rss" in t or "atom" in t))
    if link and link.get("href"):
        return str(httpx.URL(base_url).join(link["href"]))
    return None


def _meta(soup: BeautifulSoup, property_name: str) -> str | None:
    tag = soup.find("meta", property=property_name)
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def _entry_text(entry: Any) -> str:
    title = entry.get("title", "")
    summary = entry.get("summary", "")
    summary_text = BeautifulSoup(summary, "lxml").get_text(" ", strip=True) if summary else ""
    return f"{title}\n\n{summary_text}".strip()


def _parse_entry_date(entry: Any) -> dt.datetime | None:
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if not parsed:
        return None
    return dt.datetime(*parsed[:6], tzinfo=dt.timezone.utc)


def _extract_rss_media(entry: Any) -> tuple[FetchedMedia, ...]:
    media: list[FetchedMedia] = []

    for mc in entry.get("media_content", []) or []:
        url = mc.get("url")
        if not url:
            continue
        medium = mc.get("medium") or ("video" if "video" in (mc.get("type") or "") else "photo")
        media.append(FetchedMedia(type="video" if medium == "video" else "photo", url=url))

    for enc in entry.get("enclosures", []) or []:
        url = enc.get("href") or enc.get("url")
        if not url:
            continue
        enc_type = enc.get("type", "")
        if enc_type.startswith("image/"):
            media.append(FetchedMedia(type="photo", url=url))
        elif enc_type.startswith("video/"):
            media.append(FetchedMedia(type="video", url=url))

    if not media:
        summary = entry.get("summary", "")
        if summary:
            soup = BeautifulSoup(summary, "lxml")
            for img in soup.find_all("img"):
                if img.get("src"):
                    media.append(FetchedMedia(type="photo", url=img["src"]))

    return tuple(media)
