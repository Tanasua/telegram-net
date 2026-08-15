"""Один прогін конвеєра: для кожного активного каналу — всі активні джерела,
нове забрати, дублі й нерелевантне/рекламу відсіяти, решту — згенерувати й
опублікувати. Викликається scheduler.py щогодини (по одному проходу за раз,
без внутрішнього sleep-циклу — розклад задає зовнішній cron/systemd timer)."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

from sqlalchemy.orm import Session, sessionmaker

from telegram_net.classifier import Classifier
from telegram_net.config import ChannelConfig, ConfigSource, SourceConfig
from telegram_net.db import Classification as ClassificationRow
from telegram_net.db import GeneratedPost, MediaAsset, PublishLog, RawItem
from telegram_net.db import Source as SourceRow
from telegram_net.dedup import content_hash
from telegram_net.fetchers.base import Fetcher, FetchedItem
from telegram_net.generator import Generator
from telegram_net.media import build_media_batches
from telegram_net.publisher import TelegramPublisher
from telegram_net.style import pick_style

logger = logging.getLogger(__name__)


@dataclass
class PipelineDeps:
    config: ConfigSource
    session_factory: sessionmaker
    fetchers: dict[str, Fetcher]  # ключ — SourceType ("telegram" | "website" | "facebook_page")
    classifier: Classifier
    generator: Generator
    publisher: TelegramPublisher


def run_once(deps: PipelineDeps) -> None:
    channels = {c.channel_id: c for c in deps.config.load_channels()}
    sources = [s for s in deps.config.load_sources() if s.active]
    published_counts: dict[str, int] = {}  # channel_id -> скільки вже опубліковано за цей прогін

    for source in sources:
        channel = channels.get(source.channel_id)
        if channel is None or not channel.active:
            continue
        fetcher = deps.fetchers.get(source.type)
        if fetcher is None:
            logger.warning("Немає фетчера для типу джерела %s (канал %s)", source.type, source.channel_id)
            continue

        with deps.session_factory() as session:
            _process_source(session, deps, channel, source, fetcher, published_counts)


def _process_source(
    session: Session,
    deps: PipelineDeps,
    channel: ChannelConfig,
    source: SourceConfig,
    fetcher: Fetcher,
    published_counts: dict[str, int],
) -> None:
    source_row = _get_or_create_source_row(session, source)

    try:
        items, new_cursor = fetcher.fetch_new(source, source_row.last_seen_cursor)
    except Exception:
        logger.exception("Помилка читання джерела %s (%s)", source.name, source.handle_or_url)
        return

    source_row.last_seen_cursor = new_cursor
    session.commit()

    for item in items:
        try:
            _process_item(session, deps, channel, source_row, item, published_counts)
        except Exception:
            logger.exception("Помилка обробки елемента %s з джерела %s", item.external_id, source.name)
            session.rollback()


def _get_or_create_source_row(session: Session, source: SourceConfig) -> SourceRow:
    row = (
        session.query(SourceRow)
        .filter_by(channel_id=source.channel_id, handle_or_url=source.handle_or_url)
        .one_or_none()
    )
    if row is None:
        row = SourceRow(channel_id=source.channel_id, type=source.type, handle_or_url=source.handle_or_url)
        session.add(row)
        session.commit()
    return row


def _process_item(
    session: Session,
    deps: PipelineDeps,
    channel: ChannelConfig,
    source_row: SourceRow,
    item: FetchedItem,
    published_counts: dict[str, int],
) -> None:
    if _is_stale(item, channel.max_age_minutes):
        return  # застаріла новина (старша за поріг свіжості каналу) — не публікуємо старе як щойно

    text_hash = content_hash(item.text or item.url)

    already_seen = session.query(RawItem).filter_by(content_hash=text_hash).one_or_none()
    if already_seen is not None:
        return  # та ж новина вже була (з цього чи іншого джерела)

    raw_item = RawItem(
        source_id=source_row.id,
        external_id=item.external_id,
        url=item.url,
        text=item.text,
        content_hash=text_hash,
        origin_published_at=item.published_at,
    )
    session.add(raw_item)
    session.flush()

    for order, media in enumerate(item.media):
        session.add(
            MediaAsset(
                raw_item_id=raw_item.id,
                type=media.type,
                source_url=media.url,
                order_in_gallery=order,
                duration_sec=media.duration_sec,
            )
        )
    session.commit()

    if published_counts.get(channel.channel_id, 0) >= channel.max_posts_per_run:
        return  # ліміт постів на канал за цей прогін вже вичерпано — новину зафіксовано, але не публікуємо

    classification = deps.classifier.classify(item, channel.name)
    session.add(
        ClassificationRow(
            raw_item_id=raw_item.id,
            is_advertisement=classification.is_advertisement,
            relevance_to_region=classification.relevance_to_region,
            local_news_interest=classification.local_news_interest,
            reason=classification.reason,
            model=deps.classifier.model,
        )
    )
    session.commit()

    if classification.is_advertisement or classification.relevance_to_region < channel.min_relevance:
        return

    style = pick_style(channel.length_weights, channel.headline_probability)
    post_text = deps.generator.generate(item, style, channel.name)
    if channel.subscribe_cta:
        # Приписка додається кодом, а не LLM — гарантія, що назва каналу в
        # заклику завжди правильна й не спотворена генерацією.
        post_text = f"{post_text}\n\n{channel.subscribe_cta}"

    generated = GeneratedPost(
        raw_item_id=raw_item.id,
        channel_id=channel.channel_id,
        text=post_text,
        length_style=style.length,
        has_headline=style.with_headline,
        status="draft",
    )
    session.add(generated)
    session.commit()

    if not channel.telegram_chat_id:
        logger.warning("Канал %s без telegram_chat_id — пост збережено як draft, публікацію пропущено", channel.channel_id)
        return

    media_batches = build_media_batches(item.media)
    message_ids = deps.publisher.publish(channel.telegram_chat_id, post_text, media_batches)

    generated.status = "published"
    for message_id in message_ids:
        session.add(PublishLog(generated_post_id=generated.id, telegram_message_id=message_id))
    session.commit()

    published_counts[channel.channel_id] = published_counts.get(channel.channel_id, 0) + 1


def _is_stale(item: FetchedItem, max_age_minutes: int) -> bool:
    if item.published_at is None:
        return False  # немає дати (напр. знімок сайту без RSS) — не можемо судити, пропускаємо перевірку
    published_at = item.published_at
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=dt.timezone.utc)
    age = dt.datetime.now(dt.timezone.utc) - published_at
    return age > dt.timedelta(minutes=max_age_minutes)
