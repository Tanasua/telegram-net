"""Скріншот мапи Нептуна й текстовий опис цілей біля міста — без моделей.

Після сигналу тривоги бот додає в канал фото мапи (neptun.in.ua, режим ?tv=1 —
без банерів) із районом каналу в центрі й підпис: що летить поблизу, де це
відносно міста й куди рухається. Дані беруться з потоку самого Нептуна
(вебсокет /api/v1/stream) — там цілі вже розібрані: координати, «звідки →
куди», курс. Текст складається за шаблоном.

Мапа Нептуна — чужий сервіс: підпис завжди містить посилання на джерело, а
дані подаються як орієнтовні.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

NEPTUN_URL = "https://neptun.in.ua/?tv=1"  # tv=1 — чиста карта без банера про застосунок
DEFAULT_RADIUS_KM = 120
MAX_THREATS_IN_TEXT = 5

_DIR8 = ("північ", "північний схід", "схід", "південний схід", "південь", "південний захід", "захід", "північний захід")
_DIR8_FROM = ("півночі", "північного сходу", "сходу", "південного сходу", "півдня", "південного заходу", "заходу", "північного заходу")
_DIR8_KEY = {"N": 0, "NE": 1, "E": 2, "SE": 3, "S": 4, "SW": 5, "W": 6, "NW": 7}


@dataclass(frozen=True)
class MapView:
    """Що показувати на мапі: центр (місто каналу) і радіус охоплення."""

    lat: float
    lon: float
    radius_km: float = DEFAULT_RADIUS_KM
    place_genitive: str = ""  # «Бердичева» — для «на північ від Бердичева»
    wide_radius_km: float | None = None  # другий, широкий кадр (сусідні області); None — лише один знімок


@dataclass(frozen=True)
class Threat:
    id: str
    title: str
    lat: float
    lon: float
    locality: str
    region: str
    count: int | None
    heading: float | None
    to_name: str | None
    to_dir: str | None
    from_name: str | None
    expires_at: dt.datetime | None
    distance_km: float = 0.0
    bearing_deg: float = 0.0


def _parse_ts(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dlmb = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Азимут з точки 1 на точку 2, градуси від півночі за годинниковою стрілкою."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dlmb = math.radians(lon2 - lon1)
    y = math.sin(dlmb) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dlmb)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def compass(deg: float, genitive_from: bool = False) -> str:
    idx = int((deg % 360 + 22.5) // 45) % 8
    return (_DIR8_FROM if genitive_from else _DIR8)[idx]


def parse_threat(raw: dict) -> Threat | None:
    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    to = raw.get("to") or {}
    frm = raw.get("from") or {}
    heading = raw.get("heading")
    count = raw.get("count")
    return Threat(
        id=str(raw.get("id", "")),
        title=str(raw.get("title") or "Ціль"),
        lat=lat,
        lon=lon,
        locality=str(raw.get("locality") or ""),
        region=str(raw.get("region") or ""),
        count=int(count) if isinstance(count, (int, float)) and count > 1 else None,
        heading=float(heading) if isinstance(heading, (int, float)) else None,
        to_name=to.get("name") or None,
        to_dir=to.get("dir8") or None,
        from_name=frm.get("name") or None,
        expires_at=_parse_ts(raw.get("expiresAt")),
    )


class ThreatBook:
    """Поточні цілі з потоку Нептуна: знімок + оновлення (upsert)."""

    def __init__(self) -> None:
        self._threats: dict[str, dict] = {}

    def feed(self, payload: str | bytes) -> None:
        try:
            msg = json.loads(payload)
        except (TypeError, ValueError):
            return
        data = msg.get("data") if isinstance(msg, dict) else None
        if not isinstance(data, dict):
            return
        kind = msg.get("type")
        threats = data.get("threats")
        if kind == "snapshot" and isinstance(threats, list):
            self._threats = {str(t.get("id")): t for t in threats if isinstance(t, dict)}
        elif kind == "batch" and isinstance(threats, dict):
            for t in threats.get("upsert") or []:
                if isinstance(t, dict):
                    self._threats[str(t.get("id"))] = t
            for tid in threats.get("remove") or threats.get("delete") or []:
                self._threats.pop(str(tid.get("id") if isinstance(tid, dict) else tid), None)

    @property
    def has_snapshot(self) -> bool:
        return bool(self._threats)

    def threats(self) -> list[Threat]:
        active = (r for r in self._threats.values() if r.get("status", "active") == "active")
        return [t for t in map(parse_threat, active) if t is not None]


def nearby(threats: list[Threat], view: MapView, now: dt.datetime) -> list[Threat]:
    """Активні цілі в радіусі view, від найближчої. Закінчені (expiresAt) відсіюються."""
    out = []
    for t in threats:
        if t.expires_at is not None and t.expires_at < now:
            continue
        d = haversine_km(view.lat, view.lon, t.lat, t.lon)
        if d > view.radius_km:
            continue
        b = bearing_deg(view.lat, view.lon, t.lat, t.lon)
        out.append(Threat(**{**t.__dict__, "distance_km": d, "bearing_deg": b}))
    return sorted(out, key=lambda t: t.distance_km)


def describe_threat(t: Threat, view: MapView) -> str:
    place = t.locality or t.region or "невідомо де"
    if t.locality and t.region:
        place = f"{t.locality} ({t.region.replace(' область', ' обл.')})"
    title = t.title
    if t.count and "(" not in title:
        title = f"{title} ×{t.count}"
    km = max(1, round(t.distance_km / 5) * 5) if t.distance_km >= 10 else max(1, round(t.distance_km))
    where = place
    if view.place_genitive:
        where += f", ≈{km} км на {compass(t.bearing_deg)} від {view.place_genitive}"
    course = ""
    if t.to_name and t.to_name != t.locality:  # «Кременчук … курс на Кременчук» — зайве
        course = f"; курс на {t.to_name}"
        if t.from_name:
            course += f" (із напрямку: {t.from_name})"
    elif t.heading is not None and not t.to_name:
        course = f"; рухається на {compass(t.heading)}"
    elif t.from_name:
        course = f"; із напрямку: {t.from_name}"
    return f"• {title} — {where}{course}"


def render_caption(found: list[Threat], view: MapView, at: dt.datetime, max_chars: int) -> str:
    """Підпис до скріншота. Вкладається в max_chars (мінімум — заголовок і джерело)."""
    place = view.place_genitive or "району"
    head = f"🗺 <b>Що на мапі поблизу {place}</b> (станом на {at:%H:%M})"
    foot = '<i>Орієнтовно, за даними <a href="https://neptun.in.ua">Нептуна</a>.</i>'
    if not found:
        return f"{head}\nУ радіусі {round(view.radius_km)} км цілей не видно.\n\n{foot}"
    lines: list[str] = []
    budget = max_chars - len(head) - len(foot) - 8
    for i, t in enumerate(found[:MAX_THREATS_IN_TEXT]):
        line = describe_threat(t, view)
        if len("\n".join(lines + [line])) > budget - 30:
            break
        lines.append(line)
    rest = len(found) - len(lines)
    if rest > 0:
        lines.append(f"…і ще {rest} на мапі")
    return f"{head}\n" + "\n".join(lines) + f"\n\n{foot}"


def _overlay_js() -> str:
    # Наводить мапу на район і ставить підпис міста. Мапу (екземпляр maplibre)
    # знаходимо через React-fiber: сайт не віддає її у window.
    return """
    async ({lat, lon, radius, pad}) => {
      const el = document.querySelector('.maplibregl-map');
      const key = Object.keys(el).find(k => k.startsWith('__reactFiber'));
      const isMap = o => o && typeof o === 'object' && typeof o.fitBounds === 'function' && typeof o.getCenter === 'function';
      const seen = new Set(); let map = null;
      const scan = (o, d) => {
        if (map || !o || typeof o !== 'object' || seen.has(o) || d > 4) return;
        seen.add(o); if (isMap(o)) { map = o; return; }
        if (o instanceof Node || o === window) return;
        for (const k of Object.keys(o).slice(0, 60)) { try { scan(o[k], d + 1); } catch (e) {} }
      };
      for (let f = el[key], i = 0; f && !map && i < 40; f = f.return, i++) {
        scan(f.memoizedState, 0); scan(f.memoizedProps, 0); scan(f.ref, 0);
      }
      if (!map) throw new Error('map instance not found');
      const fit = radius * pad;  // трохи ширше за радіус опису, щоб цілі на межі не ховались під краєм
      const dLat = fit / 111.0, dLon = fit / (111.0 * Math.cos(lat * Math.PI / 180));
      map.fitBounds([[lon - dLon, lat - dLat], [lon + dLon, lat + dLat]], {duration: 0, padding: 10});
      await new Promise(r => { let done = false; const fin = () => { if (!done) { done = true; r(); } }; map.once('idle', fin); setTimeout(fin, 6000); });
      const p = map.project([lon, lat]);
      document.querySelectorAll('.neptun-mark').forEach(e => e.remove());
      const mark = document.createElement('div');
      mark.className = 'neptun-mark';
      mark.style.cssText = `position:fixed;left:${p.x}px;top:${p.y}px;z-index:99999;transform:translate(-50%,-50%);pointer-events:none;`;
      mark.innerHTML = '<div style="width:16px;height:16px;border-radius:50%;background:#fff;border:3px solid #1a73e8;box-shadow:0 0 0 4px rgba(26,115,232,.35)"></div>';
      document.body.appendChild(mark);
    }
    """


def capture(view: MapView, settle_sec: float = 4.0, timeout_ms: int = 60000) -> tuple[list[bytes], ThreatBook]:
    """Відкриває Нептун і повертає (знімки, цілі з потоку): крупний кадр на район
    і, якщо задано wide_radius_km, широкий — з сусідніми областями.
    Потребує playwright і Chromium; шлях до власного Chromium — у NEPTUN_CHROMIUM."""
    from playwright.sync_api import sync_playwright

    book = ThreatBook()
    shots: list[bytes] = []
    frames = [(view.radius_km, 1.2)]  # крупний кадр трохи ширший за радіус опису
    if view.wide_radius_km:
        frames.append((view.wide_radius_km, 1.0))
    with sync_playwright() as pw:
        kwargs = {"args": ["--no-sandbox"]}
        if os.environ.get("NEPTUN_CHROMIUM"):
            kwargs["executable_path"] = os.environ["NEPTUN_CHROMIUM"]
        browser = pw.chromium.launch(**kwargs)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 900}, device_scale_factor=1)
            page.on("websocket", lambda ws: ws.on("framereceived", lambda payload: book.feed(payload)))
            page.goto(NEPTUN_URL, wait_until="networkidle", timeout=timeout_ms)
            page.wait_for_selector(".maplibregl-map", timeout=timeout_ms)
            page.wait_for_timeout(int(settle_sec * 1000))
            for radius, pad in frames:
                page.evaluate(_overlay_js(), {"lat": view.lat, "lon": view.lon, "radius": radius, "pad": pad})
                page.wait_for_timeout(1500)
                shots.append(page.screenshot(type="png"))
        finally:
            browser.close()
    return shots, book


Capturer = Callable[[MapView], tuple[list[bytes], list[Threat]]]


def default_capturer(view: MapView) -> tuple[list[bytes], list[Threat]]:
    shots, book = capture(view)
    return shots, book.threats()
