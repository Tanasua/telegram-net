"""Читання ЧУЖИХ публічних каналів-джерел через Telethon (MTProto-юзерсесія).

Чому не t.me/s/... (без логіну): прев'ю-сторінка віддає стиснуті превью і часто
не дає прямого посилання на відеофайл — а вимога "заливати відео" і "робити
галерею" потребує оригінальної якості медіа. Telethon читає це напряму.

Трансформація повідомлень (group_messages_into_items) винесена в чисті функції,
не залежні від живого TelegramClient — це тестується без мережі.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Iterable, Protocol

from telegram_net.config import SourceConfig
from telegram_net.fetchers.base import FetchedItem, FetchedMedia


class TelegramMessageLike(Protocol):
    """Мінімальний інтерфейс, який нам потрібен від telethon.tl.custom.message.Message."""

    id: int
    date: dt.datetime | None
    message: str | None
    grouped_id: int | None
    photo: Any | None
    video: Any | None


def _extract_media(msg: TelegramMessageLike) -> FetchedMedia | None:
    if getattr(msg, "photo", None) is not None:
        return FetchedMedia(type="photo", url=f"telethon://photo/{msg.id}")
    video = getattr(msg, "video", None)
    if video is not None:
        duration = getattr(video, "duration", None)
        return FetchedMedia(type="video", url=f"telethon://video/{msg.id}", duration_sec=duration)
    return None


def group_messages_into_items(
    messages: Iterable[TelegramMessageLike], channel_handle: str
) -> list[FetchedItem]:
    """Telethon повертає кожен елемент альбому окремим повідомленням з однаковим
    grouped_id — тут вони об'єднуються назад в один FetchedItem з кількома media,
    інакше кожне фото галереї спливло б окремим постом."""

    singles: list[FetchedItem] = []
    groups: dict[int, list[TelegramMessageLike]] = {}
    group_order: list[int] = []

    for msg in messages:
        gid = getattr(msg, "grouped_id", None)
        if gid is None:
            media = _extract_media(msg)
            singles.append(
                FetchedItem(
                    external_id=str(msg.id),
                    text=getattr(msg, "message", "") or "",
                    url=f"https://t.me/{channel_handle}/{msg.id}",
                    published_at=getattr(msg, "date", None),
                    media=(media,) if media else (),
                )
            )
        else:
            if gid not in groups:
                groups[gid] = []
                group_order.append(gid)
            groups[gid].append(msg)

    grouped_items: list[FetchedItem] = []
    for gid in group_order:
        msgs = sorted(groups[gid], key=lambda m: m.id)
        text = next((m.message for m in msgs if getattr(m, "message", "")), "") or ""
        media = tuple(m for m in (_extract_media(msg) for msg in msgs) if m is not None)
        first = msgs[0]
        grouped_items.append(
            FetchedItem(
                external_id=str(gid),
                text=text,
                url=f"https://t.me/{channel_handle}/{first.id}",
                published_at=getattr(first, "date", None),
                media=media,
            )
        )

    return singles + grouped_items


_MAX_MESSAGES_PER_FETCH = 200


class TelethonFetcher:
    def __init__(self, client: Any):
        self._client = client

    def fetch_new(self, source: SourceConfig, cursor: str | None) -> tuple[list[FetchedItem], str | None]:
        handle = source.handle_or_url.strip().lstrip("@").rstrip("/").rsplit("/", 1)[-1]

        if cursor is None:
            # Перше підключення цього джерела: НЕ заливаємо весь історичний архів
            # каналу (для активних каналів це можуть бути тисячі старих повідомлень —
            # вони з'їдять увесь час прогону на класифікацію/генерацію і виглядатимуть
            # як спам одразу після підключення). Просто фіксуємо поточну позицію;
            # обробляти будемо тільки те, що з'явиться в каналі після цього моменту.
            latest = list(self._client.iter_messages(handle, limit=1))
            if not latest:
                return [], None
            return [], str(latest[0].id)

        min_id = int(cursor)

        # Свідомо НЕ reverse=True + min_id у самому запиті: ця комбінація має
        # задокументовану історію крихких edge-кейсів у Telethon (issues
        # LonamiWebs/Telethon #4453, #3847, #4579 — тихо повертає порожньо чи
        # обривається завчасно). Натомість читаємо найновіші-першими (сама
        # протоптана дорога бібліотеки, той самий виклик, що й для cold-start
        # вище) і самі зупиняємось, щойно дійшли до вже баченого повідомлення.
        new_messages: list[Any] = []
        for msg in self._client.iter_messages(handle, limit=_MAX_MESSAGES_PER_FETCH):
            if msg.id <= min_id:
                break
            new_messages.append(msg)

        if not new_messages:
            return [], cursor

        items = group_messages_into_items(new_messages, handle)
        new_cursor = str(max(m.id for m in new_messages))
        return items, new_cursor
