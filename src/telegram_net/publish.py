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

from telegram_net.clock import is_quiet, local_now, start_of_local_day
from telegram_net.config import ChannelConfig, Config
from telegram_net.fetchers.base import FetchedMedia
from telegram_net.signature import signature_html, signature_len, signature_plain, utf16_len
from telegram_net.media import MAX_MEDIA_GROUP_SIZE, build_media_batches
from telegram_net.state import SERVICE_KINDS, State, iso, now_utc, parse_iso, post_time
from telegram_net.stoplist import find_stop_topic

logger = logging.getLogger(__name__)

NEWS_KINDS = ("local", "oblast", "national")
RUBRIC_KINDS = ("morning", "evening", "afisha")
MAX_PROCESSED_RUNS = 300
MAX_SEND_ATTEMPTS = 3


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
    urgent: bool = False
    run_id: str | None = None


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


def _render(headline: str, text: str, channel: ChannelConfig) -> tuple[str, str]:
    """Текст посту + підпис каналу (однаковий під усіма постами)."""
    plain = f"{headline}\n\n{text}" if headline else text
    body = f"<b>{html.escape(headline)}</b>\n\n{html.escape(text)}" if headline else html.escape(text)
    return plain, f"{body}\n\n{signature_html(channel)}"


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

        today = [p for p in state.posts_since(channel.channel_id, day_start) if p.get("kind") not in SERVICE_KINDS]
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

        plain, html_text = _render(headline, text, channel)

        limit = settings.max_caption_chars if media else settings.max_text_chars
        visible_len = utf16_len(plain) + signature_len(channel)
        if visible_len > limit:
            fail(f"задовгий текст: {visible_len} символів (з підписом каналу) при ліміті {limit}" + (" (підпис до медіа)" if media else ""))
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
            with_cta=True,
            urgent=bool(post.get("urgent")),
            run_id=str(decisions.get("run_id")),
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
                    report.published.append(f"[перевірка] бот може публікувати в {channel.telegram_chat_id} («{title}»)")
                except Exception as exc:
                    report.errors.append(f"канал {channel.telegram_chat_id}: бот не може публікувати — {exc}")
        for p in prepared:
            report.published.append(
                f"[пробний] {p.channel.channel_id} · {p.kind} · медіа: {len(p.media)}\n{p.plain_text}"
                + f"\n\n{signature_plain(p.channel)}"
            )
        return report

    # Пости не відправляються підряд: кожен отримує час виходу в черзі з
    # інтервалом post_spacing_minutes від попереднього посту того ж каналу.
    # Те, чий час уже настав (зазвичай перший пост каналу), надсилає drain()
    # одразу; решту — запуски drain.yml двічі на годину.
    for p in schedule(prepared, config, state, now):
        state.queue.append(p)
        due = local_now(config, parse_iso(p["due_at"])).strftime("%H:%M")
        report.published.append(f"{p['channel_id']} · {p['kind']} · заплановано на {due}")

    # Після прогону в пулі лишається тільки те, що редактор явно попросив
    # притримати ("keep") і що не було поставлено в чергу. Так пул не розростається,
    # а редактор не перечитує щогодини ті самі відхилені новини.
    queued_ids = {c for p in prepared for c in p.candidate_ids}
    keep = {str(c) for c in decisions["keep"]} - queued_ids
    state.pool = {cid: cand for cid, cand in state.pool.items() if cid in keep}

    sent_lines, send_errors = drain(config, state, publisher, now)
    report.published.extend(sent_lines)
    report.errors.extend(send_errors)

    processed.append(run_id)
    del processed[:-MAX_PROCESSED_RUNS]
    return report


def _next_open_time(config: Config, when: dt.datetime) -> dt.datetime:
    """Найближчий момент поза нічною тишею, не раніше `when`."""
    if not is_quiet(config, when):
        return when
    local = local_now(config, when)
    opening = local.replace(hour=config.settings.quiet_to_hour, minute=0, second=0, microsecond=0)
    if opening <= local:
        opening += dt.timedelta(days=1)
    return opening.astimezone(dt.timezone.utc)


