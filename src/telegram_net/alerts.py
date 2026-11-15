"""Сигнали повітряної тривоги й відбою — окремий процес без ШІ.

Редактор працює раз на годину, а тривога має з'явитись у каналі за хвилину,
тому це простий цикл: раз на ~30 секунд читає канал Житомирської ОВА (там
сигнали йдуть у стабільному машинному форматі по районах області) і за шаблоном
публікує їх у канали відповідного району:

    🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)
    🔴 Житомирський, Коростенський, Звягельський райони — повітряна тривога, червоний рівень: ракетна загроза.
    🟢 Житомирський район — відбій повітряної тривоги

Стану на диску немає: під час старту процес читає останні повідомлення джерела,
відновлює з них, де тривога зараз триває (щоб порахувати тривалість до відбою),
і публікує лише те, що з'явиться ПІСЛЯ старту.
"""

from __future__ import annotations

import datetime as dt
import logging
import re
import time
from dataclasses import dataclass
from typing import Callable, Protocol

from telegram_net.config import ChannelConfig, Config
from telegram_net.signature import signature_html
from telegram_net.fetchers.base import FetchedItem

logger = logging.getLogger(__name__)

DISTRICTS = ("Житомирський", "Бердичівський", "Коростенський", "Звягельський")
MAX_EVENT_AGE = dt.timedelta(minutes=10)  # після простою не публікуємо застарілі сигнали

_LEVEL_SUFFIX_RE = re.compile(r"\s*\((?:жовтий|червоний) рівень\)\s*", re.IGNORECASE)


@dataclass(frozen=True)
class AlertEvent:
    kind: str  # "alert" | "clear"
    districts: tuple[str, ...]
    level: str | None = None  # "red" | "yellow" | None
    threat: str | None = None


def parse_alert(text: str) -> AlertEvent | None:
    line = text.strip().split("\n", 1)[0].strip()
    if "—" not in line:
        return None
    region, _, rest = line.partition("—")
    rest_low = rest.lower()
    if "відбій" in rest_low and "тривог" in rest_low:
        kind = "clear"
    elif "повітряна тривога" in rest_low:
        kind = "alert"
    else:
        return None
    region_low = region.lower()
    if "област" in region_low:
        if "житомирськ" not in region_low:
            return None
        districts = DISTRICTS
    elif "район" in region_low:
        districts = tuple(d for d in DISTRICTS if d[:-2].lower() in region_low)
    else:
        return None
    if not districts:
        return None
    level = threat = None
    if kind == "alert":
        level = "red" if "червон" in rest_low else "yellow" if "жовт" in rest_low else None
        if ":" in rest:
            threat = _LEVEL_SUFFIX_RE.sub("", rest.split(":", 1)[1]).strip(" .")
            threat = (threat[:1].upper() + threat[1:]) or None
    return AlertEvent(kind=kind, districts=districts, level=level, threat=threat)


def format_duration(delta: dt.timedelta) -> str:
    minutes = max(1, round(delta.total_seconds() / 60))
    hours, minutes = divmod(minutes, 60)
    if hours and minutes:
        return f"{hours} год {minutes} хв"
    return f"{hours} год" if hours else f"{minutes} хв"


def render_alert(event: AlertEvent, district: str, started_at: dt.datetime | None, at: dt.datetime) -> str:
    if event.kind == "clear":
        text = f"🟢 <b>Відбій повітряної тривоги</b>\n{district} район"
        if started_at is not None and at > started_at:
            text += f"\nТривога тривала {format_duration(at - started_at)}."
        return text
    icon = "🔴" if event.level == "red" else "🟡"
    level = {"red": ", червоний рівень", "yellow": ", жовтий рівень"}.get(event.level or "", "")
    text = f"{icon} <b>Повітряна тривога{level}</b>\n{district} район"
    if event.threat:
        text += f"\n{event.threat}."
    return text


class TextSender(Protocol):
    def send_text(self, chat_id: str, text: str) -> int: ...


class AlertRelay:
    def __init__(self, config: Config, sender: TextSender | None):
        self._sender = sender
        self._channels_by_district: dict[str, list[ChannelConfig]] = {}
        for ch in config.active_channels:
            if ch.alert_district:
                self._channels_by_district.setdefault(ch.alert_district, []).append(ch)
        self.cursor: int | None = None
        self.active: dict[str, dt.datetime] = {}  # район -> коли почалась тривога
        self.sent: list[tuple[str, str]] = []  # (chat_id, текст) — для логу й тестів

    # --- стан між перезапусками (GitHub Actions перезапускає цикл кожні ~5 год) ---
    def dump_state(self) -> dict:
        return {"cursor": self.cursor, "active": {d: t.isoformat() for d, t in self.active.items()}}

    def load_state(self, data: dict) -> None:
        """Продовжити з місця, де зупинився попередній запуск: сигнали, що прийшли
        під час перезапуску (новіші за курсор і не старші за 10 хв), буде надіслано."""
        self.cursor = data.get("cursor")
        self.active = {d: dt.datetime.fromisoformat(t) for d, t in (data.get("active") or {}).items()}

    def process(self, items: list[FetchedItem], now: dt.datetime) -> None:
        """items — повідомлення джерела від старішого до новішого."""
        bootstrap = self.cursor is None
        for item in items:
            msg_id = int(item.external_id)
            if self.cursor is not None and msg_id <= self.cursor:
                continue
            event = parse_alert(item.text)
            at = item.published_at or now
            if event is not None:
                fresh = not bootstrap and now - at <= MAX_EVENT_AGE
                for district in event.districts:
                    started = self.active.get(district)
                    if event.kind == "alert":
                        self.active.setdefault(district, at)
                    else:
                        self.active.pop(district, None)
                    if fresh:
                        self._announce(render_alert(event, district, started, at), district)
            self.cursor = msg_id if self.cursor is None else max(self.cursor, msg_id)

    def _announce(self, base: str, district: str) -> None:
        for ch in self._channels_by_district.get(district, []):
            chat_id = ch.telegram_chat_id
            text = f"{base}\n\n{signature_html(ch)}"
            self.sent.append((chat_id, text))
            if self._sender is None:
                logger.info("[пробний] %s: %s", chat_id, text.replace("\n", " | "))
                continue
            try:
                self._sender.send_text(chat_id, text)
            except Exception:
                logger.exception("Не вдалося надіслати сигнал у %s", chat_id)


def run_loop(
    relay: AlertRelay,
    fetch: Callable[[], list[FetchedItem]],
    interval_sec: float = 30,
    max_runtime_min: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    after_tick: Callable[[], None] | None = None,
) -> None:
    started = time.monotonic()
    while True:
        try:
            relay.process(fetch(), dt.datetime.now(dt.timezone.utc))
        except Exception:
            logger.exception("Не вдалося прочитати джерело сигналів — спробую ще раз")
        if after_tick is not None:
            after_tick()
        if max_runtime_min is not None and (time.monotonic() - started) / 60 >= max_runtime_min:
            return
        sleep(interval_sec)
