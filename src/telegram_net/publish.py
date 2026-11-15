"""Крок 3 прогону — перевірка й публікація рішень редактора.

Редактор (Claude) пише файл рішень outbox/<run_id>.json:

    {
      "run_id": "2026-11-14T15-00",
      "dry_run": false,
      "posts": [
        {"channel_id": "malyn", "kind": "local", "candidate_ids": ["ab12cd34"],
         "headline": "необов'язковий заголовок", "text": "текст посту",
         "media": "all" | "first" | "none", "urgent": false}
      ],
      "keep": ["ffeeddcc", ...]
    }

"keep" — кандидати, які редактор хоче побачити ще раз наступного прогону (не
влізли в ліміт, чекають уточнення). Усе інше з пулу після публікації
прибирається: використане — бо вже вийшло, решта — бо редактор її відхилив.

Усе, що можна перевірити кодом, перевіряється тут, а не «на довірі» до
редактора: стоп-теми, нічна тиша, добові ліміти, довжина, повтор тієї самої
новини в каналі. Пост, що не пройшов перевірку, не публікується; решта — так.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, Sequence

from telegram_net.clock import is_quiet, start_of_local_day
from telegram_net.config import ChannelConfig, Config
from telegram_net.fetchers.base import FetchedMedia
from telegram_net.media import MAX_MEDIA_GROUP_SIZE, build_media_batches
from telegram_net.state import State, iso, now_utc
from telegram_net.stoplist import find_stop_topic

logger = logging.getLogger(__name__)

NEWS_KINDS = ("local", "oblast", "national")
RUBRIC_KINDS = ("morning", "evening", "afisha")
MAX_PROCESSED_RUNS = 300


class Publisher(Protocol):
    def publish(self, chat_id: str, text: str, media_batches: list[tuple[FetchedMedia, ...]]) -> list[int]: ...
    def check_chat(self, chat_id: str) -> str: ...


@dataclass
class PreparedPost:
    index: int
    channel: ChannelConfig
    kind: str
    plain_text: str
    html_text: str
    media: tuple[FetchedMedia, ...]
    candidate_ids: list[str]
    source_urls: list[str]
    with_cta: bool


@dataclass
class Report:
    run_id: str
    dry_run: bool
    published: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skipped_run: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors


def load_decisions(path: str | Path) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not data.get("run_id"):
        raise ValueError(f"{path}: немає run_id")
    data.setdefault("posts", [])
    data.setdefault("keep", [])
    return data


def _cta_html(channel: ChannelConfig) -> str:
    label = html.escape(channel.subscribe_cta or f"Підписуйся на {channel.name}")
    chat = channel.telegram_chat_id or ""
    if chat.startswith("@"):
        return f'<a href="https://t.me/{chat[1:]}">{label}</a>'
    return label


def _render(headline: str, text: str, channel: ChannelConfig, with_cta: bool) -> tuple[str, str]:
    plain = f"{headline}\n\n{text}" if headline else text
    body = f"<b>{html.escape(headline)}</b>\n\n{html.escape(text)}" if headline else html.escape(text)
    if with_cta:
        body = f"{body}\n\n{_cta_html(channel)}"
    return plain, body


def prepare(decisions: dict, config: Config, state: State, now: dt.datetime | None = None) -> tuple[list[PreparedPost], list[str]]:
    """Перевіряє всі пости, нічого не надсилаючи. Повертає (придатні, помилки)."""
    now = now or now_utc()
    settings = config.settings
    day_start = start_of_local_day(config, now)
    quiet = is_quiet(config, now)
    prepared: list[PreparedPost] = []
    errors: list[str] = []
    in_run: dict[str, list[PreparedPost]] = {}

    for index, post in enumerate(decisions["posts"]):
        label = f"пост №{index + 1} ({post.get('channel_id')})"

        def fail(reason: str) -> None:
            errors.append(f"{label}: {reason}")

        channel = config.channel(str(post.get("channel_id")))
        if channel is None or not channel.active or not channel.telegram_chat_id:
            fail("невідомий або неактивний канал")
            continue
        kind = post.get("kind")
        if kind not in NEWS_KINDS + RUBRIC_KINDS:
            fail(f"невідомий kind {kind!r}")
            continue
        headline = str(post.get("headline") or "").strip()
        text = str(post.get("text") or "").strip()
        if not text:
            fail("порожній текст")
            continue
        stop = find_stop_topic(f"{headline}\n{text}")
        if stop:
            fail(f"стоп-тема («{stop}»)")
            continue
        if quiet and not post.get("urgent"):
            fail("нічна тиша — публікуються лише пости з urgent: true")
            continue

        candidate_ids = [str(c) for c in post.get("candidate_ids") or []]
        missing = [c for c in candidate_ids if c not in state.pool]
        if missing:
            fail(f"кандидатів {missing} немає в пулі")
            continue
        if kind in NEWS_KINDS and not candidate_ids:
            fail("новина без candidate_ids — писати можна лише з наданих джерел")
            continue

        already = state.posts_since(channel.channel_id, now - dt.timedelta(days=30)) + [
            {"candidate_ids": p.candidate_ids} for p in in_run.get(channel.channel_id, [])
        ]
        used = {c for p in already for c in p.get("candidate_ids", [])}
        repeated = [c for c in candidate_ids if c in used]
        if repeated and kind in NEWS_KINDS:
            fail(f"новина з кандидата {repeated} у цьому каналі вже виходила")
            continue

        today = [p for p in state.posts_since(channel.channel_id, day_start) if p.get("kind") != "alert"]
        this_run = in_run.get(channel.channel_id, [])
        if len(today) + len(this_run) >= channel.daily_limit:
            fail(f"добовий ліміт каналу ({channel.daily_limit}) вичерпано")
            continue
        news_this_run = [p for p in this_run if p.kind in NEWS_KINDS]
        if kind in NEWS_KINDS and len(news_this_run) >= channel.max_posts_per_run:
            fail(f"ліміт новин за прогін ({channel.max_posts_per_run}) вичерпано")
            continue
        if kind in RUBRIC_KINDS and (
            any(p.kind == kind for p in this_run) or any(p.get("kind") == kind for p in today)
        ):
            fail(f"рубрика {kind} сьогодні вже виходила")
            continue
        if kind == "national":
            national = len([p for p in today if p.get("kind") == "national"]) + len(
                [p for p in this_run if p.kind == "national"]
            )
            if national >= settings.national_daily_limit:
                fail(f"добовий ліміт всеукраїнських новин ({settings.national_daily_limit}) вичерпано")
                continue

        media_mode = post.get("media", "all")
        media: list[FetchedMedia] = []
        if media_mode != "none":
            # Медіа — з першого кандидата, де воно є: фото з різних джерел про одну
            # подію зазвичай повторюють одне одного.
            for cid in candidate_ids:
                found = state.pool[cid].get("media", [])
                if found:
                    media = [FetchedMedia(type=m["type"], url=m["url"], duration_sec=m.get("duration_sec")) for m in found]
                    break
            media = media[:1] if media_mode == "first" else media[:MAX_MEDIA_GROUP_SIZE]

        # Заклик підписатись — у кожному N-му пості каналу, а не в кожному.
        news_count = len([p for p in state.published if p["channel_id"] == channel.channel_id and p.get("kind") != "alert"])
        with_cta = channel.cta_every > 0 and (news_count + len(this_run) + 1) % channel.cta_every == 0
        plain, html_text = _render(headline, text, channel, with_cta)

        limit = settings.max_caption_chars if media else settings.max_text_chars
        visible_len = len(plain) + (len(channel.subscribe_cta or "") + 2 if with_cta else 0)
        if visible_len > limit:
            fail(f"задовгий текст: {visible_len} символів при ліміті {limit}" + (" (підпис до медіа)" if media else ""))
            continue

        item = PreparedPost(
            index=index,
            channel=channel,
            kind=kind,
            plain_text=plain,
            html_text=html_text,
            media=tuple(media),
            candidate_ids=candidate_ids,
            source_urls=[state.pool[c]["url"] for c in candidate_ids],
            with_cta=with_cta,
        )
        prepared.append(item)
        in_run.setdefault(channel.channel_id, []).append(item)

    return prepared, errors


def publish_run(
    decisions: dict,
    config: Config,
    state: State,
    publisher: Publisher | None,
    now: dt.datetime | None = None,
) -> Report:
    """Публікує придатні пости й оновлює стан. publisher=None або dry_run — лише перевірка."""
    now = now or now_utc()
    run_id = str(decisions["run_id"])
    dry_run = bool(decisions.get("dry_run")) or publisher is None
    report = Report(run_id=run_id, dry_run=dry_run)

    processed = state.runs
    if run_id in processed:
        # Повторний запуск workflow для того самого файлу не має дублювати пости.
        report.skipped_run = True
        return report

    prepared, errors = prepare(decisions, config, state, now)
    report.errors.extend(errors)

    if dry_run:
        if publisher is not None:
            for channel in {p.channel.channel_id: p.channel for p in prepared}.values():
                try:
                    title = publisher.check_chat(channel.telegram_chat_id)
                    report.published.append(f"[перевірка] бот має доступ до {channel.telegram_chat_id} («{title}»)")
                except Exception as exc:
                    report.errors.append(f"канал {channel.telegram_chat_id}: бот не має доступу — {exc}")
        for p in prepared:
            report.published.append(
                f"[пробний] {p.channel.channel_id} · {p.kind} · медіа: {len(p.media)}\n{p.plain_text}"
                + ("\n[+ заклик підписатись]" if p.with_cta else "")
            )
        return report

    sent: list[PreparedPost] = []
    for p in prepared:
        try:
            message_ids = publisher.publish(p.channel.telegram_chat_id, p.html_text, build_media_batches(p.media))
        except Exception as exc:
            logger.exception("Не вдалося опублікувати пост №%d у %s", p.index + 1, p.channel.channel_id)
            report.errors.append(f"пост №{p.index + 1} ({p.channel.channel_id}): помилка Telegram — {exc}")
            continue
        state.log_post(
            {
                "channel_id": p.channel.channel_id,
                "published_at": iso(now),
                "kind": p.kind,
                "text": p.plain_text,
                "candidate_ids": p.candidate_ids,
                "source_urls": p.source_urls,
                "message_ids": message_ids,
                "run_id": run_id,
            }
        )
        sent.append(p)
        report.published.append(f"{p.channel.channel_id} · {p.kind} · повідомлення {message_ids}")

    # Після прогону в пулі лишається тільки те, що редактор явно попросив
    # притримати ("keep") і що не було опубліковано. Так пул не розростається,
    # а редактор не перечитує щогодини ті самі відхилені новини.
    sent_ids = {c for p in sent for c in p.candidate_ids}
    keep = {str(c) for c in decisions["keep"]} - sent_ids
    failed_ids = {c for p in prepared if p not in sent for c in p.candidate_ids}  # Telegram підвів — спробуємо ще
    state.pool = {cid: cand for cid, cand in state.pool.items() if cid in keep | failed_ids}

    processed.append(run_id)
    del processed[:-MAX_PROCESSED_RUNS]
    return report


def format_report(report: Report) -> str:
    lines = [f"Прогін {report.run_id}" + (" (пробний, нічого не надіслано)" if report.dry_run else "")]
    if report.skipped_run:
        lines.append("Цей прогін уже оброблено раніше — пропускаю.")
    lines += [f"  + {line}" for line in report.published]
    lines += [f"  ! {line}" for line in report.errors]
    if not report.published and not report.errors and not report.skipped_run:
        lines.append("  постів немає")
    return "\n".join(lines)
