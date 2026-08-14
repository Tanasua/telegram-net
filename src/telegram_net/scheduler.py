"""Точка входу: один прогін конвеєра. Розклад — зовнішній cron/systemd timer, напр.:

    0 * * * *  cd /path/to/telegram-net && .venv/bin/python -m telegram_net.scheduler

Навмисно без внутрішнього sleep-циклу — простіше дивитись логи одного прогону
й рестартувати, ніж дебажити довгоживучий процес.
"""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from telethon.sync import TelegramClient

from telegram_net.classifier import Classifier
from telegram_net.config import GoogleSheetsConfigSource, LocalJSONConfigSource
from telegram_net.db import init_db, make_engine, make_session_factory
from telegram_net.fetchers.telegram_fetcher import TelethonFetcher
from telegram_net.fetchers.web_fetcher import WebFetcher
from telegram_net.generator import Generator
from telegram_net.pipeline import PipelineDeps, run_once
from telegram_net.publisher import TelegramPublisher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


def build_deps() -> PipelineDeps:
    load_dotenv()

    database_url = os.environ.get("DATABASE_URL", "sqlite:///telegram_net.db")
    engine = make_engine(database_url)
    init_db(engine)
    session_factory = make_session_factory(engine)

    spreadsheet_id = os.environ.get("GOOGLE_SHEETS_SPREADSHEET_ID")
    if spreadsheet_id:
        credentials_path = os.environ["GOOGLE_SHEETS_CREDENTIALS_JSON"]
        config = GoogleSheetsConfigSource(spreadsheet_id, credentials_path)
    else:
        seed_path = os.environ.get("LOCAL_CONFIG_PATH", "data/sources_seed.json")
        logger.warning("GOOGLE_SHEETS_SPREADSHEET_ID не задано — читаю конфіг з %s", seed_path)
        config = LocalJSONConfigSource(seed_path)

    api_id = os.environ["TELEGRAM_API_ID"]
    api_hash = os.environ["TELEGRAM_API_HASH"]
    session_name = os.environ.get("TELEGRAM_SESSION_NAME", "telegram_net_reader")
    telethon_client = TelegramClient(session_name, int(api_id), api_hash)
    telethon_client.connect()

    bot_token = os.environ["TELEGRAM_BOT_TOKEN"]

    return PipelineDeps(
        config=config,
        session_factory=session_factory,
        fetchers={
            "telegram": TelethonFetcher(telethon_client),
            "website": WebFetcher(),
        },
        classifier=Classifier(),
        generator=Generator(),
        publisher=TelegramPublisher(bot_token),
    )


def main() -> None:
    deps = build_deps()
    logger.info("Починаю прогін конвеєра")
    run_once(deps)
    logger.info("Прогін завершено")


if __name__ == "__main__":
    main()
