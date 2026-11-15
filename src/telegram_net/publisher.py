"""Публікація в канали мережі через звичайний Telegram Bot API (один бот,
доданий адміном у всі канали) — httpx, без важких SDK-обгорток.

Текст надсилається з parse_mode=HTML (жирний заголовок, посилання в заклику
підписатись) — розмітку будує код у publish.py, редактор її не пише.

Медіа спершу передається посиланням (Telegram сам його завантажує). Якщо
Telegram не зміг — файл завантажується й надсилається напряму; якщо й так ні —
publish() надсилає пост без медіа, щоб новина не губилась через картинку.

Ліміти, на які тут зважено (перевірено на серпень 2026, core.telegram.org/bots/api):
- sendMediaGroup: 2-10 елементів за раз;
- підпис (caption) для альбому кріпиться лише до першого елемента групи;
- стандартний Bot API — до 50 МБ на файл (для більшого потрібен окремий
  локальний Bot API сервер — не піднімаємо його заздалегідь без реальної потреби).
"""

from __future__ import annotations

import json
import logging
from typing import Sequence

import httpx

logger = logging.getLogger(__name__)

_MAX_UPLOAD_BYTES = 45 * 1024 * 1024

from telegram_net.fetchers.base import FetchedMedia


class TelegramPublisher:
    def __init__(self, bot_token: str, http_client: httpx.Client | None = None):
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._http = http_client or httpx.Client(timeout=60)

    def send_text(self, chat_id: str, text: str) -> int:
        data = self._post(
            "sendMessage",
            {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        )
        return data["result"]["message_id"]

    def check_chat(self, chat_id: str) -> str:
        """Перевірка доступу бота до каналу (для пробного прогону) — нічого не публікує."""
        data = self._post("getChat", {"chat_id": chat_id})
        return str(data["result"].get("title", chat_id))

    def send_single_media(self, chat_id: str, media: FetchedMedia, caption: str | None = None) -> int:
        method = "sendPhoto" if media.type == "photo" else "sendVideo"
        field = "photo" if media.type == "photo" else "video"
        payload: dict = {"chat_id": chat_id, field: media.url}
        if caption:
            payload["caption"] = caption
            payload["parse_mode"] = "HTML"
        try:
            data = self._post(method, payload)
        except RuntimeError as exc:
            logger.warning("%s за посиланням не вдався (%s) — завантажую файл сам", method, exc)
            data = self._upload(method, payload, {field: media.url})
        return data["result"]["message_id"]

    def send_media_group(
        self, chat_id: str, media_batch: Sequence[FetchedMedia], caption: str | None = None
    ) -> list[int]:
        media_payload = []
        for i, m in enumerate(media_batch):
            entry: dict = {"type": "photo" if m.type == "photo" else "video", "media": m.url}
            if i == 0 and caption:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media_payload.append(entry)
        try:
            data = self._post("sendMediaGroup", {"chat_id": chat_id, "media": media_payload})
        except RuntimeError as exc:
            logger.warning("sendMediaGroup за посиланнями не вдався (%s) — завантажую файли сам", exc)
            files = {}
            for i, entry in enumerate(media_payload):
                files[f"file{i}"] = entry["media"]
                entry["media"] = f"attach://file{i}"
            data = self._upload("sendMediaGroup", {"chat_id": chat_id, "media": json.dumps(media_payload)}, files)
        return [r["message_id"] for r in data["result"]]

    def publish(
        self, chat_id: str, text: str, media_batches: list[tuple[FetchedMedia, ...]]
    ) -> list[int]:
        """Підпис-текст кріпиться до першої партії медіа (чи йде окремим повідомленням,
        якщо медіа немає). Наступні партії (>10 медіа загалом) — без підпису, він
        уже був сказаний у першій."""
        if not media_batches:
            return [self.send_text(chat_id, text)]

        message_ids: list[int] = []
        first_batch, *rest_batches = media_batches

        try:
            if len(first_batch) == 1:
                message_ids.append(self.send_single_media(chat_id, first_batch[0], caption=text))
            else:
                message_ids.extend(self.send_media_group(chat_id, first_batch, caption=text))
        except (RuntimeError, httpx.HTTPError) as exc:
            logger.warning("Медіа не надіслалось (%s) — публікую пост без нього", exc)
            return [self.send_text(chat_id, text)]

        for batch in rest_batches:
            if len(batch) == 1:
                message_ids.append(self.send_single_media(chat_id, batch[0]))
            else:
                message_ids.extend(self.send_media_group(chat_id, batch))

        return message_ids

    def _upload(self, method: str, payload: dict, file_urls: dict[str, str]) -> dict:
        """Запасний шлях: завантажити файли самим і надіслати multipart-запитом."""
        files = {}
        for field, url in file_urls.items():
            resp = self._http.get(url, follow_redirects=True)
            resp.raise_for_status()
            if len(resp.content) > _MAX_UPLOAD_BYTES:
                raise RuntimeError(f"файл {url} завеликий для Bot API ({len(resp.content)} байт)")
            name = url.rsplit("/", 1)[-1].split("?")[0] or field
            files[field] = (name, resp.content)
        data = {k: v for k, v in payload.items() if k not in file_urls}
        resp = self._http.post(f"{self._base_url}/{method}", data=data, files=files)
        return self._parse(method, resp)

    def _post(self, method: str, payload: dict) -> dict:
        resp = self._http.post(f"{self._base_url}/{method}", json=payload)
        return self._parse(method, resp)

    def _parse(self, method: str, resp: httpx.Response) -> dict:
        try:
            data = resp.json()
        except ValueError:
            # Не JSON-відповідь (мережева помилка, проксі тощо) — тут дійсно
            # більше нема чого сказати, крім статусу.
            resp.raise_for_status()
            raise
        if not data.get("ok"):
            # Telegram майже завжди повертає 4xx РАЗОМ з інформативним JSON-тілом
            # ("chat not found", "not enough rights" тощо) — якщо викликати
            # resp.raise_for_status() раніше за парсинг тіла, цей опис губиться
            # і лишається лише голий код статусу. Тому тіло парситься першим.
            raise RuntimeError(
                f"Telegram API {method} не виконався ({resp.status_code}): "
                f"{data.get('description', data)}"
            )
        return data
