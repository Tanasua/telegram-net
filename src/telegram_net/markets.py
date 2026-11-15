"""Вечірня інфографіка «Курс валют і пальне» — без жодної моделі, лише код.

Джерела (безкоштовно, без ключів):
- офіційний курс — НБУ (bank.gov.ua, NBUStatService);
- готівковий курс — ПриватБанк (api.privatbank.ua, курс у відділеннях);
- пальне — середні ціни по Житомирській області й ціни мереж АЗС з
  index.minfin.com.ua (robots.txt дозволяє).

Картинка: долар, євро й злотий (офіційний курс зі зміною за день, купівля й
продаж готівкою), середні ціни на А-95+, А-95, А-92, дизель і газ зі зміною,
та найдешевші мережі на А-95 і дизель.
"""

from __future__ import annotations

import datetime as dt
import html
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "assets"
FUEL_URL = "https://index.minfin.com.ua/ua/markets/fuel/reg/zhitomirskaya/"
NBU_URL = "https://bank.gov.ua/NBUStatService/v1/statdirectory/exchange"
PRIVAT_URL = "https://api.privatbank.ua/p24api/pubinfo"
CURRENCIES = (("USD", "Долар", "$"), ("EUR", "Євро", "€"), ("PLN", "Злотий", "zł"))
FUELS = (("А-95+", "преміум"), ("А-95", ""), ("А-92", ""), ("ДП", "дизель"), ("Газ", "автогаз"))
_BROWSER = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept-Language": "uk-UA,uk;q=0.9",
}
MONTHS_GEN = ("січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня")


@dataclass
class Rate:
    code: str
    official: float
    change: float | None = None  # до попереднього дня
    buy: float | None = None  # готівка, ПриватБанк
    sell: float | None = None


@dataclass
class Fuel:
    name: str
    price: float
    change: float | None = None


@dataclass
class Markets:
    rate_date: dt.date
    rates: list[Rate]
    fuels: list[Fuel]
    cheapest: dict[str, tuple[str, float]] = field(default_factory=dict)  # вид пального → (мережа, ціна)


# --- дані -----------------------------------------------------------------------


def _num(text: str) -> float | None:
    text = text.strip().replace("\xa0", "").replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def fetch_nbu(http: httpx.Client, day: dt.date | None = None) -> tuple[dt.date, dict[str, float]]:
    params = {"json": ""}
    if day is not None:
        params["date"] = day.strftime("%Y%m%d")
    resp = http.get(NBU_URL, params=params)
    resp.raise_for_status()
    data = resp.json()
    rates = {x["cc"]: float(x["rate"]) for x in data if x.get("cc")}
    date = dt.datetime.strptime(data[0]["exchangedate"], "%d.%m.%Y").date() if data else (day or dt.date.today())
    return date, rates


def fetch_privat_cash(http: httpx.Client) -> dict[str, tuple[float, float]]:
    resp = http.get(PRIVAT_URL, params={"exchange": "", "coursid": "5"})
    resp.raise_for_status()
    return {x["ccy"]: (float(x["buy"]), float(x["sale"])) for x in resp.json() if x.get("ccy")}


_FUEL_NAMES = {
    "А-95 преміум": "А-95+",
    "А-95": "А-95",
    "А-92": "А-92",
    "Дизельне": "ДП",
    "Газ": "Газ",
}


