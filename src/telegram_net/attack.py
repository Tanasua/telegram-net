"""Ранкова інфографіка «Нічна атака» — без жодної моделі, лише код.

Джерело — офіційний канал Повітряних сил ЗСУ (@kpszsu), читаний через t.me/s:

- ранкове зведення («У ніч на 15 листопада противник атакував 132 ударними
  БпЛА… збито/подавлено 106… влучання на 20 локаціях») — головні цифри й
  напрямки пусків;
- нічна стрічка («Реактивний БпЛА з Київщини на Житомирщину (Коростень)») —
  скільки разів кожна область фігурувала в повідомленнях про шахеди й ракети.

Картинка: карта України з областями, підфарбованими за кількістю згадок,
стрілки від місць пусків, цифри зведення і рядок про Житомирщину.
Межі областей — OpenStreetMap (assets/ukraine_oblasts.json, ODbL).

Це підсумок уже відомого з офіційного джерела, а не онлайн-моніторинг: ні
роботи ППО в реальному часі, ні місць влучань тут немає.
"""

from __future__ import annotations

import datetime as dt
import io
import json
import logging
import math
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from telegram_net.fetchers.base import FetchedItem
from telegram_net.fetchers.tme_fetcher import parse_channel_page

logger = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "assets"
SOURCE = "kpszsu"
MAX_PAGES = 40
ZHYTOMYR = "UA-18"

MONTHS_GEN = {
    "січня": 1, "лютого": 2, "березня": 3, "квітня": 4, "травня": 5, "червня": 6,
    "липня": 7, "серпня": 8, "вересня": 9, "жовтня": 10, "листопада": 11, "грудня": 12,
}

# Як області й міста звуться в повідомленнях Повітряних сил → код області (ISO 3166-2).
_PLACES = {
    "UA-05": r"вінниччин|вінниц",
    "UA-07": r"волин|луцьк|ковел",
    "UA-09": r"луганщин|луганськ",
    "UA-12": r"дніпропетровщин|дніпр[оау]\b|дніпрі\b|кривий ріг|кривого рогу|павлоград|нікопол|кам.янськ",
    "UA-14": r"донеччин|донецьк|краматорськ|слов.янськ|покровськ|костянтинівк",
    "UA-18": r"житомирщин|житомир|коростен|бердич|звягел|малин|овруч|олевськ|народич|радомишл|коростиш",
    "UA-21": r"закарпатт|ужгород|мукачев",
    "UA-23": r"запоріжж|запорізьк",
    "UA-26": r"франківщин|івано-франківськ|прикарпатт",
    "UA-32": r"київщин|київ|біла церква|білої церкви|бровар|бориспіл|фастів|вишгород|обухів",
    "UA-35": r"кіровоградщин|кропивницьк|олександрі[яї]",
    "UA-43": r"крим",
    "UA-46": r"львівщин|львів",
    "UA-48": r"миколаївщин|миколаїв|очаків|вознесенськ",
    "UA-51": r"одещин|одес|ізмаїл|ананьїв|подільськ|чорноморськ|південне\b",
    "UA-53": r"полтавщин|полтав|кременчук|миргород|лубни",
    "UA-56": r"рівненщин|рівне\b|рівного\b",
    "UA-59": r"сумщин|суми\b|сум\b|сумах|конотоп|шостк|охтирк|ромни",
    "UA-61": r"тернопільщин|тернопіл",
    "UA-63": r"харківщин|харків|ізюм|куп.янськ|чугуїв|лозов",
    "UA-65": r"херсонщин|херсон",
    "UA-68": r"хмельниччин|хмельницьк|старокостянтинів|шепетівк|кам.янець",
    "UA-71": r"черкащин|черкас|умань|жашків|сміл",
    "UA-74": r"чернігівщин|чернігів|ніжин|прилук|славутич",
    "UA-77": r"буковин|чернівеччин|чернівц",
}
_PLACE_RE = {iso: re.compile(pattern, re.IGNORECASE) for iso, pattern in _PLACES.items()}

_DRONE_RE = re.compile(r"бпла|шахед|безпілотник|дрон", re.IGNORECASE)
_MISSILE_RE = re.compile(r"ракет|балістик|крилат|калібр|кинджал|х-\d+|іскандер", re.IGNORECASE)
_KAB_RE = re.compile(r"\bкаб", re.IGNORECASE)

