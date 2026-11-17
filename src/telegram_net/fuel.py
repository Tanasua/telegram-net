"""Обідня інфографіка «Ціни на пальне» — окрема від курсу валют.

Дані: середні ціни по Житомирській області з index.minfin.com.ua.
Три колірні варіанти на ротації (день року % 3), щоб не виглядало автоматично.
Картинка квадратна (1080×1080) — Telegram обрізає вищі.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import logging
from dataclasses import dataclass
from pathlib import Path

from telegram_net.markets import (
    ASSETS,
    FUELS,
    MONTHS_GEN,
    Fuel,
    Markets,
    fetch_markets,
    fmt_change,
    fmt_money,
)

logger = logging.getLogger(__name__)

SIZE = 1080
WHITE = (255, 255, 255)
SOFT = (255, 255, 255, 205)
FAINT = (255, 255, 255, 130)
UP = (255, 120, 110)
DOWN = (110, 220, 160)


@dataclass(frozen=True)
class _Palette:
    top: tuple[int, int, int]
    bottom: tuple[int, int, int]
    card_fill: tuple[int, int, int, int]
    card_outline: tuple[int, int, int, int]
    accent: tuple[int, int, int]
    glow_pos: tuple[int, int, int, int]  # ellipse bbox
    glow_fill: tuple[int, int, int, int]


_PALETTES = (
    _Palette(
        top=(180, 83, 9), bottom=(120, 53, 15),
        card_fill=(255, 255, 255, 26), card_outline=(255, 255, 255, 50),
        accent=(255, 191, 36),
        glow_pos=(620, -180, 1260, 420),
        glow_fill=(255, 200, 100, 60),
    ),
    _Palette(
        top=(30, 41, 59), bottom=(15, 23, 42),
        card_fill=(255, 255, 255, 20), card_outline=(255, 255, 255, 40),
        accent=(56, 189, 248),
        glow_pos=(-200, -180, 440, 420),
        glow_fill=(56, 189, 248, 40),
    ),
    _Palette(
        top=(6, 78, 59), bottom=(2, 44, 34),
        card_fill=(255, 255, 255, 22), card_outline=(255, 255, 255, 45),
        accent=(52, 211, 153),
        glow_pos=(620, 580, 1260, 1180),
        glow_fill=(52, 211, 153, 40),
    ),
)


def _font(size: int, weight: str = "ExtraBold"):
    from PIL import ImageFont

    font = ImageFont.truetype(str(ASSETS / "fonts" / "Nunito.ttf"), size)
    try:
        font.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return font


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _change_color(v: float | None):
    if v is None or abs(v) < 0.005:
        return FAINT
    return UP if v > 0 else DOWN


def render(m: Markets, channel_title: str, day: dt.date, variant: int | None = None) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    if variant is None:
        variant = day.timetuple().tm_yday % 3
    pal = _PALETTES[variant % len(_PALETTES)]

    img = Image.new("RGBA", (SIZE, SIZE))
    g = ImageDraw.Draw(img)
    for y in range(SIZE):
        g.line([(0, y), (SIZE, y)], fill=_mix(pal.top, pal.bottom, y / (SIZE - 1)) + (255,))

    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse(pal.glow_pos, fill=pal.glow_fill)
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))

    layer = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    texts: list = []
    changes: list = []

    def text(xy, s, font, fill, anchor="mm"):
        texts.append((xy, s, font, fill, anchor))

    cx = SIZE // 2
    text((cx, 88), "Ціни на пальне", _font(70), WHITE)
    text((cx, 150), f"{day.day} {MONTHS_GEN[day.month - 1]} · Житомирщина", _font(34, "SemiBold"), SOFT)

    y0 = 210
    ld.rounded_rectangle(
        (50, y0, SIZE - 50, 820),
        radius=40,
        fill=pal.card_fill,
        outline=pal.card_outline,
        width=2,
    )
    text((90, y0 + 46), "Ціни на пальне", _font(38), WHITE, "lm")
    text((SIZE - 90, y0 + 46), "середня ціна, грн/л", _font(26, "SemiBold"), FAINT, "rm")

    subtitles = dict(FUELS)
    row_y = y0 + 105
    for f in m.fuels:
        row_y += 10
        ld.rounded_rectangle((90, row_y - 30, 250, row_y + 30), radius=30, fill=(255, 255, 255, 40))
        text((170, row_y), f.name, _font(30), WHITE)
        sub = subtitles.get(f.name)
        if sub:
            text((280, row_y), sub, _font(28, "SemiBold"), FAINT, "lm")
        text((800, row_y), fmt_money(f.price), _font(50), WHITE, "rm")
        changes.append(((SIZE - 90, row_y), f.change))
        row_y += 90

    cheap = [f"{fuel}: {net} {fmt_money(price)}" for fuel, (net, price) in m.cheapest.items()]
    if cheap:
        text((cx, 785), "Найдешевше · " + " · ".join(cheap), _font(26, "Bold"), pal.accent + (255,))

    text((cx, 890), "index.minfin.com.ua", _font(22, "SemiBold"), FAINT)

    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    for xy, s, font, fill, anchor in texts:
        d.text(xy, s, font=font, fill=fill, anchor=anchor)

    change_font = _font(28, "Bold")
    for (x, y), value in changes:
        color = _change_color(value)
        label = fmt_change(value)
        d.text((x, y), label, font=change_font, fill=color, anchor="rm")
        left = x - d.textlength(label, font=change_font) - 12
        if value is None or abs(value) < 0.005:
            continue
        if value > 0:
            d.polygon([(left - 18, y + 8), (left, y + 8), (left - 9, y - 9)], fill=color)
        else:
            d.polygon([(left - 18, y - 8), (left, y - 8), (left - 9, y + 9)], fill=color)

    from telegram_net.heart import draw_title_with_heart

    draw_title_with_heart(img, d, (cx, 940), channel_title, _font(28, "SemiBold"), (255, 255, 255, 200))

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# --- тексти ---------------------------------------------------------------------


def caption(m: Markets) -> str:
    fuels = {f.name: f for f in m.fuels}
    parts = [f"{n} — {fmt_money(fuels[n].price)}" for n in ("А-95", "ДП", "Газ") if n in fuels]
    return "Середні ціни на пальне на Житомирщині: " + ", ".join(parts) + " грн/л." if parts else ""


def post_caption(m: Markets, ch) -> str:
    from telegram_net.signature import signature_html

    return f"<b>Ціни на пальне</b>\n{html.escape(caption(m))}\n\n{signature_html(ch)}"


# --- публікація -----------------------------------------------------------------


def publish_fuel(config, state, publisher, now: dt.datetime | None = None, force: bool = False, out_dir: Path | None = None, fetch=fetch_markets) -> list[str]:
    from telegram_net.clock import local_now, start_of_local_day
    from telegram_net.signature import channel_title
    from telegram_net.state import iso

    now = now or dt.datetime.now(dt.timezone.utc)
    local = local_now(config, now)
    hour = getattr(config.settings, "fuel_hour", 12)
    if not force and not (hour <= local.hour < hour + 2):
        return [f"Зараз {local:%H:%M} за Києвом — пальне виходить з {hour}:00 до {hour + 2}:00, пропускаю"]
    day_start = start_of_local_day(config, now)
    channels = [ch for ch in config.active_channels if publisher is None or not state.posts_since(ch.channel_id, day_start, kind="fuel")]
    if not channels:
        return ["Ціни на пальне сьогодні вже були"]
    try:
        markets = fetch()
    except Exception as exc:
        logger.exception("Не вдалося отримати ціни на пальне")
        return [f"ПОМИЛКА даних — {exc}"]

    lines = []
    for ch in channels:
        image = render(markets, channel_title(ch), local.date())
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"fuel_{ch.channel_id}.png").write_bytes(image)
        if publisher is None:
            lines.append(f"{ch.channel_id}: [пробний] {caption(markets)}")
            continue
        try:
            message_id = publisher.send_photo_bytes(ch.telegram_chat_id, image, post_caption(markets, ch), f"fuel_{ch.channel_id}.png")
        except Exception as exc:
            logger.exception("Не вдалося надіслати пальне в %s", ch.channel_id)
            lines.append(f"{ch.channel_id}: ПОМИЛКА Telegram — {exc}")
            continue
        state.log_post(
            {
                "channel_id": ch.channel_id,
                "published_at": iso(now),
                "kind": "fuel",
                "text": caption(markets),
                "candidate_ids": [],
                "message_ids": [message_id],
            }
        )
        lines.append(f"{ch.channel_id}: надіслано — {caption(markets)}")
    return lines
