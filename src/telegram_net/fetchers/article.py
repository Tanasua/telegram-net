"""Повний текст статті для джерел-сайтів.

RSS дає заголовок і короткий анонс, скрапінг списку — лише заголовок (у
середньому 82 символи). З такого матеріалу пост виходив із «води»: «Центр буде
сучасним реабілітаційним простором». Тому для кожної НОВОЇ новини з сайту
відкриваємо саму статтю й дістаємо текст, головне фото та дату публікації.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup, Tag

logger = logging.getLogger(__name__)

MAX_ARTICLE_CHARS = 2500

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "uk-UA,uk;q=0.9,en;q=0.8",
}

_NOISE_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form", "iframe", "figure")
_NOISE_CLASS_RE = re.compile(
    r"comment|related|share|social|sidebar|widget|menu|breadcrumb|banner|advert|popular|tags|footer|subscribe",
    re.IGNORECASE,
)
_LOGO_RE = re.compile(r"logo|favicon|default|placeholder|no[-_]?image|noimage|stub|blank|avatar", re.IGNORECASE)


@dataclass(frozen=True)
class Article:
    text: str
    image_url: str | None
    published_at: dt.datetime | None


def looks_like_logo(url: str) -> bool:
    """Назва файлу натякає на логотип/заглушку сайту, а не фото новини."""
    name = url.rsplit("/", 1)[-1]
    return bool(_LOGO_RE.search(name)) or name.lower().endswith((".svg", ".ico", ".gif"))


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        el = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if el and el.get("content"):
            return str(el["content"]).strip()
    return None


def _parse_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        ts = dt.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    # Дата без часового поясу на українському сайті — це київський час, але
    # різниця з UTC (2–3 год) для порогу свіжості в 12 год несуттєва.
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _published_at(soup: BeautifulSoup) -> dt.datetime | None:
    found = _parse_date(_meta(soup, "article:published_time", "og:article:published_time", "datePublished"))
    if found:
        return found
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except ValueError:
            continue
        for node in data if isinstance(data, list) else [data]:
            if isinstance(node, dict) and node.get("datePublished"):
                found = _parse_date(str(node["datePublished"]))
                if found:
                    return found
    time_el = soup.find("time", attrs={"datetime": True})
    return _parse_date(time_el["datetime"]) if time_el else None


def _is_noise(el: Tag) -> bool:
    return bool(_NOISE_CLASS_RE.search(" ".join(el.get("class", [])) + " " + (el.get("id") or "")))


def _paragraph_text(container: Tag) -> str:
    parts: list[str] = []
    for p in container.find_all(["p", "li", "blockquote", "h2", "h3"]):
        # Шум шукаємо лише між абзацом і контейнером: класи <body> на кшталт
        # "has-sidebar" не мають викидати всю статтю.
        noisy = False
        for parent in p.parents:
            if parent is container:
                break
            if isinstance(parent, Tag) and _is_noise(parent):
                noisy = True
                break
        if noisy:
            continue
        text = re.sub(r"\s+", " ", p.get_text(" ", strip=True))
        links = p.find_all("a")
        if links and len(" ".join(a.get_text(" ", strip=True) for a in links)) >= 0.8 * len(text):
            continue  # рядок-посилання («читайте також», списки інших новин), а не текст статті
        if len(text) >= 25 and text not in parts:
            parts.append(text)
    return "\n".join(parts)


def extract_article(html: str, url: str) -> Article:
    soup = BeautifulSoup(html, "lxml")
    image = _meta(soup, "og:image", "twitter:image")
    if image:
        image = str(httpx.URL(url).join(image))
    published_at = _published_at(soup)

    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()

    candidates: list[Tag] = list(soup.find_all("article"))
    candidates += soup.select(
        '[itemprop="articleBody"], .entry-content, .post-content, .article-content, .news-text, '
        ".td-post-content, .tdb_single_content, .single-content, .post-body, main"
    )
    best = ""
    for container in candidates:
        text = _paragraph_text(container)
        if len(text) > len(best):
            best = text
    if len(best) < 200 and soup.body is not None:
        # Нестандартна верстка — беремо блок, де найбільше тексту в абзацах.
        blocks = [(len(_paragraph_text(d)), d) for d in soup.body.find_all("div", recursive=True) if len(d.find_all("p", recursive=False)) >= 2]
        if blocks:
            fallback = _paragraph_text(max(blocks, key=lambda b: b[0])[1])
            if len(fallback) > len(best):
                best = fallback

    return Article(text=best[:MAX_ARTICLE_CHARS].strip(), image_url=image, published_at=published_at)


class ArticleFetcher:
    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=15, follow_redirects=True, headers=_BROWSER_HEADERS)

    def fetch(self, url: str) -> Article | None:
        try:
            resp = self._http.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("Не вдалося відкрити статтю %s: %s", url, exc)
            return None
        return extract_article(resp.text, url)
