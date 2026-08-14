"""Розбивка медіа на пачки, придатні для Telegram Bot API.

sendMediaGroup приймає 2-10 елементів за раз (підтверджено з core.telegram.org/bots/api
на серпень 2026) — тож галерея з 15 фото піде як дві пачки: 10 + 5.
"""

from __future__ import annotations

from telegram_net.fetchers.base import FetchedMedia

MAX_MEDIA_GROUP_SIZE = 10


def build_media_batches(media: tuple[FetchedMedia, ...]) -> list[tuple[FetchedMedia, ...]]:
    if not media:
        return []
    return [
        media[i : i + MAX_MEDIA_GROUP_SIZE]
        for i in range(0, len(media), MAX_MEDIA_GROUP_SIZE)
    ]
