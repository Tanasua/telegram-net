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

Кожне повідомлення про тривогу / відбій супроводжується квадратною картинкою
(жовта/червона для тривоги, зелена для відбою) з лого каналу.  Карту повітряних
загроз (NEPTUN) надсилаємо лише раз — наступним повідомленням після першого
оголошення тривоги в районі.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from telegram_net.config import ChannelConfig, Config
from telegram_net.signature import signature_html, channel_title
from telegram_net.fetchers.base import FetchedItem
from telegram_net.neptun import KYIV, map_caption
from telegram_net.neptun_view import Capturer, MapView, nearby, render_caption

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "assets"
SIZE = 1080

DISTRICTS = ("Житомирський", "Бердичівський", "Коростенський", "Звягельський")
MAX_EVENT_AGE = dt.timedelta(minutes=10)

_LEVEL_SUFFIX_RE = re.compile(r"\s*\((?:жовтий|червоний) рівень\)\s*", re.IGNORECASE)

WHITE = (255, 255, 255)
SOFT = (255, 255, 255, 200)


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


# --- малювання картинок тривоги / відбою (1080×1080, з лого каналу) -----------


def _font(size: int, weight: str = "ExtraBold"):
    from PIL import ImageFont

    font = ImageFont.truetype(str(ASSETS / "fonts" / "Nunito.ttf"), size)
    try:
        font.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return font


def _gradient(top: tuple, bottom: tuple):
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (SIZE, SIZE))
    d = ImageDraw.Draw(img)
    for y in range(SIZE):
        t = y / (SIZE - 1)
        c = tuple(round(a + (b - a) * t) for a, b in zip(top, bottom))
        d.line([(0, y), (SIZE, y)], fill=c + (255,))
    return img


def render_alert_image(district: str, level: str | None, threat: str | None, ch_title: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    if level == "red":
        img = _gradient((180, 30, 30), (120, 15, 15))
    else:
        img = _gradient((180, 140, 20), (140, 100, 10))

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((200, 50, 880, 650), fill=(255, 255, 255, 40))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(100)))

    d = ImageDraw.Draw(img)
    cx = SIZE // 2

    d.text((cx, 300), "ПОВІТРЯНА", font=_font(90), fill=WHITE, anchor="mm")
    d.text((cx, 400), "ТРИВОГА", font=_font(90), fill=WHITE, anchor="mm")

    level_text = {"red": "червоний рівень", "yellow": "жовтий рівень"}.get(level or "", "")
    if level_text:
        pill_font = _font(34, "Bold")
        tw = d.textlength(level_text, font=pill_font)
        px, py = cx - tw / 2 - 30, 460
        pill_fill = (255, 80, 80, 200) if level == "red" else (255, 200, 60, 200)
        pill = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
        ImageDraw.Draw(pill).rounded_rectangle((px, py, px + tw + 60, py + 52), radius=26, fill=pill_fill)
        img.alpha_composite(pill)
        d = ImageDraw.Draw(img)
        d.text((cx, py + 26), level_text, font=pill_font, fill=WHITE, anchor="mm")

    d.text((cx, 580), f"{district} район", font=_font(50, "Bold"), fill=WHITE, anchor="mm")

    if threat:
        d.text((cx, 660), threat, font=_font(36, "SemiBold"), fill=SOFT, anchor="mm")

    d.text((cx, 760), "Бережіть себе!", font=_font(46, "Bold"), fill=WHITE, anchor="mm")

    from telegram_net.heart import draw_title_with_heart

    draw_title_with_heart(img, d, (cx, 960), ch_title, _font(28, "SemiBold"), SOFT)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def render_clear_image(district: str, duration_str: str | None, ch_title: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    img = _gradient((20, 120, 60), (10, 80, 40))

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((200, 100, 880, 700), fill=(100, 255, 160, 45))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(100)))

    d = ImageDraw.Draw(img)
    cx = SIZE // 2

    d.text((cx, 350), "ВІДБІЙ", font=_font(100), fill=WHITE, anchor="mm")
    d.text((cx, 460), "ПОВІТРЯНОЇ ТРИВОГИ", font=_font(60, "Bold"), fill=WHITE, anchor="mm")
    d.text((cx, 560), f"{district} район", font=_font(50, "Bold"), fill=SOFT, anchor="mm")

    if duration_str:
        d.text((cx, 650), f"Тривога тривала {duration_str}", font=_font(36, "SemiBold"), fill=SOFT, anchor="mm")

    from telegram_net.heart import draw_title_with_heart

    draw_title_with_heart(img, d, (cx, 960), ch_title, _font(28, "SemiBold"), SOFT)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


class TextSender(Protocol):
    def send_text(self, chat_id: str, text: str) -> int: ...

    def send_photo_bytes(self, chat_id: str, image: bytes, caption: str | None = None, filename: str = "image.png") -> int: ...

    def send_photo_group_bytes(self, chat_id: str, images: list[bytes], caption: str | None = None) -> list[int]: ...


