"""Ранкова інфографіка погоди для кожного каналу — без жодної моделі, лише код.

Дані: MET Norway (api.met.no, безкоштовно, без ключа), запасне джерело —
Open-Meteo. Картинку малює Pillow: шрифт Nunito (assets/fonts, OFL) і іконки погоди
Meteocons (assets/icons, MIT, PNG 512×512), тож у GitHub Actions нічого не
треба доставляти.

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
    wind_dir: float | None = None  # звідки дме, градуси (0 — з півночі)


@dataclass(frozen=True)
class Condition:
    icon: str
    night_icon: str
    phrase: str
    severity: int
    wet: bool = False


# Тип погоди → іконка, фраза, «вага» (для вибору головної погоди частини доби).
_SKY = {
    "clear": Condition("clear-day", "clear-night", "ясно", 0),
    "fair": Condition("partly-cloudy-day", "partly-cloudy-night", "переважно ясно", 1),
    "partly": Condition("overcast-day", "overcast-night", "мінлива хмарність", 2),
    "cloudy": Condition("cloudy", "cloudy", "хмарно", 3),
    "fog": Condition("fog", "fog", "туман", 4),
    "showers": Condition("partly-cloudy-day-rain", "partly-cloudy-night-rain", "короткочасний дощ", 5, wet=True),
    "rain": Condition("rain", "rain", "дощ", 6, wet=True),
    "sleet": Condition("sleet", "sleet", "мокрий сніг", 7, wet=True),
    "snow": Condition("snow", "snow", "сніг", 8, wet=True),
    "thunder": Condition("thunderstorms-rain", "thunderstorms-rain", "гроза", 9, wet=True),
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
                wind_dir=details.get("wind_from_direction"),
            )
        )
    return hours


def fetch_open_meteo(lat: float, lon: float, tz: ZoneInfo, http: httpx.Client) -> list[Hour]:
    resp = http.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat,
            "longitude": lon,
            "hourly": "temperature_2m,weather_code,wind_speed_10m,wind_direction_10m",
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
            wind_dir=wdir,
        )
        for t, temp, code, wind, wdir in zip(
            h["time"], h["temperature_2m"], h["weather_code"], h["wind_speed_10m"], h["wind_direction_10m"]
        )
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
    wind: str = ""  # «вітер південно-західний, 4 м/с»


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
    wind = describe_wind(daytime or hours[:12])

    if sky == "thunder":
        tip_icon, tip = "lightning-bolt", "Можлива гроза — бережіть себе"
    elif sky in ("snow", "sleet"):
        tip_icon, tip = "snowflake", "Сніжно — вдягайтеся тепліше"
    elif cond.wet:
        tip_icon, tip = "umbrella", "Візьміть парасольку"
    elif max_wind >= 10:
        tip_icon, tip = "wind", "Сильний вітер — тримайте капелюхи"
    elif day_max <= 0:
        tip_icon, tip = "thermometer-colder", "Морозно — вдягайтеся тепло"
    elif morning.temp <= 3:
        tip_icon, tip = "thermometer-colder", "Зранку холодно — вдягніться тепліше"
    elif sky in ("clear", "fair") and day_max >= 22:
        tip_icon, tip = "thermometer-warmer", "Сонячно — гарного дня!"
    else:
        tip_icon, tip = "sunrise", "Гарного дня!"

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
        wind=wind,
    )


_DIRECTIONS = ("північний", "північно-східний", "східний", "південно-східний", "південний", "південно-західний", "західний", "північно-західний")


def describe_wind(hours: list[Hour]) -> str:
    """«Вітер південно-західний, 4 м/с»: денна швидкість (округлене середнє, але не
    менше 1) і переважний напрямок. Штиль — без напрямку."""
    if not hours:
        return ""
    speed = max(1, round(sum(h.wind for h in hours) / len(hours)))
    if speed <= 1 and max(h.wind for h in hours) < 2:
        return "Майже без вітру"
    dirs = [h.wind_dir for h in hours if h.wind_dir is not None]
    if not dirs:
        return f"Вітер {speed} м/с"
    import math

    # середній напрямок — через вектори, щоб 350° і 10° дали північ, а не південь
    x = sum(math.cos(math.radians(d)) for d in dirs)
    y = sum(math.sin(math.radians(d)) for d in dirs)
    angle = math.degrees(math.atan2(y, x)) % 360
    return f"Вітер {_DIRECTIONS[round(angle / 45) % 8]}, {speed} м/с"


def fmt_temp(t: int) -> str:
    if t > 0:
        return f"+{t}°"
    if t < 0:
        return f"−{abs(t)}°"  # типографський мінус
    return "0°"


def caption(f: DayForecast) -> str:
    phrase = f.phrase[:1].upper() + f.phrase[1:]
    wind = f" {f.wind[:1].upper()}{f.wind[1:]}." if f.wind else ""
    return f"{phrase}, вдень до {fmt_temp(f.day_max)}, уночі {fmt_temp(f.night_min)}.{wind} {f.tip}"


# --- малювання ---------------------------------------------------------------

# Насичений градієнт неба під тип погоди (верх → низ): білий текст і білі хмари
# на ньому читаються краще, ніж на білій картці.
_BACKGROUNDS = {
    "clear-day": ((33, 118, 230), (96, 196, 245)),
    "partly-cloudy-day": ((44, 120, 214), (118, 186, 238)),
    "overcast-day": ((60, 112, 180), (132, 170, 214)),
    "cloudy": ((70, 96, 134), (140, 162, 192)),
    "overcast": ((70, 96, 134), (140, 162, 192)),
    "fog": ((96, 110, 132), (162, 176, 194)),
    "partly-cloudy-day-rain": ((50, 88, 150), (112, 148, 200)),
    "rain": ((46, 70, 120), (100, 128, 176)),
    "sleet": ((70, 104, 160), (150, 182, 222)),
    "snow": ((82, 124, 186), (168, 200, 238)),
    "thunderstorms-rain": ((36, 38, 72), (88, 92, 140)),
}
WHITE = (255, 255, 255)
INK = (31, 42, 68)
SOFT = (255, 255, 255, 215)  # напівпрозорий білий для другорядного тексту


def _font(size: int, weight: str = "ExtraBold"):
    from PIL import ImageFont

    font = ImageFont.truetype(str(ASSETS / "fonts" / "Nunito.ttf"), size)
    try:
        font.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return font


# Суцільний місяць візуально «важчий» за сонце з променями — трохи менший.
_ICON_SCALE = {"clear-night": 0.8}


def _icon(name: str, size: int):
    """Іконка Meteocons (assets/icons, 512×512, MIT), обрізана до вмісту й зменшена до `size`."""
    from PIL import Image

    im = Image.open(ASSETS / "icons" / f"{name}.png").convert("RGBA")
    im = im.crop(im.getbbox())
    scale = size * _ICON_SCALE.get(name, 1.0) / max(im.size)
    return im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.LANCZOS)


def _paste_center(base, im, cx: int, cy: int) -> None:
    base.alpha_composite(im, (round(cx - im.width / 2), round(cy - im.height / 2)))


def render(f: DayForecast, channel_title: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    top, bottom = _BACKGROUNDS.get(f.hero_icon, _BACKGROUNDS["cloudy"])
    img = Image.new("RGBA", (SIZE, SIZE))
    grad = ImageDraw.Draw(img)
    for y in range(SIZE):
        k = y / (SIZE - 1)
        grad.line([(0, y), (SIZE, y)], fill=tuple(round(a + (b - a) * k) for a, b in zip(top, bottom)) + (255,))

    # м'яке світіння за головною іконкою
    glow = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    ImageDraw.Draw(glow).ellipse((150, 230, 510, 590), fill=(255, 255, 255, 70))
    img.alpha_composite(glow.filter(ImageFilter.GaussianBlur(70)))

    overlay = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    cx = SIZE // 2

    title = f"Погода у {f.city_loc} сьогодні"
    size = 68
    while size > 44 and od.textlength(title, font=_font(size)) > SIZE - 140:
        size -= 2
    od.text((cx, 112), title, font=_font(size), fill=WHITE, anchor="mm")
    date_line = f"{WEEKDAYS[f.date.weekday()]}, {f.date.day} {MONTHS[f.date.month - 1]}"
    od.text((cx, 180), date_line, font=_font(38, "SemiBold"), fill=SOFT, anchor="mm")

    # головний блок
    od.text((650, 360), fmt_temp(f.day_max), font=_font(190), fill=WHITE, anchor="mm")
    od.text((650, 470), f.phrase, font=_font(46, "Bold"), fill=SOFT, anchor="mm")
    wind_icon = None
    if f.wind:
        wind_font = _font(32, "SemiBold")
        wind_icon = _icon("wind-white", 46)
        wind_w = wind_icon.width + 8 + od.textlength(f.wind, font=wind_font)
        wind_x = min(650 - wind_w / 2, SIZE - 70 - wind_w)
        od.text((wind_x + wind_icon.width + 8, 532), f.wind, font=wind_font, fill=SOFT, anchor="lm")

    # частини доби — «скляні» плитки
    n = len(f.parts)
    gap, left, right = 22, 70, SIZE - 70
    width = (right - left - gap * (n - 1)) / n
    y0, y1 = 590, 850
    for i in range(n):
        x0 = left + i * (width + gap)
        od.rounded_rectangle((x0, y0, x0 + width, y1), radius=40, fill=(255, 255, 255, 46), outline=(255, 255, 255, 70), width=2)
        part = f.parts[i]
        mid = x0 + width / 2
        od.text((mid, y0 + 44), part.name, font=_font(34, "Bold"), fill=SOFT, anchor="mm")
        od.text((mid, y0 + 214), fmt_temp(part.temp), font=_font(56), fill=WHITE, anchor="mm")

    # підказка
    tip_font = _font(38, "Bold")
    tip_w = od.textlength(f.tip, font=tip_font)
    tip_icon = _icon(f.tip_icon, 66)
    total = tip_icon.width + 16 + tip_w
    od.rounded_rectangle((cx - total / 2 - 36, 890, cx + total / 2 + 36, 972), radius=41, fill=(255, 255, 255, 235))
    od.text((cx - total / 2 + tip_icon.width + 16, 931), f.tip, font=tip_font, fill=INK, anchor="lm")

    img.alpha_composite(overlay)
    from telegram_net.heart import draw_title_with_heart

    draw_title_with_heart(img, ImageDraw.Draw(img), (cx, 1020), channel_title, _font(28, "SemiBold"), (255, 255, 255, 200))

    # іконки — поверх, щоб не пригасали під напівпрозорими шарами
    _paste_center(img, _icon(f.hero_icon, 300), 320, 395)
    if wind_icon is not None:
        _paste_center(img, wind_icon, round(wind_x + wind_icon.width / 2), 532)
    for i, part in enumerate(f.parts):
        mid = left + i * (width + gap) + width / 2
        _paste_center(img, _icon(part.icon, 112), round(mid), y0 + 128)
    _paste_center(img, tip_icon, round(cx - total / 2 + tip_icon.width / 2), 931)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# --- публікація ----------------------------------------------------------------


def post_caption(f: DayForecast, ch) -> str:
    import html

    from telegram_net.signature import signature_html

    return f"<b>Погода у {html.escape(f.city_loc)} сьогодні</b>\n{html.escape(caption(f))}\n\n{signature_html(ch)}"


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
            message_id = publisher.send_photo_bytes(ch.telegram_chat_id, image, post_caption(forecast, ch), f"weather_{ch.channel_id}.png")
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
    from telegram_net.signature import channel_title

    return channel_title(ch)
