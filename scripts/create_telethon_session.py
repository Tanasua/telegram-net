"""ОДНОРАЗОВИЙ локальний скрипт. Логін в Telegram (номер телефону + код
підтвердження) можна зробити тільки інтерактивно — GitHub Actions цього не
вміє. Тому: запускаєш це ОДИН РАЗ на будь-якому своєму комп'ютері, отримуєш
рядок, вставляєш його в GitHub Secret TELEGRAM_SESSION_B64 — і все, цей
комп'ютер більше не потрібен, бот далі живе тільки на GitHub Actions.

Використання:
    pip install telethon
    TELEGRAM_API_ID=... TELEGRAM_API_HASH=... python scripts/create_telethon_session.py
"""

from __future__ import annotations

import base64
import os

from telethon.sync import TelegramClient


def main() -> None:
    api_id = os.environ["TELEGRAM_API_ID"]
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_name = os.environ.get("TELEGRAM_SESSION_NAME", "telegram_net_reader")

    with TelegramClient(session_name, int(api_id), api_hash) as client:
        me = client.get_me()
        print(f"Успішно залогінено як: {me.first_name} (@{me.username})")

    session_path = f"{session_name}.session"
    with open(session_path, "rb") as f:
        encoded = base64.b64encode(f.read()).decode()

    print("\nГотово! Файл сесії створено локально:", session_path)
    print("Встав ЦЕ значення в GitHub Secret під назвою TELEGRAM_SESSION_B64:\n")
    print(encoded)
    print("\nПісля цього файл", session_path, "можна видалити з цього комп'ютера.")


if __name__ == "__main__":
    main()