# Відомі місця пусків (широта, довгота) — для стрілок «звідки».
LAUNCH_SITES = {
    "Брянськ": (53.25, 34.37),
    "Орел": (52.97, 36.07),
    "Курськ": (51.73, 36.19),
    "Бєлгород": (50.60, 36.59),
    "Воронеж": (51.67, 39.20),
    "Міллерово": (48.92, 40.40),
    "Приморсько-Ахтарськ": (46.05, 38.17),
    "Єйськ": (46.71, 38.27),
    "Таганрог": (47.21, 38.94),
    "Ростов": (47.23, 39.72),
    "Донецьк": (48.00, 37.80),
    "Крим": (45.30, 34.10),
    "Чорне море": (44.40, 32.20),
    "Азовське море": (46.10, 36.60),
    "Навля": (52.83, 34.50),
    "Шаталово": (54.30, 32.40),
}
_SITE_ALIASES = {
    "Гвардійське": "Крим", "Чауда": "Крим", "Джанкой": "Крим", "Саки": "Крим", "Кримськ": "Приморсько-Ахтарськ",
    "АР Крим": "Крим", "Брянська": "Брянськ", "Курська": "Курськ", "Орловська": "Орел", "Бєлгородська": "Бєлгород",
}


@dataclass
class Summary:
    night_of: str  # «15 листопада»
    date: dt.date
    drones: int | None = None
    jet_drones: int | None = None
    missiles: int | None = None
    downed: int | None = None
    hits: int | None = None
    directions: list[str] = field(default_factory=list)
    url: str = ""


@dataclass
class NightReport:
    summary: Summary
    oblast_counts: Counter  # ISO → кількість повідомлень про шахеди/ракети
    zhytomyr_places: list[str]
    zhytomyr_count: int
    messages: int


def fetch_feed(since: dt.datetime, http: httpx.Client | None = None, handle: str = SOURCE) -> list[FetchedItem]:
    """Повідомлення каналу від `since` до зараз — гортаючи t.me/s назад сторінками."""
    http = http or httpx.Client(timeout=20)
    collected: dict[int, FetchedItem] = {}
    before = None
    for _ in range(MAX_PAGES):
        resp = http.get(f"https://t.me/s/{handle}", params={"before": before} if before else None)
        resp.raise_for_status()
        page = parse_channel_page(resp.text)
        if not page:
            break
        for msg_id, item in page:
            collected[msg_id] = item
        before = page[0][0]
        oldest = page[0][1].published_at
        if oldest is not None and oldest < since:
            break
    return [item for _, item in sorted(collected.items()) if item.published_at and item.published_at >= since]


def _int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.IGNORECASE | re.DOTALL)
    return int(m.group(1)) if m else None


def parse_summary(item: FetchedItem, tz: ZoneInfo) -> Summary | None:
    text = item.text
    m = re.search(r"у ніч на (\d{1,2})\s+([а-яіїєґ']+)", text, re.IGNORECASE)
    if not m or m.group(2).lower() not in MONTHS_GEN:
        return None
    published = (item.published_at or dt.datetime.now(dt.timezone.utc)).astimezone(tz)
    day, month = int(m.group(1)), MONTHS_GEN[m.group(2).lower()]
    year = published.year - (1 if month == 12 and published.month == 1 else 0)
    try:
        date = dt.date(year, month, day)
    except ValueError:
        return None

    # Речення про атаку — від «атакував» до наступного абзацу: там і ракети, і БпЛА.
    attack = re.search(r"атакува\w*(.+?)(?:\n\s*\n|повітряний напад|$)", text, re.IGNORECASE | re.DOTALL)
    attack_text = attack.group(1) if attack else text
    missiles = [int(n) for n in re.findall(r"(\d+)\s+(?:[^\s\d,.;]+\s+){0,3}?ракет", attack_text, re.IGNORECASE)]

    directions: list[str] = []
    d = re.search(r"напрямк\w*:\s*(.+?)(?:\.\s|\.$|\n)", text, re.IGNORECASE | re.DOTALL)
    if d:
        for raw in re.split(r",|;", d.group(1)):
            name = re.sub(r"\s*[-–—]\s*рф\b|\bТОТ\b|\bобл\.?|\bобласть\b", " ", raw, flags=re.IGNORECASE)
            parts = [p.strip(" -–—.") for p in re.split(r"\s+[–—-]\s+", name) if p.strip(" -–—.")]
            for part in parts or [name.strip()]:
                part = re.sub(r"\s+", " ", part).strip()
                if part and part not in directions:
                    directions.append(part)

    return Summary(
        night_of=f"{day} {m.group(2).lower()}",
        date=date,
        drones=_int(r"(\d+)\s+ударн\w*\s+(?:БпЛА|безпілотник)", attack_text),
        jet_drones=_int(r"\((\d+)\s+із\s+них\s*[-–—]?\s*реактивн", attack_text),
        missiles=sum(missiles) if missiles else None,
        downed=_int(r"збито/подавлено\s+(\d+)", text),
        hits=_int(r"влучання[^.]*?на\s+(\d+)\s+локаці", text),
        directions=directions,
        url=item.url,
    )