def schedule(prepared: list[PreparedPost], config: Config, state: State, now: dt.datetime) -> list[dict]:
    """Призначає кожному посту час виходу. Порядок постів каналу в файлі рішень —
    порядок виходу; невідкладні (urgent) йдуть одразу, без інтервалу й черги."""
    spacing = dt.timedelta(minutes=config.settings.post_spacing_minutes)
    last: dict[str, dt.datetime] = {}
    for entry in state.published + state.queue:
        if entry.get("kind") in SERVICE_KINDS:
            continue
        t = post_time(entry)
        if entry["channel_id"] not in last or t > last[entry["channel_id"]]:
            last[entry["channel_id"]] = t

    entries = []
    for p in sorted(prepared, key=lambda x: (not x.urgent, x.index)):
        cid = p.channel.channel_id
        if p.urgent:
            due = now
        else:
            due = max(now, last[cid] + spacing) if cid in last else now
            due = _next_open_time(config, due)
        last[cid] = max(last.get(cid, due), due)
        entries.append(
            {
                "channel_id": cid,
                "due_at": iso(due),
                "kind": p.kind,
                "text": p.plain_text,
                "html": p.html_text,
                "media": [
                    {"type": m.type, "url": m.url, **({"duration_sec": m.duration_sec} if m.duration_sec else {})}
                    for m in p.media
                ],
                "candidate_ids": p.candidate_ids,
                "source_urls": p.source_urls,
                "run_id": p.run_id,
                "urgent": p.urgent,
                "attempts": 0,
            }
        )
    return entries


def drain(config: Config, state: State, publisher: Publisher, now: dt.datetime | None = None) -> tuple[list[str], list[str]]:
    """Надсилає з черги все, чий час настав. Повертає (надіслане, помилки)."""
    now = now or now_utc()
    sent_lines: list[str] = []
    errors: list[str] = []
    quiet = is_quiet(config, now)
    remaining: list[dict] = []
    for entry in sorted(state.queue, key=post_time):
        if parse_iso(entry["due_at"]) > now or (quiet and not entry.get("urgent")):
            remaining.append(entry)
            continue
        channel = config.channel(entry["channel_id"])
        if channel is None or not channel.active or not channel.telegram_chat_id:
            errors.append(f"{entry['channel_id']}: канал вимкнено — пост із черги прибрано")
            continue
        media = tuple(FetchedMedia(type=m["type"], url=m["url"], duration_sec=m.get("duration_sec")) for m in entry["media"])
        try:
            message_ids = publisher.publish(channel.telegram_chat_id, entry["html"], build_media_batches(media))
        except Exception as exc:
            entry["attempts"] = entry.get("attempts", 0) + 1
            logger.exception("Не вдалося надіслати пост із черги в %s", entry["channel_id"])
            if entry["attempts"] < MAX_SEND_ATTEMPTS:
                remaining.append(entry)
                errors.append(f"{entry['channel_id']}: помилка Telegram, спробую ще — {exc}")
            else:
                errors.append(f"{entry['channel_id']}: пост прибрано після {MAX_SEND_ATTEMPTS} невдалих спроб — {exc}")
            continue
        state.log_post(
            {
                "channel_id": entry["channel_id"],
                "published_at": iso(now),
                "kind": entry["kind"],
                "text": entry["text"],
                "candidate_ids": entry["candidate_ids"],
                "source_urls": entry["source_urls"],
                "message_ids": message_ids,
                "run_id": entry.get("run_id"),
            }
        )
        sent_lines.append(f"{entry['channel_id']} · {entry['kind']} · надіслано, повідомлення {message_ids}")
    state.queue = remaining
    return sent_lines, errors


def format_report(report: Report) -> str:
    lines = [f"Прогін {report.run_id}" + (" (пробний, нічого не надіслано)" if report.dry_run else "")]
    if report.skipped_run:
        lines.append("Цей прогін уже оброблено раніше — пропускаю.")
    lines += [f"  + {line}" for line in report.published]
    lines += [f"  ! {line}" for line in report.errors]
    if not report.published and not report.errors and not report.skipped_run:
        lines.append("  постів немає")
    return "\n".join(lines)
