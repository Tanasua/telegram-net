"""Публічні телеграм-канали через веб-версію https://t.me/s/<канал>.

Замінює Telethon: не потрібні ні API-ключі Telegram, ні юзер-сесія, а головне —
фото й відео приходять звичайними https-посиланнями, які Bot API приймає напряму.
(З Telethon медіа зберігались як внутрішнє "telethon://photo/…", бот не міг їх
надіслати, і всі 97 постів із телеграм-джерел застрягли в чернетках.)

Обмеження веб-версії: лише публічні канали з увімкненим переглядом, ~20
останніх повідомлень на сторінку (старіші — через ?before=<id>), великі відео
віддаються без посилання на файл — тоді беремо кадр-обкладинку як фото.
"""

from __future__ import annotations

import datetime as dt
import logging
import re

import httpx
from bs4 import BeautifulSoup, Tag

from telegram_net.config import SourceConfig
from telegram_net.fetchers.base import FetchedItem, FetchedMedia

logger = logging.getLogger(__name__)

_BG_URL_RE = re.compile(r"background-image:\s*url\(['\"]?(.+?)['\"]?\)")
_MAX_EXTRA_PAGES = 2


def channel_handle(handle_or_url: str) -> str:
    return handle_or_url.strip().lstrip("@").rstrip("/").rsplit("/", 1)[-1]


def parse_duration(text: str) -> float | None:
    parts = text.strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return float(seconds)


def _bg_url(el: Tag | None) -> str | None:
    if el is None:
        return None
    match = _BG_URL_RE.search(el.get("style", ""))
    return match.group(1) if match else None


def _message_text(msg: Tag) -> str:
    el = msg.select_one("div.tgme_widget_message_text.js-message_text")
    if el is None:
        return ""
    for br in el.find_all("br"):
        br.replace_with("\n")
    lines = [re.sub(r"[ \t\xa0]+", " ", line).strip() for line in el.get_text("").split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def _message_media(msg: Tag) -> tuple[FetchedMedia, ...]:
    media: list[FetchedMedia] = []
    # Порядок у розмітці = порядок в альбомі.
    for el in msg.select("a.tgme_widget_message_photo_wrap, a.tgme_widget_message_video_player"):
        classes = el.get("class", [])
        if "tgme_widget_message_photo_wrap" in classes:
            url = _bg_url(el)
            if url:
                media.append(FetchedMedia(type="photo", url=url))
            continue
        video = el.select_one("video[src]")
        duration_el = el.select_one(".message_video_duration")
        duration = parse_duration(duration_el.get_text()) if duration_el else None
        if video is not None and video.get("src"):
            media.append(FetchedMedia(type="video", url=video["src"], duration_sec=duration))
        else:
            thumb = _bg_url(el.select_one(".tgme_widget_message_video_thumb"))
            if thumb:
                media.append(FetchedMedia(type="photo", url=thumb))
    return tuple(media)


def parse_channel_page(html: str) -> list[tuple[int, FetchedItem]]:
    """(id повідомлення, елемент) у порядку від старішого до новішого."""
    soup = BeautifulSoup(html, "lxml")
    result: list[tuple[int, FetchedItem]] = []
    for msg in soup.select("div.tgme_widget_message[data-post]"):
        post = msg["data-post"]
        try:
            msg_id = int(post.rsplit("/", 1)[-1])
        except ValueError:
            continue
        if "service_message" in msg.get("class", []):
            continue  # "закріплено повідомлення" тощо
        time_el = msg.select_one("a.tgme_widget_message_date time[datetime]") or msg.select_one("time[datetime]")
        published_at = None
        if time_el is not None:
            try:
                published_at = dt.datetime.fromisoformat(time_el["datetime"])
            except ValueError:
                published_at = None
        text = _message_text(msg)
        media = _message_media(msg)
        if not text and not media:
            continue
        result.append(
            (
                msg_id,
                FetchedItem(
                    external_id=str(msg_id),
                    text=text,
                    url=f"https://t.me/{post}",
                    published_at=published_at,
                    media=media,
                ),
            )
        )
    result.sort(key=lambda pair: pair[0])
    return result


class TmeFetcher:
    def __init__(self, http_client: httpx.Client | None = None):
        self._http = http_client or httpx.Client(timeout=20, follow_redirects=False)

    def _page(self, handle: str, before: int | None = None) -> list[tuple[int, FetchedItem]]:
        params = {"before": str(before)} if before else None
        resp = self._http.get(f"https://t.me/s/{handle}", params=params)
        if resp.status_code in (301, 302):
            # t.me перекидає на t.me/<канал>, коли веб-перегляд недоступний
            # (приватний канал, вимкнений перегляд, неіснуюче ім'я).
            raise RuntimeError(f"канал @{handle} недоступний через t.me/s (приватний або не існує)")
        resp.raise_for_status()
        return parse_channel_page(resp.text)

    def latest(self, handle_or_url: str) -> list[FetchedItem]:
        """Останні повідомлення каналу без курсора — для читання власних каналів мережі."""
        return [item for _, item in self._page(channel_handle(handle_or_url))]

    def fetch_new(self, source: SourceConfig, cursor: str | None) -> tuple[list[FetchedItem], str | None]:
        handle = channel_handle(source.handle_or_url)
        messages = self._page(handle)
        if not messages:
            return [], cursor

        if cursor is None:
            # Перше підключення: віддаємо лише поточну сторінку. Дата в телеграм-
            # повідомлень надійна, тож старе відсіє поріг свіжості в collect.py —
            # окрема "базова лінія" тут не потрібна.
            return [item for _, item in messages], str(messages[-1][0])

        last_seen = int(cursor)
        pages = 0
        # Якщо за час між прогонами вийшло більше, ніж уміщує сторінка, — дочитуємо назад.
        while messages[0][0] > last_seen + 1 and pages < _MAX_EXTRA_PAGES:
            older = self._page(handle, before=messages[0][0])
            if not older:
                break
            messages = older + messages
            pages += 1

        new = [(mid, item) for mid, item in messages if mid > last_seen]
        if not new:
            return [], cursor
        return [item for _, item in new], str(new[-1][0])