def find_summary(items: list[FetchedItem], tz: ZoneInfo, day: dt.date) -> Summary | None:
    """Зведення за ніч на `day` — найсвіжіше, якщо їх кілька (уточнення)."""
    for item in reversed(items):
        if len(item.text) < 150:
            continue
        summary = parse_summary(item, tz)
        if summary and summary.date == day:
            return summary
    return None


def oblasts_in(text: str) -> set[str]:
    return {iso for iso, regex in _PLACE_RE.items() if regex.search(text)}


def is_threat(text: str) -> bool:
    """Повідомлення про шахеди чи ракети (КАБи біля фронту не рахуємо)."""
    return bool(_DRONE_RE.search(text) or _MISSILE_RE.search(text)) and not (_KAB_RE.search(text) and not _DRONE_RE.search(text))


_PLACE_NAME_RE = re.compile(r"(?:\(([^)]{3,40})\)|(?:курсом на|повз|в напрямку|у напрямку)\s+([А-ЯІЇЄҐ][а-яіїєґ'’\-]+(?:\s[А-ЯІЇЄҐ][а-яіїєґ'’\-]+)?))")


def zhytomyr_places(items: list[FetchedItem]) -> list[str]:
    """Найчастіші населені пункти Житомирщини в повідомленнях за ніч."""
    names: Counter = Counter()
    for item in items:
        if ZHYTOMYR not in oblasts_in(item.text):
            continue
        for m in _PLACE_NAME_RE.finditer(item.text):
            raw = (m.group(1) or m.group(2) or "").strip()
            for name in re.split(r"[/,]", raw):
                name = name.strip()
                if not name or name.lower().startswith(("житомирщин", "київщин", "місто")):
                    continue
                if re.search(_PLACES[ZHYTOMYR], name, re.IGNORECASE):
                    names[_nominative(name)] += 1
        if re.match(r"\W*житомир\s*:", item.text, re.IGNORECASE):
            names["Житомир"] += 1
    return [n for n, _ in names.most_common(4)]


def _nominative(name: str) -> str:
    """«Коростеня» → «Коростень», «Житомира» → «Житомир» — для коротких підписів."""
    fixes = {"Коростеня": "Коростень", "Житомира": "Житомир", "Бердичева": "Бердичів", "Малина": "Малин", "Звягеля": "Звягель", "Овруча": "Овруч", "Радомишля": "Радомишль", "Коростишева": "Коростишів"}
    return fixes.get(name, name)


def build_report(items: list[FetchedItem], tz: ZoneInfo, day: dt.date) -> NightReport | None:
    summary = find_summary(items, tz, day)
    if summary is None:
        return None
    night_start = dt.datetime.combine(day - dt.timedelta(days=1), dt.time(18, 0), tz)
    night_end = dt.datetime.combine(day, dt.time(9, 0), tz)
    night = [
        i
        for i in items
        if i.published_at
        and night_start <= i.published_at <= night_end
        and is_threat(i.text)
        and not re.search(r"у ніч на \d", i.text, re.IGNORECASE)  # саме зведення — не повідомлення про ціль
    ]
    counts: Counter = Counter()
    for item in night:
        for iso in oblasts_in(item.text):
            counts[iso] += 1
    return NightReport(
        summary=summary,
        oblast_counts=counts,
        zhytomyr_places=zhytomyr_places(night),
        zhytomyr_count=counts.get(ZHYTOMYR, 0),
        messages=len(night),
    )


def site_for(direction: str) -> str | None:
    for alias, site in _SITE_ALIASES.items():
        if alias.lower() in direction.lower():
            return site
    for site in LAUNCH_SITES:
        if site.lower()[:5] in direction.lower():
            return site
    return None