class AlertRelay:
    def __init__(
        self,
        config: Config,
        sender: TextSender | None,
        map_provider: Callable[[], bytes | None] | None = None,
        local_maps: Capturer | None = None,
    ):
        self._sender = sender
        self._map_provider = map_provider
        self._local_maps = local_maps  # місцева мапа з описом цілей (neptun_view.py) для каналів із полем neptun
        self._max_caption = config.settings.max_caption_chars
        self._channels_by_district: dict[str, list[ChannelConfig]] = {}
        for ch in config.active_channels:
            if ch.alert_district:
                self._channels_by_district.setdefault(ch.alert_district, []).append(ch)
        self.cursor: int | None = None
        self.active: dict[str, dt.datetime] = {}
        self.sent: list[tuple[str, str]] = []

    def dump_state(self) -> dict:
        return {
            "cursor": self.cursor,
            "active": {d: t.isoformat() for d, t in self.active.items()},
        }

    def load_state(self, data: dict) -> None:
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
                        was_active = district in self.active
                        self.active.setdefault(district, at)
                        if fresh:
                            self._announce(event, district, started, at)
                            if not was_active:
                                self._send_map(district, now, event.threat)
                    else:
                        self.active.pop(district, None)
                        if fresh:
                            self._announce(event, district, started, at)
            self.cursor = msg_id if self.cursor is None else max(self.cursor, msg_id)

    def _send_map(self, district: str, now: dt.datetime, threat: str | None = None) -> None:
        """Карта після першого оголошення тривоги в районі. Канали з полем `neptun`
        отримують місцеву мапу з описом цілей; якщо поблизу цілей немає чи зйомка не
        вдалась — як усі інші канали, загальний знімок віджета."""
        generic: list[bytes | None] = []  # лінива зйомка: один знімок на всі канали
        for ch in self._channels_by_district.get(district, []):
            if self._send_local_map(ch, now):
                continue
            if self._map_provider is None:
                continue
            if not generic:
                generic.append(self._map_provider())
            image = generic[0]
            if image is None:
                continue
            cap = map_caption(now, district, threat)
            self.sent.append((ch.telegram_chat_id, f"[карта] {cap}"))
            if self._sender is None:
                logger.info("[пробний] %s: карта NEPTUN (%d байт)", ch.telegram_chat_id, len(image))
                continue
            try:
                self._sender.send_photo_bytes(ch.telegram_chat_id, image, caption=cap, filename="neptun.png")
            except Exception:
                logger.exception("Не вдалося надіслати карту в %s", ch.telegram_chat_id)

    def _send_local_map(self, ch: ChannelConfig, now: dt.datetime) -> bool:
        """True — місцеву мапу надіслано (чи показано в пробному прогоні)."""
        cfg = ch.neptun
        if not cfg or self._local_maps is None:
            return False
        view = MapView(
            lat=cfg["lat"],
            lon=cfg["lon"],
            radius_km=cfg.get("radius_km", 120),
            place_genitive=ch.name_genitive or "",
            wide_radius_km=cfg.get("wide_radius_km"),
        )
        try:
            shots, threats = self._local_maps(view)
            found = nearby(threats, view, now)
            if not found or not shots:
                logger.info("Нептун: поблизу %s цілей немає — шлю загальну карту", ch.channel_id)
                return False
            caption = render_caption(found, view, now.astimezone(KYIV), self._max_caption)
            self.sent.append((ch.telegram_chat_id, f"[карта] {caption}"))
            if self._sender is None:
                logger.info("[пробний] %s: місцева карта NEPTUN (%d кадр.): %s", ch.telegram_chat_id, len(shots), caption.replace("\n", " | "))
                return True
            if len(shots) > 1:
                self._sender.send_photo_group_bytes(ch.telegram_chat_id, shots, caption=caption)
            else:
                self._sender.send_photo_bytes(ch.telegram_chat_id, shots[0], caption=caption, filename="neptun.png")
            return True
        except Exception:
            logger.exception("Не вдалося зробити місцеву карту для %s", ch.channel_id)
            return False

    def _announce(self, event: AlertEvent, district: str, started: dt.datetime | None, at: dt.datetime) -> None:
        base = render_alert(event, district, started, at)
        for ch in self._channels_by_district.get(district, []):
            chat_id = ch.telegram_chat_id
            text = f"{base}\n\n{signature_html(ch)}"
            ch_title = channel_title(ch)
            if event.kind == "alert":
                image = render_alert_image(district, event.level, event.threat, ch_title)
            else:
                dur = format_duration(at - started) if started and at > started else None
                image = render_clear_image(district, dur, ch_title)
            self.sent.append((chat_id, text))
            if self._sender is None:
                logger.info("[пробний] %s: %s", chat_id, text.replace("\n", " | "))
                continue
            try:
                self._sender.send_photo_bytes(chat_id, image, caption=text, filename="alert.png")
            except Exception:
                logger.exception("Не вдалося надіслати сигнал у %s", chat_id)


def run_loop(
    relay: AlertRelay,
    fetch: Callable[[], list[FetchedItem]],
    interval_sec: float = 30,
    max_runtime_min: float | None = None,
    sleep: Callable[[float], None] = time.sleep,
    after_tick: Callable[[], None] | None = None,
    should_stop: Callable[[], bool] | None = None,
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
        if should_stop is not None:
            try:
                if should_stop():
                    logger.info("Зупиняю цикл достроково — наступний запуск продовжить")
                    return
            except Exception:
                logger.exception("Перевірка дострокової зупинки не вдалася — працюю далі")
        sleep(interval_sec)
