"""Публікація в один із 23 каналів через звичайний Telegram Bot API (один бот,
доданий адміном у всі канали) — httpx, без важких SDK-обгорток.

Ліміти, на які тут зважено (перевірено на серпень 2026, core.telegram.org/bots/api):
- sendMediaGroup: 2-10 елементів за раз;
- підпис (caption) для альбому кріпиться лише до першого елемента групи;
- стандартний Bot API — до 50 МБ на файл (для більшого потрібен окремий
  локальний Bot API сервер — не піднімаємо його заздалегідь без реальної потреби).
"""

from __future__ import annotations

from typing import Sequence

import httpx

from telegram_net.fetchers.base import FetchedMedia


class TelegramPublisher:
    def __init__(self, bot_token: str, http_client: httpx.Client | None = None):
        self._base_url = f"https://api.telegram.org/bot{bot_token}"
        self._http = http_client or httpx.Client(timeout=60)

    def send_text(self, chat_id: str, text: str) -> int:
        data = self._post("sendMessage", {"chat_id": chat_id, "text": text})
        return data["result"]["message_id"]

    def send_single_media(self, chat_id: str, media: FetchedMedia, caption: str | None = None) -> int:
        method = "sendPhoto" if media.type == "photo" else "sendVideo"
        field = "photo" if media.type == "photo" else "video"
        payload: dict = {"chat_id": chat_id, field: media.url}
        if caption:
            payload["caption"] = caption
        data = self._post(method, payload)
        return data["result"]["message_id"]

    def send_media_group(
        self, chat_id: str, media_batch: Sequence[FetchedMedia], caption: str | None = None
    ) -> list[int]:
        media_payload = []
        for i, m in enumerate(media_batch):
            entry: dict = {"type": "photo" if m.type == "photo" else "video", "media": m.url}
            if i == 0 and caption:
                entry["caption"] = caption
            media_payload.append(entry)
        data = self._post("sendMediaGroup", {"chat_id": chat_id, "media": media_payload})
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

        if len(first_batch) == 1:
            message_ids.append(self.send_single_media(chat_id, first_batch[0], caption=text))
        else:
            message_ids.extend(self.send_media_group(chat_id, first_batch, caption=text))

        for batch in rest_batches:
            if len(batch) == 1:
                message_ids.append(self.send_single_media(chat_id, batch[0]))
            else:
                message_ids.extend(self.send_media_group(chat_id, batch))

        return message_ids

    def _post(self, method: str, payload: dict) -> dict:
        resp = self._http.post(f"{self._base_url}/{method}", json=payload)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API {method} не виконався: {data}")
        return data
