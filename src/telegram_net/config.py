"""Конфіг: канали мережі, джерела й спільні налаштування. Єдине джерело правди —
JSON-файл у репозиторії (data/sources_seed.json), його легко правити руками чи
попросити Claude.

Джерело має scope:
- "local"    — про одне місто, прив'язане до channel_id;
- "oblast"   — обласне (поліція, ОВА, Суспільне Житомир): читається ОДИН раз за
               прогін, а редактор вирішує, у які канали мережі піде новина;
- "national" — всеукраїнське: так само один раз на всю мережу.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

SourceType = Literal["telegram", "website", "web_scrape", "facebook_page"]
Scope = Literal["local", "oblast", "national"]


@dataclass(frozen=True)
class SourceConfig:
    channel_id: str | None
    type: SourceType
    name: str
    handle_or_url: str
    active: bool = True
    note: str = ""
    scope: Scope = "local"
    # Тільки для type="web_scrape" (сайт без RSS): CSS-селектор картки новини
    # і, опційно, заголовка всередині неї. Див. fetchers/scrape_fetcher.py.
    scrape_item_selector: str | None = None
    scrape_title_selector: str | None = None

    @property
    def key(self) -> str:
        """Ключ стану (курсор, здоров'я джерела). Для обласних/всеукраїнських
        джерел не залежить від каналу — тому джерело читається один раз."""
        owner = self.channel_id if self.scope == "local" else self.scope
        return f"{owner}|{self.type}|{self.handle_or_url}"


@dataclass(frozen=True)
class ChannelConfig:
    channel_id: str
    name: str
    telegram_chat_id: str | None
    active: bool = True
    alert_district: str | None = None  # район області для сигналів тривоги, напр. "Коростенський"
    daily_limit: int = 12  # максимум постів на добу (без тривог)
    max_posts_per_run: int = 2
    subscribe_cta: str | None = None
    cta_every: int = 4  # заклик підписатись — у кожному N-му пості, не в кожному


@dataclass(frozen=True)
class Settings:
    timezone: str = "Europe/Kyiv"
    quiet_from_hour: int = 23  # нічна тиша: з цієї години...
    quiet_to_hour: int = 7  # ...до цієї новини не публікуються (тривоги — завжди)
    max_age_hours: float = 12  # старіші новини в кандидати не потрапляють
    pool_ttl_hours: float = 10  # скільки невикористаний кандидат чекає в пулі
    national_daily_limit: int = 4  # всеукраїнських постів на канал за добу
    max_caption_chars: int = 1000  # ліміт Telegram на підпис до фото — 1024
    max_text_chars: int = 3500
    alerts_source: str = "zhytomyrmilitaryadministration"
    # Лагідні звертання до підписників: код підказує редактору, коли й яке
    # вжити (кожен N-й пост каналу, по колу), щоб вони не зникали й не частили.
    addresses: tuple[str, ...] = (
        "Котики",
        "Друзі",
        "Любі друзі",
        "Хлопці й дівчатка",
        "Шановні",
        "Хороші мої",
        "Любчики-голубчики",
        "Сонечки",
    )
    address_every: int = 3


@dataclass(frozen=True)
class Config:
    settings: Settings
    channels: list[ChannelConfig] = field(default_factory=list)
    sources: list[SourceConfig] = field(default_factory=list)

    def channel(self, channel_id: str) -> ChannelConfig | None:
        return next((c for c in self.channels if c.channel_id == channel_id), None)

    @property
    def active_channels(self) -> list[ChannelConfig]:
        return [c for c in self.channels if c.active and c.telegram_chat_id]


DEFAULT_CONFIG_PATH = "data/sources_seed.json"


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_settings = dict(data.get("settings", {}))
    if "addresses" in raw_settings:
        raw_settings["addresses"] = tuple(raw_settings["addresses"])
    settings = Settings(**raw_settings)
    channels = [
        ChannelConfig(
            channel_id=c["channel_id"],
            name=c["name"],
            telegram_chat_id=c.get("telegram_chat_id") or None,
            active=c.get("active", True),
            alert_district=c.get("alert_district"),
            daily_limit=c.get("daily_limit", 12),
            max_posts_per_run=c.get("max_posts_per_run", 2),
            subscribe_cta=c.get("subscribe_cta") or f"Підписуйся на {c['name']}",
            cta_every=c.get("cta_every", 4),
        )
        for c in data["channels"]
    ]
    sources = []
    for s in data["sources"]:
        scope = s.get("scope", "local")
        if scope == "local" and not s.get("channel_id"):
            raise ValueError(f"Локальне джерело без channel_id: {s.get('name')}")
        sources.append(
            SourceConfig(
                channel_id=s.get("channel_id") if scope == "local" else None,
                type=s["type"],
                name=s["name"],
                handle_or_url=s["handle_or_url"],
                active=s.get("active", True),
                note=s.get("note", ""),
                scope=scope,
                scrape_item_selector=s.get("scrape_item_selector") or None,
                scrape_title_selector=s.get("scrape_title_selector") or None,
            )
        )
    return Config(settings=settings, channels=channels, sources=sources)
