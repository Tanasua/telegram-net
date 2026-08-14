"""Конфіг-шар: список каналів і джерел. Google Sheets — робоче джерело правди,
LocalJSONConfigSource — той самий формат локально (для розробки й тестів без
живого доступу до Sheets)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

SourceType = Literal["telegram", "website", "facebook_page"]


@dataclass(frozen=True)
class SourceConfig:
    channel_id: str
    type: SourceType
    name: str
    handle_or_url: str
    active: bool = True
    note: str = ""


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    name: str
    telegram_chat_id: str | None
    active: bool = True
    min_relevance: float = 0.5
    max_posts_per_run: int = 1
    max_age_minutes: int = 60
    length_weights: dict[str, float] = field(
        default_factory=lambda: {"short": 0.4, "medium": 0.35, "long": 0.25}
    )
    headline_probability: float = 0.4
    timezone: str = "Europe/Kyiv"


class ConfigSource(Protocol):
    def load_channels(self) -> list[ChannelConfig]: ...
    def load_sources(self) -> list[SourceConfig]: ...


class LocalJSONConfigSource:
    """Формат файлу — див. data/sources_seed.json."""

    def __init__(self, path: str | Path):
        self._path = Path(path)

    def _load(self) -> dict:
        return json.loads(self._path.read_text(encoding="utf-8"))

    def load_channels(self) -> list[ChannelConfig]:
        data = self._load()
        return [
            ChannelConfig(
                channel_id=c["channel_id"],
                name=c["name"],
                telegram_chat_id=c.get("telegram_chat_id") or None,
                active=c.get("active", True),
                min_relevance=c.get("min_relevance", 0.5),
                max_posts_per_run=c.get("max_posts_per_run", 1),
                max_age_minutes=c.get("max_age_minutes", 60),
                length_weights=c.get(
                    "length_weights", {"short": 0.4, "medium": 0.35, "long": 0.25}
                ),
                headline_probability=c.get("headline_probability", 0.4),
                timezone=c.get("timezone", "Europe/Kyiv"),
            )
            for c in data["channels"]
        ]

    def load_sources(self) -> list[SourceConfig]:
        data = self._load()
        return [
            SourceConfig(
                channel_id=s["channel_id"],
                type=s["type"],
                name=s["name"],
                handle_or_url=s["handle_or_url"],
                active=s.get("active", True),
                note=s.get("note", ""),
            )
            for s in data["sources"]
        ]


class GoogleSheetsConfigSource:
    """Лист 'Channels' — перелік каналів. Один лист на кожен channel_id — його джерела.

    Очікувані заголовки:
      Channels:     channel_id | Райцентр | telegram_chat_id | Активний | Мін. релевантність |
                    Макс. постів за прогін | Макс. вік новини (хв)
      <channel_id>: Тип | Назва | URL/handle | Активне | Примітка
    """

    CHANNELS_SHEET = "Channels"

    def __init__(self, spreadsheet_id: str, credentials_path: str):
        import gspread

        gc = gspread.service_account(filename=credentials_path)
        self._sheet = gc.open_by_key(spreadsheet_id)

    def load_channels(self) -> list[ChannelConfig]:
        ws = self._sheet.worksheet(self.CHANNELS_SHEET)
        rows = ws.get_all_records()
        return [
            ChannelConfig(
                channel_id=str(r["channel_id"]),
                name=str(r["Райцентр"]),
                telegram_chat_id=str(r["telegram_chat_id"]) or None,
                active=str(r.get("Активний", "Так")).strip().lower() == "так",
                min_relevance=float(r.get("Мін. релевантність") or 0.5),
                max_posts_per_run=int(r.get("Макс. постів за прогін") or 1),
                max_age_minutes=int(r.get("Макс. вік новини (хв)") or 60),
            )
            for r in rows
            if r.get("channel_id")
        ]

    def load_sources(self) -> list[SourceConfig]:
        type_map = {"сайт": "website", "телеграм": "telegram", "facebook сторінка": "facebook_page"}
        sources: list[SourceConfig] = []
        for channel in self.load_channels():
            ws = self._sheet.worksheet(channel.channel_id)
            for r in ws.get_all_records():
                raw_type = str(r.get("Тип", "")).strip().lower()
                mapped_type = type_map.get(raw_type)
                if mapped_type is None or not r.get("URL/handle"):
                    continue
                sources.append(
                    SourceConfig(
                        channel_id=channel.channel_id,
                        type=mapped_type,  # type: ignore[arg-type]
                        name=str(r.get("Назва", "")),
                        handle_or_url=str(r["URL/handle"]),
                        active=str(r.get("Активне", "Так")).strip().lower() == "так",
                        note=str(r.get("Примітка", "")),
                    )
                )
        return sources
