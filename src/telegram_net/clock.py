"""Київський час: нічна тиша й початок доби. Окремий модуль без сторонніх
залежностей — ним користується крок публікації, який у GitHub Actions
ставить лише httpx."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from telegram_net.config import Config


def local_now(config: Config, now: dt.datetime) -> dt.datetime:
    return now.astimezone(ZoneInfo(config.settings.timezone))


def is_quiet(config: Config, now: dt.datetime) -> bool:
    hour = local_now(config, now).hour
    start, end = config.settings.quiet_from_hour, config.settings.quiet_to_hour
    return hour >= start or hour < end if start > end else start <= hour < end


def start_of_local_day(config: Config, now: dt.datetime) -> dt.datetime:
    return local_now(config, now).replace(hour=0, minute=0, second=0, microsecond=0)
