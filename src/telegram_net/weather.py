"""Ранкова інфографіка погоди для кожного каналу — без жодної моделі, лише код.

Дані: MET Norway (api.met.no, безкоштовно, без ключа), запасне джерело —
Open-Meteo. Картинку малює Pillow: шрифт Nunito (assets/fonts, OFL) і кольорові
емодзі Noto (assets/emoji, OFL), тож у GitHub Actions нічого не треба доставляти.

На картинці лише головне: заголовок «Погода у Радомишлі сьогодні», велика
іконка з температурою вдень, чотири частини доби (ранок, день, вечір, ніч) з
іконкою й температурою і одна підказка («Візьміть парасольку»).
"""

from __future__ import annotations

import datetime as dt
import io
import logging
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "assets"
USER_AGENT = "telegram-net-weather/1.0"  # MET вимагає змістовний User-Agent
SIZE = 1080

MONTHS = ("січня", "лютого", "березня", "квітня", "травня", "червня", "липня", "серпня", "вересня", "жовтня", "листопада", "грудня")
WEEKDAYS = ("понеділок", "вівторок", "середа", "четвер", "пʼятниця", "субота", "неділя")

# Частини доби за київським часом: (назва, години). Ніч — з 22:00 до 05:00 наступного дня.
PERIODS = (
    ("Ранок", range(7, 11)),
    ("День", range(11, 17)),
    ("Вечір", range(17, 22)),
    ("Ніч", tuple(range(22, 24)) + tuple(range(0, 6))),
)


@dataclass(frozen=True)
class Hour:
    time: dt.datetime  # київський час
    temp: float
    sky: str  # нормалізований тип погоди, див. _SKY
    wind: float  # м/с


@dataclass(frozen=True)
class Condition:
    icon: str
    night_icon: str
    phrase: str
    severity: int
    wet: bool = False


# Тип погоди → іконка, фраза, «вага» (для вибору головної погоди частини доби).
_SKY = {
    "clear": Condition("sun", "moon", "ясно", 0),
    "fair": Condition("sun_small_cloud", "moon", "переважно ясно", 1),
    "partly": Condition("partly", "cloud", "мінлива хмарність", 2),
    "cloudy": Condition("cloud", "cloud", "хмарно", 3),
    "fog": Condition("fog", "fog", "туман", 4),
    "showers": Condition("sun_rain", "rain", "короткочасний дощ", 5, wet=True),
    "rain": Condition("rain", "rain", "дощ", 6, wet=True),
    "sleet": Condition("snow_cloud", "snow_cloud", "мокрий сніг", 7, wet=True),
    "snow": Condition("snow_cloud", "snow_cloud", "сніг", 8, wet=True),
    "thunder": Condition("thunder", "thunder", "гроза", 9, wet=True),
}


def met_sky(symbol_code: str) -> str:
    code = symbol_code.split("_")[0]
    if "thunder" in code:
        return "thunder"
    if "snow" in code:
        return "snow"
    if "sleet" in code:
        return "sleet"
    if "showers" in code:
        return "showers"
    if "rain" in code:
        return "rain"
    return {"clearsky": "clear", "fair": "fair", "partlycloudy": "partly", "cloudy": "cloudy", "fog": "fog"}.get(code, "cloudy")


def wmo_sky(code: int) -> str:
    """Коди погоди WMO (Open-Meteo)."""
    if code in (95, 96, 99):
        return "thunder"
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    if code in (56, 57, 66, 67):
        return "sleet"
    if code in (80, 81, 82):
        return "showers"
    if code in (51, 53, 55, 61, 63, 65):
        return "rain"
    if code in (45, 48):
        return "fog"
    return {0: "clear", 1: "fair", 2: "partly", 3: "cloudy"}.get(code, "cloudy")


def fetch_met(lat: float, lon: float, tz: ZoneInfo, http: httpx.Client) -> list[Hour]:
    resp = http.get(
        "https://api.met.no/weatherapi/locationforecast/2.0/compact",
        params={"lat": f"{lat:.2f}", "lon": f"{lon:.2f}"},
        headers={"User-Agent": USER_AGENT},
    )
    resp.raise_for_status()
    hours = []
    for ts in resp.json()["properties"]["timeseries"]:
        data = ts["data"]
        nxt = data.get("next_1_hours") or data.get("next_6_hours")
        if not nxt:
            continue
        details = data["instant"]["details"]
        hours.append(
            Hour(
                time=dt.datetime.fromisoformat(ts["time"].replace("Z", "+00:00")).astimezone(tz),
                temp=float(details["air_temperature"]),
                sky=met_sky(nxt["summary"]["symbol_code"]),
                wind=float(details.get("wind_speed", 0)),
            )
        )
    return hours