# --- тексти --------------------------------------------------------------------


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def caption(r: NightReport) -> str:
    s = r.summary
    parts = []
    if s.drones:
        jets = f" (з них {s.jet_drones} реактивних)" if s.jet_drones else ""
        parts.append(f"{s.drones} {_plural(s.drones, 'ударний БпЛА', 'ударні БпЛА', 'ударних БпЛА')}{jets}")
    if s.missiles:
        parts.append(f"{s.missiles} {_plural(s.missiles, 'ракета', 'ракети', 'ракет')}")
    lines = [f"У ніч на {s.night_of} росія атакувала Україну: " + " і ".join(parts) + "." if parts else f"Нічна атака у ніч на {s.night_of}."]
    if s.downed:
        lines.append(f"Збито/подавлено {s.downed}.")
    if s.hits:
        lines.append(f"Влучання — на {s.hits} {_plural(s.hits, 'локації', 'локаціях', 'локаціях')}.")
    if r.zhytomyr_count:
        where = f": {', '.join(r.zhytomyr_places)}" if r.zhytomyr_places else ""
        lines.append(f"Житомирщина фігурувала в {r.zhytomyr_count} {_plural(r.zhytomyr_count, 'повідомленні', 'повідомленнях', 'повідомленнях')} про цілі{where}.")
    return " ".join(lines)


def post_caption(r: NightReport) -> str:
    import html

    return f"<b>Нічна атака: підсумок</b>\n{html.escape(caption(r))}\n\nДжерело: Повітряні сили ЗСУ"


# --- малювання -----------------------------------------------------------------

W, H = 1080, 1350
BG_TOP, BG_BOTTOM = (10, 18, 38), (22, 34, 64)
WHITE = (255, 255, 255)
SOFT = (255, 255, 255, 200)
FAINT = (255, 255, 255, 120)
ACCENT = (255, 96, 64)
ZT_OUTLINE = (255, 210, 74)
NONE_FILL = (30, 44, 76)
BORDER = (62, 82, 122)

# Видима область карти: Україна + місця пусків на сході й півночі.
LON0, LON1, LAT0, LAT1 = 21.8, 41.0, 44.0, 53.6
MAP_BOX = (40, 360, W - 40, 1150)


def _font(size: int, weight: str = "ExtraBold"):
    from PIL import ImageFont

    font = ImageFont.truetype(str(ASSETS / "fonts" / "Nunito.ttf"), size)
    try:
        font.set_variation_by_name(weight)
    except (OSError, ValueError):
        pass
    return font


def _projector():
    x0, y0, x1, y1 = MAP_BOX
    k = math.cos(math.radians((LAT0 + LAT1) / 2))
    span_x, span_y = (LON1 - LON0) * k, LAT1 - LAT0
    scale = min((x1 - x0) / span_x, (y1 - y0) / span_y)
    ox = x0 + ((x1 - x0) - span_x * scale) / 2
    oy = y0 + ((y1 - y0) - span_y * scale) / 2

    def project(lon: float, lat: float) -> tuple[float, float]:
        return ox + (lon - LON0) * k * scale, oy + (LAT1 - lat) * scale

    return project


def _mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def _heat(t: float):
    """0 → темно-синій, 1 → помаранчево-червоний, через фіолетовий."""
    if t <= 0:
        return NONE_FILL
    mid = (128, 52, 98)
    low = (58, 52, 104)
    return _mix(low, mid, t * 2) if t < 0.5 else _mix(mid, ACCENT, (t - 0.5) * 2)


def _centroid(ring):
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-9:
        return ring[0]
    return cx / (3 * a), cy / (3 * a)


def _arrow(draw, start, end, color, width=5, head=22):
    draw.line([start, end], fill=color, width=width)
    ang = math.atan2(end[1] - start[1], end[0] - start[0])
    left = (end[0] - head * math.cos(ang - 0.45), end[1] - head * math.sin(ang - 0.45))
    right = (end[0] - head * math.cos(ang + 0.45), end[1] - head * math.sin(ang + 0.45))
    draw.polygon([end, left, right], fill=color)


