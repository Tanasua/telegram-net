from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Literal, Protocol

from telegram_net.config import SourceConfig

MediaType = Literal["photo", "video"]


@dataclass(frozen=True)
class FetchedMedia:
    type: MediaType
    url: str
    duration_sec: float | None = None


@dataclass(frozen=True)
class FetchedItem:
    external_id: str
    text: str
    url: str
    published_at: dt.datetime | None
    media: tuple[FetchedMedia, ...] = ()


class Fetcher(Protocol):
    """cursor — те, що зберігається в Source.last_seen_cursor і повертається назад
    оновленим, щоб наступний запуск читав тільки нове."""

    def fetch_new(self, source: SourceConfig, cursor: str | None) -> tuple[list[FetchedItem], str | None]: ...
