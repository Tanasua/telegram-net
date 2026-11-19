"""Знімок онлайн-карти повітряних загроз NEPTUN (neptun.in.ua) для каналів.

Сайт — безкоштовний інформаційний агрегатор; для вбудовування в інші сторінки в
нього є віджет `/embed/map` — його й відкриваємо в безголовому браузері та
знімаємо картинку. Підпис — лише час знімка, без застережень і посилань.

Що саме показує карта — вирішує сайт; код нічого на ній не домальовує і не
прибирає. Правило редакції: позицій ППО й ударів по військових об'єктах не
публікуємо — віджет їх не показує (лише загрози й області з тривогою).

Playwright імпортується ліниво: звичайним прогонам редактора він не потрібен.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

MAP_URL = "https://neptun.in.ua/embed/map"
VIEWPORT = {"width": 1000, "height": 800}
SETTLE_MS = 6000  # карта домальовується після networkidle
KYIV = ZoneInfo("Europe/Kyiv")


def capture_map(url: str = MAP_URL, timeout_ms: int = 45000) -> bytes | None:
    """PNG карти або None, якщо знімок не вдався (тоді сигнал тривоги однаково йде текстом)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        logger.warning("playwright не встановлено — знімок карти пропущено")
        return None
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM_PATH") or None)
            try:
                page = browser.new_page(viewport=VIEWPORT, device_scale_factor=1)
                page.goto(url, wait_until="networkidle", timeout=timeout_ms)
                page.wait_for_timeout(SETTLE_MS)
                return page.screenshot(type="png")
            finally:
                browser.close()
    except Exception:
        logger.exception("Не вдалося зробити знімок карти %s", url)
        return None


def map_caption(at: dt.datetime, district: str | None = None, threat: str | None = None) -> str:
    local = at.astimezone(KYIV)
    parts = [f"🗺 Карта повітряних загроз станом на {local:%H:%M}"]
    if threat and district:
        parts.append(f"{threat} у напрямку {district}ого району")
    elif threat:
        parts.append(threat)
    return "\n".join(parts)
