"""Джерела-сайти без RSS/Atom (type="web_scrape" у конфігу).

На відміну від колишнього OG-тегового фолбека (видалений з web_fetcher.py —
описував ОРГАНІЗАЦІЮ, а не подію), тут список новин береться з реальної
розмітки сторінки за CSS-селектором з конфігу джерела (scrape_item_selector /
scrape_title_selector) — це справжні картки новин на сторінці, а не здогад за
OG-тегами головної. Один спільний код для всіх таких сайтів замість окремого
Python-парсера на кожен — селектори підбираються по факту (README) під
конкретну верстку сайту й лежать у конфізі, а не в коді.

Сторінки такого типу майже ніколи не дають надійну дату публікації в розмітці
(на відміну від RSS-записів). Тому, як і для Telethon-фетчера: перше
підключення джерела лише фіксує поточний список посилань як базову лінію,
не публікуючи "заднім числом" усе, що вже висіло на сайті до підключення.

Фото: беремо не з картки в списку (там воно є не завжди й часто низької
якості), а з og:image самої статті — один додатковий GET на кожен НОВИЙ
елемент (не на весь список), тож вартість обмежена реальною кількістю нових
новин за прогін. Якщо og:image повторюється в кількох різних статей з того ж
джерела за один прогін — це логотип/заглушка сайту, а не фото конкретної
новини (підтверджено на практиці для zhitomirnews.com — там завжди та сама
картинка-логотип), і такий "фолбек" відкидається: без цього правило "немає
фото — немає посту" (pipeline.py) публікувало б логотип замість новини."""

from __future__ import annotations

import logging
from collections import Counter
from typing import Iterable

import httpx
from bs4 import BeautifulSoup, Tag

from telegram_net.config import SourceConfig
from telegram_net.fetchers.base import FetchedItem, FetchedMedia

logger = logging.getLogger(__name__)

_MAX_CURSOR_URLS = 200

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}


class HtmlListFetcher:
    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(
            timeout=15, follow_redirects=True, headers=_BROWSER_HEADERS
        )

    def fetch_new(self, source: SourceConfig, cursor: str | None) -> tuple[list[FetchedItem], str | None]:
        if not source.scrape_item_selector:
            logger.warning(
                "Джерело %s (web_scrape) без scrape_item_selector у конфізі — пропускаю",
                source.handle_or_url,
            )
            return [], cursor

        resp = self._http.get(source.handle_or_url)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        entries = _extract_entries(soup, source)

        if cursor is None:
            # Перше підключення джерела: без дати публікації в розмітці немає
            # способу відрізнити "щойно вийшло" від "висить на сайті давно" —
            # фіксуємо лише поточний список як базову лінію, як і з Telethon.
            return [], _encode_cursor([url for url, _ in entries])

        seen = set(cursor.split(",")) if cursor else set()
        new_entries = [(url, title) for url, title in entries if url not in seen]

        if not new_entries:
            return [], cursor

        images = self._fetch_article_images(url for url, _ in new_entries)
        image_counts = Counter(images.values())

        items = []
        for url, title in new_entries:
            image_url = images.get(url)
            if image_url and image_counts[image_url] > 1:
                # Той самий og:image у кількох різних статей за один прогін —
                # це логотип/заглушка сайту, а не фото цієї конкретної новини.
                logger.info(
                    "Джерело %s: og:image %s повторюється для кількох статей — не фото новини, ігнорую",
                    url,
                    image_url,
                )
                image_url = None
            media = (FetchedMedia(type="photo", url=image_url),) if image_url else ()
            items.append(FetchedItem(external_id=url, text=title, url=url, published_at=None, media=media))

        new_cursor = _encode_cursor([*seen, *(url for url, _ in new_entries)])
        return items, new_cursor

    def _fetch_article_images(self, urls: Iterable[str]) -> dict[str, str]:
        images: dict[str, str] = {}
        for url in urls:
            try:
                resp = self._http.get(url)
                resp.raise_for_status()
            except httpx.HTTPError:
                continue
            article_soup = BeautifulSoup(resp.text, "lxml")
            og_image = article_soup.find("meta", property="og:image")
            if og_image and og_image.get("content"):
                images[url] = str(httpx.URL(url).join(og_image["content"]))
        return images


def _extract_entries(soup: BeautifulSoup, source: SourceConfig) -> list[tuple[str, str]]:
    base_url = source.handle_or_url
    entries: list[tuple[str, str]] = []

    for item in soup.select(source.scrape_item_selector):
        link_el = item if item.has_attr("href") else item.select_one("a[href]")
        if link_el is None:
            continue
        url = str(httpx.URL(base_url).join(link_el["href"]))
        title = _extract_title(item, source.scrape_title_selector)
        if not title:
            continue
        entries.append((url, title))

    return entries


def _extract_title(item: Tag, title_selector: str | None) -> str:
    if title_selector:
        title_el = item.select_one(title_selector)
        if title_el:
            text = title_el.get_text(" ", strip=True)
            if text:
                return text

    text = item.get_text(" ", strip=True)
    if text:
        return text

    title_attr = item.get("title")
    if title_attr:
        return str(title_attr)

    img = item.select_one("img[alt]")
    if img and img.get("alt"):
        return str(img["alt"])

    return ""


def _encode_cursor(urls: list[str]) -> str:
    unique_in_order = list(dict.fromkeys(urls))
    return ",".join(unique_in_order[-_MAX_CURSOR_URLS:])