def fetch_open_meteo(lat: float, lon: float, tz: ZoneInfo, http: httpx.Client) -> list[Hour]:
    resp = http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,weather_code,wind_speed_10m",
            "wind_speed_unit": "ms",
            "timezone": "UTC",
            "forecast_days": 2,
        },
    )
    resp.raise_for_status()
    h = resp.json()["hourly"]
    return [
        Hour(
            time=dt.datetime.fromisoformat(t).replace(tzinfo=dt.timezone.utc).astimezone(tz),
            temp=float(temp),
            sky=wmo_sky(int(code)),
            wind=float(wind or 0),
        )
        for t, temp, code, wind in zip(h["time"], h["temperature_2m"], h["weather_code"], h["wind_speed_10m"])
    ]


def fetch_hours(lat: float, lon: float, tz: ZoneInfo, http: httpx.Client | None = None) -> list[Hour]:
    http = http or httpx.Client(timeout=20)
    try:
        return fetch_met(lat, lon, tz, http)
    except Exception as exc:
        logger.warning("MET Norway недоступний (%s) — беру Open-Meteo", exc)
        return fetch_open_meteo(lat, lon, tz, http)


@dataclass(frozen=True)
class Part:
    name: str
    icon: str
    temp: int


@dataclass(frozen=True)
class DayForecast:
    city_loc: str  # місто в місцевому відмінку: «Радомишлі»
    date: dt.date
    hero_icon: str
    phrase: str  # «ясно», «дощ»…
    day_max: int
    night_min: int
    parts: tuple[Part, ...]
    tip_icon: str
    tip: str


def _dominant(hours: list[Hour]) -> str:
    """Головна погода проміжку: опади, якщо їх щонайменше дві години, інакше — найчастіше небо."""
    wet = [h.sky for h in hours if _SKY[h.sky].wet]
    if len(wet) >= 2 or (wet and len(hours) <= 3):
        return max(wet, key=lambda s: (_SKY[s].severity, wet.count(s)))
    counts = Counter(h.sky for h in hours)
    return max(counts, key=lambda s: (counts[s], _SKY[s].severity))


def summarize(hours: list[Hour], day: dt.date, city_loc: str) -> DayForecast:
    def in_period(h: Hour, hours_of_day) -> bool:
        if h.time.hour not in hours_of_day:
            return False
        if h.time.hour < 6:  # ніч — це ранок наступного дня
            return h.time.date() == day + dt.timedelta(days=1)
        return h.time.date() == day

    parts = []
    for name, hours_of_day in PERIODS:
        chunk = [h for h in hours if in_period(h, hours_of_day)]
        if not chunk:
            continue
        sky = _dominant(chunk)
        cond = _SKY[sky]
        if name == "Ніч":
            temp = min(h.temp for h in chunk)
            icon = cond.night_icon
        elif name == "День":
            temp = max(h.temp for h in chunk)
            icon = cond.icon
        else:
            temp = sum(h.temp for h in chunk) / len(chunk)
            icon = cond.night_icon if name == "Вечір" else cond.icon  # увечері вже темно: місяць, а не сонце
        parts.append(Part(name, icon, round(temp)))
    if not parts:
        raise ValueError("немає прогнозу на цей день")

    daytime = [h for h in hours if h.time.date() == day and 8 <= h.time.hour <= 21]
    sky = _dominant(daytime or hours[:12])
    cond = _SKY[sky]
    day_max = round(max(h.temp for h in daytime)) if daytime else parts[0].temp
    night_part = next((p for p in parts if p.name == "Ніч"), parts[-1])
    morning = next((p for p in parts if p.name == "Ранок"), parts[0])
    max_wind = max((h.wind for h in daytime), default=0)

    if sky == "thunder":
        tip_icon, tip = "thunder", "Можлива гроза — бережіть себе"
    elif sky in ("snow", "sleet"):
        tip_icon, tip = "gloves", "Сніжно — вдягайтеся тепліше"
    elif cond.wet:
        tip_icon, tip = "umbrella", "Візьміть парасольку"
    elif max_wind >= 10:
        tip_icon, tip = "wind", "Сильний вітер — тримайте капелюхи"
    elif day_max <= 0:
        tip_icon, tip = "scarf", "Морозно — вдягайтеся тепло"
    elif morning.temp <= 3:
        tip_icon, tip = "scarf", "Зранку холодно — вдягніться тепліше"
    elif sky in ("clear", "fair") and day_max >= 22:
        tip_icon, tip = "sunglasses", "Сонячно — гарного дня!"
    else:
        tip_icon, tip = "heart", "Гарного дня!"

    return DayForecast(
        city_loc=city_loc,
        date=day,
        hero_icon=cond.icon,
        phrase=cond.phrase,
        day_max=day_max,
        night_min=night_part.temp,
        parts=tuple(parts),
        tip_icon=tip_icon,
        tip=tip,
    )