def render(r: NightReport, channel_title: str) -> bytes:
    from PIL import Image, ImageDraw, ImageFilter

    oblasts = json.loads((ASSETS / "ukraine_oblasts.json").read_text(encoding="utf-8"))
    project = _projector()
    s = r.summary

    img = Image.new("RGBA", (W, H))
    g = ImageDraw.Draw(img)
    for y in range(H):
        g.line([(0, y), (W, y)], fill=_mix(BG_TOP, BG_BOTTOM, y / (H - 1)) + (255,))

    # --- карта
    max_count = max(r.oblast_counts.values(), default=0)
    shapes = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shapes)
    for iso, data in oblasts.items():
        t = (r.oblast_counts.get(iso, 0) / max_count) if max_count else 0
        t = math.sqrt(t)  # щоб і кілька згадок було видно
        for ring in data["rings"]:
            pts = [project(lon, lat) for lon, lat in ring]
            sd.polygon(pts, fill=_heat(t) + (255,), outline=BORDER + (255,), width=2)
    glow = shapes.filter(ImageFilter.GaussianBlur(18))
    img.alpha_composite(Image.eval(glow, lambda v: v // 3))
    img.alpha_composite(shapes)

    d = ImageDraw.Draw(img)
    # Житомирщина — жовтий контур
    for ring in oblasts[ZHYTOMYR]["rings"]:
        pts = [project(lon, lat) for lon, lat in ring]
        d.line(pts + pts[:1], fill=ZT_OUTLINE + (255,), width=5, joint="curve")

    # стрілки від місць пусків до центру України
    target = project(31.5, 49.0)
    site_font = _font(24, "Bold")
    drawn = set()
    site_labels: list = []
    site_dots: list = []
    for direction in s.directions:
        site = site_for(direction)
        if site is None or site in drawn:
            continue
        drawn.add(site)
        lat, lon = LAUNCH_SITES[site]
        sx, sy = project(lon, lat)
        dist = math.hypot(target[0] - sx, target[1] - sy)
        length = min(130, dist * 0.45)
        ex = sx + (target[0] - sx) / dist * length
        ey = sy + (target[1] - sy) / dist * length
        _arrow(d, (sx, sy), (ex, ey), ACCENT + (230,), width=5)
        d.ellipse((sx - 9, sy - 9, sx + 9, sy + 9), fill=ACCENT + (255,), outline=WHITE + (255,), width=2)
        # підпис над точкою (під нею — для південних), не виходячи за край картинки
        half = d.textlength(site, font=site_font) / 2
        lx = min(max(sx, 24 + half), W - 24 - half)
        below = sy > target[1] + 60
        site_labels.append(((lx, sy + 18 if below else sy - 16), site, "mt" if below else "mb"))
        site_dots.append((sx, sy))

    # напівпрозорі елементи — окремим шаром, інакше альфа не змішується з тлом
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ld = ImageDraw.Draw(layer)

    # кількість згадок у центрі області
    label_font = _font(26)
    for iso, count in r.oblast_counts.items():
        if iso not in oblasts or not count:
            continue
        ring = max(oblasts[iso]["rings"], key=len)
        cx, cy = project(*_centroid(ring))
        if any(math.hypot(cx - x, cy - y) < 34 for x, y in site_dots):
            cy -= 38  # точка пуску всередині області (Донецьк, Крим) — цифру трохи вище
        d.text((cx, cy), str(count), font=label_font, fill=WHITE, anchor="mm", stroke_width=3, stroke_fill=(10, 18, 38))

    for xy, site, anchor in site_labels:
        d.text(xy, site, font=site_font, fill=SOFT, anchor=anchor, stroke_width=3, stroke_fill=(10, 18, 38))

    # --- заголовок і цифри
    d.text((W // 2, 92), "Нічна атака", font=_font(76), fill=WHITE, anchor="mm")
    d.text((W // 2, 158), f"у ніч на {s.night_of} · дані Повітряних сил ЗСУ", font=_font(34, "SemiBold"), fill=SOFT, anchor="mm")

    cards = []
    if s.drones is not None:
        cards.append((str(s.drones), _plural(s.drones, "шахед", "шахеди", "шахедів"), f"з них {s.jet_drones} реактивних" if s.jet_drones else ""))
    if s.missiles:
        cards.append((str(s.missiles), _plural(s.missiles, "ракета", "ракети", "ракет"), ""))
    if s.downed is not None:
        cards.append((str(s.downed), "збито", "подавлено"))
    if s.hits is not None:
        cards.append((str(s.hits), _plural(s.hits, "локація", "локації", "локацій"), "з влучаннями"))
    cards = cards[:4]
    texts: list = []
    if cards:
        gap, left, right = 20, 60, W - 60
        cw = (right - left - gap * (len(cards) - 1)) / len(cards)
        for i, (num, label, sub) in enumerate(cards):
            x0 = left + i * (cw + gap)
            ld.rounded_rectangle((x0, 205, x0 + cw, 340), radius=30, fill=(255, 255, 255, 22), outline=(255, 255, 255, 50), width=2)
            mid = x0 + cw / 2
            texts.append(((mid, 256), num, _font(64), ACCENT if label == "збито" else WHITE))
            texts.append(((mid, 303), label, _font(28, "Bold"), SOFT))
            if sub:
                texts.append(((mid, 329), sub, _font(20, "SemiBold"), FAINT))

    # --- підвал
    if r.zhytomyr_count:
        where = " · " + ", ".join(r.zhytomyr_places) if r.zhytomyr_places else ""
        zt = f"Житомирщина: {r.zhytomyr_count} {_plural(r.zhytomyr_count, 'повідомлення', 'повідомлення', 'повідомлень')} про цілі{where}"
    else:
        zt = "Житомирщина: цілей у повідомленнях за ніч не було"
    size = 34
    while size > 22 and d.textlength(zt, font=_font(size, "Bold")) > W - 190:
        size -= 1
    ld.rounded_rectangle((60, 1172, W - 60, 1240), radius=34, fill=ZT_OUTLINE + (40,), outline=ZT_OUTLINE + (160,), width=2)
    img.alpha_composite(layer)
    d = ImageDraw.Draw(img)
    for xy, text, font, fill in texts:
        d.text(xy, text, font=font, fill=fill, anchor="mm")
    d.text((W // 2, 1206), zt, font=_font(size, "Bold"), fill=WHITE, anchor="mm")
    d.text((W // 2, 1278), "Колір і цифра — скільки разів область згадували в повідомленнях про шахеди й ракети", font=_font(22, "SemiBold"), fill=FAINT, anchor="mm")
    d.text((W // 2, 1318), f"{channel_title} · карта © OpenStreetMap", font=_font(22, "SemiBold"), fill=FAINT, anchor="mm")

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


# --- публікація ----------------------------------------------------------------


def publish_attack(config, state, publisher, now: dt.datetime | None = None, force: bool = False, out_dir: Path | None = None, fetch=fetch_feed) -> list[str]:
    """Інфографіка в усі активні канали — раз на добу, з 08:30 до 11:00 за Києвом,
    коли Повітряні сили вже опублікували ранкове зведення. Немає зведення — чекаємо
    наступного запуску. publisher=None — лише зберегти картинку в out_dir."""
    from telegram_net.clock import local_now, start_of_local_day
    from telegram_net.state import iso as iso_time
    from telegram_net.weather import _channel_title

    now = now or dt.datetime.now(dt.timezone.utc)
    local = local_now(config, now)
    if not force and not (dt.time(8, 30) <= local.time() < dt.time(11, 0)):
        return [f"Зараз {local:%H:%M} за Києвом — підсумок ночі виходить з 08:30 до 11:00, пропускаю"]
    day_start = start_of_local_day(config, now)
    channels = [ch for ch in config.active_channels if publisher is None or not state.posts_since(ch.channel_id, day_start, kind="attack")]
    if not channels:
        return ["Підсумок ночі сьогодні вже був"]

    tz = ZoneInfo(config.settings.timezone)
    since = dt.datetime.combine(local.date() - dt.timedelta(days=1), dt.time(17, 30), tz)
    items = fetch(since)
    report = build_report(items, tz, local.date())
    if report is None:
        return [f"Ранкового зведення Повітряних сил за ніч на {local:%d.%m} ще немає ({len(items)} повідомлень) — спробую пізніше"]

    lines = []
    for ch in channels:
        image = render(report, _channel_title(ch))
        if out_dir is not None:
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / f"attack_{ch.channel_id}.png").write_bytes(image)
        if publisher is None:
            lines.append(f"{ch.channel_id}: [пробний] {caption(report)}")
            continue
        try:
            message_id = publisher.send_photo_bytes(ch.telegram_chat_id, image, post_caption(report), f"attack_{ch.channel_id}.png")
        except Exception as exc:
            logger.exception("Не вдалося надіслати підсумок ночі в %s", ch.channel_id)
            lines.append(f"{ch.channel_id}: ПОМИЛКА Telegram — {exc}")
            continue
        state.log_post(
            {
                "channel_id": ch.channel_id,
                "published_at": iso_time(now),
                "kind": "attack",
                "text": caption(report),
                "candidate_ids": [],
                "source_urls": [report.summary.url],
                "message_ids": [message_id],
            }
        )
        lines.append(f"{ch.channel_id}: надіслано — {caption(report)}")
    return lines