def parse_fuel_page(page: str) -> tuple[list[Fuel], dict[str, tuple[str, float]]]:
    """Середні ціни по області (перша таблиця) і найдешевші мережі (друга)."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(page, "lxml")
    tables = soup.find_all("table")
    fuels: list[Fuel] = []
    if tables:
        for row in tables[0].find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(cells) < 2:
                continue
            label = cells[0].replace("­", "").replace("Бензин ", "")
            name = next((v for k, v in _FUEL_NAMES.items() if label.startswith(k)), None)
            price = _num(cells[1])
            if name and price:
                change = _num(cells[2]) if len(cells) > 2 else None
                fuels.append(Fuel(name, price, change))
    order = [f for f, _ in FUELS]
    fuels.sort(key=lambda f: order.index(f.name))

    cheapest: dict[str, tuple[str, float]] = {}
    if len(tables) > 1:
        header = [re.sub(r"\s+", " ", c.get_text(" ", strip=True)) for c in tables[1].find_all("tr")[0].find_all(["td", "th"])]
        columns = {"А 95": "А-95", "ДП": "ДП"}
        for row in tables[1].find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if not cells or not cells[0]:
                continue
            # У рядку на одну клітинку більше, ніж у заголовку (порожня після назви мережі).
            values = cells[-(len(header) - 1):] if len(cells) >= len(header) else cells[1:]
            for col_name, fuel in columns.items():
                if col_name not in header:
                    continue
                price = _num(values[header.index(col_name) - 1]) if header.index(col_name) - 1 < len(values) else None
                if price and (fuel not in cheapest or price < cheapest[fuel][1]):
                    cheapest[fuel] = (cells[0], price)
    return fuels, cheapest


def fetch_markets(http: httpx.Client | None = None, today: dt.date | None = None) -> Markets:
    http = http or httpx.Client(timeout=20, follow_redirects=True, headers=_BROWSER)
    rate_date, rates = fetch_nbu(http)
    # Зміна — до курсу попереднього дня, за який НБУ встановлював курс.
    previous: dict[str, float] = {}
    for back in range(1, 5):
        try:
            prev_date, previous = fetch_nbu(http, rate_date - dt.timedelta(days=back))
        except Exception:
            previous = {}
        if previous and any(abs(previous.get(c, 0) - rates.get(c, 0)) > 1e-9 for c, _, _ in CURRENCIES):
            break
        if previous and back == 4:
            break
    try:
        cash = fetch_privat_cash(http)
    except Exception as exc:
        logger.warning("ПриватБанк недоступний: %s", exc)
        cash = {}
    result_rates = []
    for code, _, _ in CURRENCIES:
        if code not in rates:
            continue
        change = rates[code] - previous[code] if code in previous else None
        buy, sell = cash.get(code, (None, None))
        result_rates.append(Rate(code, rates[code], change, buy, sell))

    resp = http.get(FUEL_URL)
    resp.raise_for_status()
    fuels, cheapest = parse_fuel_page(resp.text)
    if not fuels:
        raise ValueError("на сторінці цін на пальне не знайдено таблиці")
    return Markets(rate_date=rate_date, rates=result_rates, fuels=fuels, cheapest=cheapest)


# --- тексти ---------------------------------------------------------------------


def fmt_money(v: float, digits: int = 2) -> str:
    return f"{v:.{digits}f}".replace(".", ",")


def fmt_change(v: float | None, digits: int = 2) -> str:
    if v is None or abs(v) < 0.005:
        return "без змін"
    sign = "+" if v > 0 else "−"
    return f"{sign}{fmt_money(abs(v), digits)}"


def caption(m: Markets) -> str:
    parts = []
    for r in m.rates[:2]:
        cash = f", готівка {fmt_money(r.buy)}/{fmt_money(r.sell)}" if r.buy and r.sell else ""
        parts.append(f"{r.code} {fmt_money(r.official)} грн{cash}")
    fuels = {f.name: f for f in m.fuels}
    fuel_parts = [f"{n} — {fmt_money(fuels[n].price)}" for n in ("А-95", "ДП", "Газ") if n in fuels]
    text = f"Курс НБУ на {m.rate_date.day} {MONTHS_GEN[m.rate_date.month - 1]}: " + "; ".join(parts) + "."
    if fuel_parts:
        text += " Середні ціни на пальне на Житомирщині: " + ", ".join(fuel_parts) + " грн/л."
    return text


def post_caption(m: Markets, ch) -> str:
    from telegram_net.signature import signature_html

    return f"<b>Курс валют і ціни на пальне</b>\n{html.escape(caption(m))}\n\n{signature_html(ch)}"


# --- малювання ------------------------------------------------------------------

W, H = 1080, 1350
TOP, BOTTOM = (34, 26, 74), (88, 42, 112)  # вечірній індиго → фіолет
WHITE = (255, 255, 255)
SOFT = (255, 255, 255, 205)
FAINT = (255, 255, 255, 130)
UP = (255, 120, 110)  # дорожчає — червонуватий
DOWN = (110, 220, 160)  # дешевшає — зелений
GOLD = (255, 205, 96)


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


def render(m: Markets, channel_title: str, day: dt.date) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    img = Image.new("RGBA", (W, H))
    g = ImageDraw.Draw(img)
    for y in range(H):
        g.line([(0, y), (W, y)], fill=_mix(TOP, BOTTOM, y / (H - 1)) + (255,))
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((620, -180, 1260, 420), fill=(255, 190, 120, 70))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(90)))

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)
    texts: list = []  # (xy, text, font, fill, anchor) — малюються після напівпрозорих панелей
    changes: list = []  # (правий край, зміна) — число зі стрілкою-трикутником

    def text(xy, s, font, fill, anchor="mm"):
        texts.append((xy, s, font, fill, anchor))

    text((W // 2, 88), "Курс валют і пальне", _font(70), WHITE)
    text((W // 2, 150), f"{day.day} {MONTHS_GEN[day.month - 1]} · Житомирщина", _font(34, "SemiBold"), SOFT)

    # --- валюти
    y0 = 200
    ld.rounded_rectangle((50, y0, W - 50, y0 + 470), radius=40, fill=(255, 255, 255, 26), outline=(255, 255, 255, 50), width=2)
    text((90, y0 + 46), "Курс валют", _font(38), WHITE, "lm")
    text((W - 90, y0 + 46), f"НБУ на {m.rate_date.day} {MONTHS_GEN[m.rate_date.month - 1]}", _font(26, "SemiBold"), FAINT, "rm")
    symbols = {code: sym for code, _, sym in CURRENCIES}
    names = {code: name for code, name, _ in CURRENCIES}
    row_y = y0 + 125
    for r in m.rates:
        # значок валюти
        ld.ellipse((90, row_y - 42, 174, row_y + 42), fill=GOLD + (255,))
        text((132, row_y), symbols.get(r.code, r.code), _font(40 if len(symbols.get(r.code, "")) == 1 else 30), (60, 34, 90))
        text((200, row_y - 16), names.get(r.code, r.code), _font(34, "Bold"), WHITE, "lm")
        cash = f"готівка {fmt_money(r.buy)} / {fmt_money(r.sell)}" if r.buy and r.sell else "офіційний курс"
        text((200, row_y + 24), cash, _font(24, "SemiBold"), FAINT, "lm")
        text((800, row_y - 4), fmt_money(r.official), _font(58), WHITE, "rm")
        changes.append(((W - 90, row_y - 4), r.change))
        row_y += 120

    # --- пальне
    y1 = 700
    ld.rounded_rectangle((50, y1, W - 50, y1 + 520), radius=40, fill=(255, 255, 255, 26), outline=(255, 255, 255, 50), width=2)
    text((90, y1 + 46), "Пальне", _font(38), WHITE, "lm")
    text((W - 90, y1 + 46), "середня ціна, грн/л", _font(26, "SemiBold"), FAINT, "rm")
    subtitles = dict(FUELS)
    row_y = y1 + 115
    for f in m.fuels:
        ld.rounded_rectangle((90, row_y - 30, 250, row_y + 30), radius=30, fill=(255, 255, 255, 40))
        text((170, row_y), f.name, _font(30), WHITE)
        if subtitles.get(f.name):
            text((280, row_y), subtitles[f.name], _font(28, "SemiBold"), FAINT, "lm")
        text((800, row_y), fmt_money(f.price), _font(50), WHITE, "rm")
        changes.append(((W - 90, row_y), f.change))
        row_y += 72
    cheap = [f"{fuel}: {net} {fmt_money(price)}" for fuel, (net, price) in m.cheapest.items()]
    if cheap:
        text((W // 2, y1 + 480), "Найдешевше · " + " · ".join(cheap), _font(26, "Bold"), GOLD)

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
        # трикутник: вгору — подорожчало, вниз — подешевшало (у шрифті немає ▲▼)
        if value > 0:
            d.polygon([(left - 18, y + 8), (left, y + 8), (left - 9, y - 9)], fill=color)
        else:
            d.polygon([(left - 18, y - 8), (left, y - 8), (left - 9, y + 9)], fill=color)
    d.text((W // 2, 1262), "Курс — НБУ, готівка — ПриватБанк · пальне — index.minfin.com.ua", font=_font(22, "SemiBold"), fill=FAINT, anchor="mm")
    from telegram_net.heart import draw_title_with_heart

    draw_title_with_heart(img, d, (W // 2, 1302), channel_title, _font(26, "SemiBold"), SOFT)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# --- публікація -----------------------------------------------------------------


def publish_markets(config, state, publisher, now: dt.datetime | None = None, force: bool = False, out_dir: Path | None = None, fetch=fetch_markets) -> list[str]:
    """Вечірня інфографіка в усі активні канали — раз на добу о settings.markets_hour
    за Києвом (GitHub запускає workflow двічі, бо не знає про літній час).
    publisher=None — лише зберегти картинки в out_dir."""
    from telegram_net.clock import local_now, start_of_local_day
    from telegram_net.signature import channel_title
    from telegram_net.state import iso

    now = now or dt.datetime.now(dt.timezone.utc)
    local = local_now(config, now)
    hour = getattr(config.settings, "markets_hour", 19)
    if not force and local.hour != hour:
        return [f"Зараз {local:%H:%M} за Києвом — курс і пальне виходять о {hour}:00, пропускаю"]
    day_start = start_of_local_day(config, now)
    channels = [ch for ch in config.active_channels if publisher is None or not state.posts_since(ch.channel_id, day_start, kind="markets")]
    if not channels:
        return ["Курс і пальне сьогодні вже були"]
    try:
        markets = fetch()
    except Exception as exc:
        logger.exception("Не вдалося отримати курс чи ціни на пальне")
        return [f"ПОМИЛКА даних — {exc}"]

    lines = []
    for ch in channels:
        image = render(markets, channel_title(ch), local.date())
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"markets_{ch.channel_id}.png").write_bytes(image)
        if publisher is None:
            lines.append(f"{ch.channel_id}: [пробний] {caption(markets)}")
            continue
        try:
            message_id = publisher.send_photo_bytes(ch.telegram_chat_id, image, post_caption(markets, ch), f"markets_{ch.channel_id}.png")
        except Exception as exc:
            logger.exception("Не вдалося надіслати курс і пальне в %s", ch.channel_id)
            lines.append(f"{ch.channel_id}: ПОМИЛКА Telegram — {exc}")
            continue
        state.log_post(
            {
                "channel_id": ch.channel_id,
                "published_at": iso(now),
                "kind": "markets",
                "text": caption(markets),
                "candidate_ids": [],
                "message_ids": [message_id],
            }
        )
        lines.append(f"{ch.channel_id}: надіслано — {caption(markets)}")
    return lines
