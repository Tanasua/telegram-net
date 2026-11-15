"""Єдиний підпис під усіма постами каналів мережі «Мій …».

Три блоки (зразок — t.me/myberdychivnews/10):

    ❤️ Мій Бердичів | Новини Бердичева 🇺🇦

    ⚡️Новини нашого району⚡️

    👉👉👉 Підписатися 👈👈👈   (посилання на канал)
"""

from __future__ import annotations

import html

from telegram_net.config import ChannelConfig

LINE2 = "⚡️Новини нашого району⚡️"
LINE3 = "👉👉👉 Підписатися 👈👈👈"


def channel_title(ch: ChannelConfig) -> str:
    """«Мій Радомишль»: з subscribe_cta у лапках «…», інакше з назви."""
    cta = ch.subscribe_cta or ""
    if "«" in cta and "»" in cta:
        return cta[cta.index("«") + 1 : cta.index("»")]
    return f"Мій {ch.name}"


def _line1(ch: ChannelConfig) -> str:
    news = f"Новини {ch.name_genitive}" if ch.name_genitive else "Новини"
    return f"❤️ {channel_title(ch)} | {news} 🇺🇦"


def signature_plain(ch: ChannelConfig) -> str:
    return f"{_line1(ch)}\n\n{LINE2}\n\n{LINE3}"


def signature_html(ch: ChannelConfig) -> str:
    chat = ch.telegram_chat_id or ""
    line3 = html.escape(LINE3)
    if chat.startswith("@"):
        line3 = f'<a href="https://t.me/{chat[1:]}"><b>{line3}</b></a>'
    else:
        line3 = f"<b>{line3}</b>"
    return f"<b>{html.escape(_line1(ch))}</b>\n\n{html.escape(LINE2)}\n\n{line3}"


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def signature_len(ch: ChannelConfig) -> int:
    """Скільки символів підпис з'їдає від ліміту (разом з розділювачем \\n\\n)."""
    return utf16_len(signature_plain(ch)) + 2