def fmt_temp(t: int) -> str:
    if t > 0:
        return f"+{t}°"
    if t < 0:
        return f"−{abs(t)}°"  # типографський мінус
    return "0°"


def caption(f: DayForecast) -> str:
    phrase = f.phrase[:1].upper() + f.phrase[1:]
    return f"{phrase}, вдень до {fmt_temp(f.day_max)}, уночі {fmt_temp(f.night_min)}. {f.tip}"


# --- малювання ---------------------------------------------------------------

_BACKGROUNDS = {
    "sun": ((126, 200, 250), (225, 244, 255)),
    "sun_small_cloud": ((126, 200, 250), (225, 244, 255)),
    "partly": ((150, 190, 230), (230, 240, 250)),
    "cloud": ((170, 186, 205), (236, 241, 246)),
    "fog": ((185, 192, 200), (240, 242, 245)),
    "sun_rain": ((120, 160, 210), (220, 232, 246)),
    "rain": ((110, 140, 190), (214, 226, 242)),
    "thunder": ((95, 105, 150), (205, 210, 232)),
    "snow_cloud": ((180, 210, 240), (246, 250, 255)),
}
INK = (31, 42, 68)
MUTED = (107, 122, 144)
CARD = (255, 255, 255)
TILE = (243, 247, 252)


def _font(size: int, weight: str = "ExtraBold"):
    from PIL import ImageFont

    font = ImageFont.truetype(str(ASSETS / "fonts" / "Nunito.ttf"), size)
    try:
        font.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return font


def _emoji(name: str, size: int):
    from PIL import Image

    im = Image.open(ASSETS / "emoji" / f"{name}.png").convert("RGBA")
    scale = size / max(im.size)
    return im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)


def _paste_center(base, im, cx: int, cy: int) -> None:
    base.alpha_composite(im, (round(cx - im.width / 2), round(cy - im.height / 2)))


def render(f: DayForecast, channel_title: str) -> bytes:
    from PIL import Image, ImageDraw

    top, bottom = _BACKGROUNDS.get(f.hero_icon, _BACKGROUNDS["cloud"])
    img = Image.new("RGBA", (SIZE, SIZE))
    grad = ImageDraw.Draw(img)
    for y in range(SIZE):
        k = y / (SIZE - 1)
        grad.line([(0, y), (SIZE, y)], fill=tuple(round(a + (b - a) * k) for a, b in zip(top, bottom)) + (255,))

    # тінь картки + картка
    shadow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rounded_rectangle((60, 72, SIZE - 60, SIZE - 48), radius=56, fill=(20, 40, 80, 40))
    img.alpha_composite(shadow)
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((60, 60, SIZE - 60, SIZE - 60), radius=56, fill=CARD)

    cx = SIZE // 2
    title = f"Погода у {f.city_loc} сьогодні"
    size = 70
    while size > 44 and d.textlength(title, font=_font(size)) > SIZE - 200:
        size -= 2
    d.text((cx, 150), title, font=_font(size), fill=INK, anchor="mm")
    date_line = f"{WEEKDAYS[f.date.weekday()]}, {f.date.day} {MONTHS[f.date.month - 1]}"
    d.text((cx, 222), date_line, font=_font(38, "SemiBold"), fill=MUTED, anchor="mm")

    # головний блок: іконка + температура вдень + фраза
    _paste_center(img, _emoji(f.hero_icon, 230), 330, 415)
    d.text((640, 392), fmt_temp(f.day_max), font=_font(170), fill=INK, anchor="mm")
    d.text((640, 500), f.phrase, font=_font(44, "Bold"), fill=MUTED, anchor="mm")

    # чотири частини доби
    n = len(f.parts)
    gap, left, right = 22, 100, SIZE - 100
    width = (right - left - gap * (n - 1)) / n
    y0, y1 = 585, 835
    for i, part in enumerate(f.parts):
        x0 = left + i * (width + gap)
        d.rounded_rectangle((x0, y0, x0 + width, y1), radius=36, fill=TILE)
        mid = x0 + width / 2
        d.text((mid, y0 + 42), part.name, font=_font(34, "Bold"), fill=MUTED, anchor="mm")
        _paste_center(img, _emoji(part.icon, 92), round(mid), y0 + 125)
        d.text((mid, y0 + 208), fmt_temp(part.temp), font=_font(52), fill=INK, anchor="mm")

    # підказка
    tip_font = _font(38, "Bold")
    tip_w = d.textlength(f.tip, font=tip_font)
    icon = _emoji(f.tip_icon, 56)
    total = icon.width + 18 + tip_w
    px0 = cx - total / 2 - 34
    d.rounded_rectangle((px0, 870, cx + total / 2 + 34, 950), radius=40, fill=(255, 244, 214))
    _paste_center(img, icon, round(cx - total / 2 + icon.width / 2), 910)
    d.text((cx - total / 2 + icon.width + 18, 910), f.tip, font=tip_font, fill=INK, anchor="lm")

    d.text((cx, 990), channel_title, font=_font(28, "SemiBold"), fill=(160, 172, 190), anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# --- публікація ----------------------------------------------------------------


def post_caption(f: DayForecast) -> str:
    import html

    return f"<b>Погода у {html.escape(f.city_loc)} сьогодні</b>\n{html.escape(caption(f))}"


def publish_weather(config, state, publisher, now: dt.datetime | None = None, force: bool = False, out_dir: Path | None = None, fetch=fetch_hours) -> list[str]:
    """Інфографіка погоди в кожен активний канал із налаштованими координатами.

    Один раз на добу і лише о settings.weather_hour за Києвом (GitHub запускає
    workflow двічі — о 05:00 і 06:00 UTC, бо не знає про перехід на літній час;
    зайвий запуск тут просто нічого не робить). force — ігнорувати годину.
    publisher=None — нічого не надсилати, лише зберегти картинки в out_dir."""
    from telegram_net.clock import local_now, start_of_local_day
    from telegram_net.state import iso

    now = now or dt.datetime.now(dt.timezone.utc)
    local = local_now(config, now)
    if not force and local.hour != config.settings.weather_hour:
        return [f"Зараз {local:%H:%M} за Києвом — погода виходить о {config.settings.weather_hour}:00, пропускаю"]
    tz = ZoneInfo(config.settings.timezone)
    day_start = start_of_local_day(config, now)
    lines = []
    cache: dict[tuple[float, float], list[Hour]] = {}
    for ch in config.active_channels:
        if not ch.weather:
            continue
        if publisher is not None and state.posts_since(ch.channel_id, day_start, kind="weather"):
            lines.append(f"{ch.channel_id}: погода сьогодні вже була")
            continue
        try:
            key = (ch.weather["lat"], ch.weather["lon"])
            if key not in cache:
                cache[key] = fetch(key[0], key[1], tz)
            forecast = summarize(cache[key], local.date(), ch.weather["city_loc"])
            image = render(forecast, _channel_title(ch))
        except Exception as exc:
            logger.exception("Погода для %s не вдалася", ch.channel_id)
            lines.append(f"{ch.channel_id}: ПОМИЛКА — {exc}")
            continue
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"weather_{ch.channel_id}.png").write_bytes(image)
        if publisher is None:
            lines.append(f"{ch.channel_id}: [пробний] {caption(forecast)}")
            continue
        try:
            message_id = publisher.send_photo_bytes(ch.telegram_chat_id, image, post_caption(forecast), f"weather_{ch.channel_id}.png")
        except Exception as exc:
            logger.exception("Не вдалося надіслати погоду в %s", ch.channel_id)
            lines.append(f"{ch.channel_id}: ПОМИЛКА Telegram — {exc}")
            continue
        state.log_post(
            {
                "channel_id": ch.channel_id,
                "published_at": iso(now),
                "kind": "weather",
                "text": f"Погода у {forecast.city_loc} сьогодні. {caption(forecast)}",
                "candidate_ids": [],
                "message_ids": [message_id],
            }
        )
        lines.append(f"{ch.channel_id}: надіслано — {caption(forecast)}")
    return lines


def _channel_title(ch) -> str:
    """«Мій Радомишль» для підпису внизу картинки: з заклику підписатись, інакше з назви."""
    cta = ch.subscribe_cta or ""
    if "«" in cta and "»" in cta:
        return cta[cta.index("«") + 1 : cta.index("»")]
    return f"Мій {ch.name}"
