"""Крок 1 прогону — збір кандидатів (без жодного ШІ).

Обходить усі активні джерела, відсіює старе, дублі, стоп-теми, сигнали тривоги
й логотипи замість фото, а решту складає в пул кандидатів. Результат — JSON,
який читає редактор (Claude, див. EDITOR.md): кандидати + що вже виходило в
кожному каналі + ліміти на сьогодні. Редактор НЕ ходить по джерелах сам.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import re
from typing import Callable, Protocol
from zoneinfo import ZoneInfo

from telegram_net.config import ChannelConfig, Config, SourceConfig
from telegram_net.dedup import content_hash
from telegram_net.fetchers.article import Article, looks_like_logo
from telegram_net.fetchers.base import FetchedItem, Fetcher
from telegram_net.state import State, iso, now_utc, parse_iso
from telegram_net.stoplist import find_stop_topic

logger = logging.getLogger(__name__)

MAX_CANDIDATE_CHARS = 1500
MAX_NATIONAL_PER_SOURCE = 6  # всеукраїнські стрічки дуже щільні — беремо лише найсвіжіше
MAX_ARTICLES_PER_SOURCE = 8
MIN_TEXT_CHARS = 20
RECENT_POSTS_HOURS = 36
RECENT_POSTS_LIMIT = 30

# Сигнали тривоги публікує окремий процес (alerts.py) за шаблоном і за хвилину,
# а не редактор раз на годину — тому в кандидати вони не потрапляють.
_ALERT_RE = re.compile(r"^\W*(?:[\w,\s]+ район\w*|[\w\s]+ област\w*)?\s*[—-]?\s*(?:повітрян\w+ тривог|відбій)", re.IGNORECASE)
_ALERT_ANY_RE = re.compile(r"повітрян\w+ тривог\w*|відбій (?:повітрян\w+ )?тривог\w*", re.IGNORECASE)


class ArticleSource(Protocol):
    def fetch(self, url: str) -> Article | None: ...


def is_alert_signal(text: str) -> bool:
    head = text.strip()[:200]
    return bool(_ALERT_RE.search(head)) or (len(text) < 300 and bool(_ALERT_ANY_RE.search(text)))


def candidate_id(source: SourceConfig, external_id: str) -> str:
    return hashlib.sha256(f"{source.key}|{external_id}".encode("utf-8")).hexdigest()[:8]


def _local_now(config: Config, now: dt.datetime) -> dt.datetime:
    return now.astimezone(ZoneInfo(config.settings.timezone))


def is_quiet(config: Config, now: dt.datetime) -> bool:
    hour = _local_now(config, now).hour
    start, end = config.settings.quiet_from_hour, config.settings.quiet_to_hour
    return hour >= start or hour < end if start > end else start <= hour < end


def start_of_local_day(config: Config, now: dt.datetime) -> dt.datetime:
    return _local_now(config, now).replace(hour=0, minute=0, second=0, microsecond=0)


def collect(
    config: Config,
    state: State,
    fetchers: dict[str, Fetcher],
    articles: ArticleSource | None = None,
    now: dt.datetime | None = None,
    own_channel_reader: Callable[[str], list[FetchedItem]] | None = None,
) -> dict:
    now = now or now_utc()
    active_channels = {c.channel_id for c in config.active_channels}
    stats = {"sources_ok": 0, "sources_failed": 0, "new": 0, "stale": 0, "duplicate": 0, "stop_topic": 0, "alert": 0, "empty": 0, "capped": 0}

    for source in config.sources:
        if not source.active or source.type not in fetchers:
            continue
        if source.scope == "local" and source.channel_id not in active_channels:
            continue
        health = state.source(source.key)
        try:
            items, new_cursor = fetchers[source.type].fetch_new(source, health.get("cursor"))
        except Exception as exc:  # одне зламане джерело не має зупиняти решту
            health["fails"] = health.get("fails", 0) + 1
            health["last_error"] = f"{type(exc).__name__}: {exc}"[:200]
            stats["sources_failed"] += 1
            logger.warning("Джерело %s (%s): %s", source.name, source.handle_or_url, health["last_error"])
            continue
        health.update(cursor=new_cursor, fails=0, last_ok=iso(now))
        health.pop("last_error", None)
        stats["sources_ok"] += 1
        if items:
            health["last_item"] = iso(now)
        logger.info("Джерело %s (%s): %d нових", source.name, source.handle_or_url, len(items))

        if source.scope == "national" and len(items) > MAX_NATIONAL_PER_SOURCE:
            stats["capped"] += len(items) - MAX_NATIONAL_PER_SOURCE
            items = items[-MAX_NATIONAL_PER_SOURCE:]  # фетчери віддають від старішого до новішого

        enriched = 0
        for item in items:
            outcome = _consider(config, state, source, item, articles if enriched < MAX_ARTICLES_PER_SOURCE else None, now)
            if outcome.pop("enriched", False):
                enriched += 1
            stats[outcome["result"]] += 1

    _expire_pool(config, state, now)

    live_posts: dict[str, list[FetchedItem]] = {}
    if own_channel_reader is not None:
        for ch in config.active_channels:
            if not ch.telegram_chat_id.startswith("@"):
                continue
            try:
                live_posts[ch.channel_id] = own_channel_reader(ch.telegram_chat_id)
            except Exception as exc:
                logger.warning("Не вдалося прочитати власний канал %s: %s", ch.telegram_chat_id, exc)
    return build_brief(config, state, now, stats, live_posts)


def _consider(
    config: Config, state: State, source: SourceConfig, item: FetchedItem, articles: ArticleSource | None, now: dt.datetime
) -> dict:
    max_age = dt.timedelta(hours=config.settings.max_age_hours)
    if item.published_at is not None and now - _aware(item.published_at) > max_age:
        return {"result": "stale"}
    if is_alert_signal(item.text):
        return {"result": "alert"}

    text = _clean_text(item.text)
    published_at = item.published_at
    media = [{"type": m.type, "url": m.url, **({"duration_sec": m.duration_sec} if m.duration_sec else {})} for m in item.media]
    enriched = False

    if source.type in ("website", "web_scrape") and articles is not None:
        article = articles.fetch(item.url)
        enriched = True
        if article is not None:
            if len(article.text) >= 200:
                # Повний текст статті замінює RSS-анонс (той лише повторює її початок);
                # від вихідного тексту лишаємо перший рядок — заголовок.
                title = text.split("\n", 1)[0].strip()
                text = article.text if article.text.startswith(title) else f"{title}\n\n{article.text}"
            elif article.text and article.text not in text:
                text = f"{text}\n\n{article.text}".strip()
            published_at = published_at or article.published_at
            if not media and article.image_url:
                media = [{"type": "photo", "url": article.image_url}]
            if published_at is not None and now - _aware(published_at) > max_age:
                return {"result": "stale", "enriched": True}

    if len(text) < MIN_TEXT_CHARS:
        return {"result": "empty", "enriched": enriched}  # лише фото без підпису — писати нема про що
    stop = find_stop_topic(text)
    if stop:
        logger.info("Стоп-тема (%s) у %s — пропускаю", stop, item.url)
        return {"result": "stop_topic", "enriched": enriched}

    # Дедуп за точним збігом — за початком тексту (заголовок + перші абзаци):
    # передруки між сайтами зазвичай відрізняються лише «хвостом».
    text_hash = content_hash(text[:400])
    if text_hash in state.seen:
        return {"result": "duplicate", "enriched": enriched}
    state.seen[text_hash] = iso(now)

    cid = candidate_id(source, item.external_id)
    state.pool[cid] = {
        "id": cid,
        "scope": source.scope,
        "channel_id": source.channel_id,
        "source": source.name,
        "url": item.url,
        "published_at": iso(_aware(published_at)) if published_at else None,
        "added_at": iso(now),
        "text": text[:MAX_CANDIDATE_CHARS],
        "media": _clean_media(state, cid, media),
    }
    return {"result": "new", "enriched": enriched}


_TAG_RE = re.compile(r"<[^>]{1,200}>")
_WP_FOOTER_RE = re.compile(
    r"(?:\[…\]|\[&#8230;\]|\[\.\.\.\])?\s*(?:The post .{0,300}? first appeared on|Запис .{0,300}? спершу з.явиться на) .{0,100}?\.?\s*$",
    re.DOTALL,
)


def _clean_text(text: str) -> str:
    """RSS-анонси WordPress часто тягнуть залишки розмітки й службовий хвіст
    «The post … first appeared on …» — редактору це не потрібно."""
    text = _TAG_RE.sub(" ", text or "")
    text = _WP_FOOTER_RE.sub("", text.strip())
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _clean_media(state: State, cid: str, media: list[dict]) -> list[dict]:
    """Прибирає логотипи/заглушки: за назвою файлу або якщо та сама картинка
    вже траплялась в ІНШІЙ новині (фото конкретної новини не повторюється)."""
    kept = []
    for m in media:
        url = m["url"]
        if m["type"] == "photo" and looks_like_logo(url):
            continue
        users = state.media_seen.setdefault(url, [])
        if cid not in users:
            users.append(cid)
        if len(users) > 1:
            # Картинка-заглушка: прибираємо її і з кандидатів, які взяли її раніше.
            for other in users:
                if other in state.pool and other != cid:
                    state.pool[other]["media"] = [x for x in state.pool[other]["media"] if x["url"] != url]
            continue
        kept.append(m)
    return kept


def _aware(ts: dt.datetime) -> dt.datetime:
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def _expire_pool(config: Config, state: State, now: dt.datetime) -> None:
    ttl = dt.timedelta(hours=config.settings.pool_ttl_hours)
    max_age = dt.timedelta(hours=config.settings.max_age_hours)
    for cid, cand in list(state.pool.items()):
        published = cand.get("published_at")
        too_old = published is not None and now - parse_iso(published) > max_age
        # Уночі прогонів немає, тож ранковий має побачити все, що назбиралось за ніч:
        # відлік очікування в пулі йде лише від моменту, коли кандидата вперше показали.
        waited = now - parse_iso(cand["added_at"]) > ttl
        if too_old or waited:
            del state.pool[cid]


def rubrics_due(config: Config, state: State, channel: ChannelConfig, now: dt.datetime) -> list[str]:
    local = _local_now(config, now)
    day_start = start_of_local_day(config, now)
    today = state.posts_since(channel.channel_id, day_start)
    kinds = {p.get("kind") for p in today}
    due = []
    if 7 <= local.hour < 10 and "morning" not in kinds:
        due.append("morning")
    if 20 <= local.hour < 23 and "evening" not in kinds and len([p for p in today if p.get("kind") in ("local", "oblast", "national")]) >= 3:
        due.append("evening")
    if local.weekday() == 4 and 15 <= local.hour < 20:
        week_ago = now - dt.timedelta(days=3)
        if not state.posts_since(channel.channel_id, week_ago, kind="afisha"):
            due.append("afisha")
    return due


def address_hint(config: Config, state: State, channel: ChannelConfig) -> str | None:
    """Звертання до підписників для наступного посту каналу або None, якщо ще не час.

    Рахуємо від кількості вже опублікованих постів каналу: перший пост і далі
    кожен N-й отримують звертання, а саме слово береться зі списку по колу."""
    settings = config.settings
    if not settings.addresses or settings.address_every <= 0:
        return None
    count = len([p for p in state.published if p["channel_id"] == channel.channel_id and p.get("kind") != "alert"])
    if count % settings.address_every != 0:
        return None  # звертання отримують 1-й, (N+1)-й, (2N+1)-й… пости каналу
    turn = count // settings.address_every
    # Зсув за каналом — щоб сім каналів не зверталися однаково в один день.
    offset = sum(ord(ch) for ch in channel.channel_id)
    return settings.addresses[(turn + offset) % len(settings.addresses)]


def build_brief(
    config: Config,
    state: State,
    now: dt.datetime,
    stats: dict | None = None,
    live_posts: dict[str, list[FetchedItem]] | None = None,
) -> dict:
    """Документ для редактора. Без побічних ефектів — можна викликати повторно.

    live_posts — останні повідомлення самих каналів мережі (channel_id -> items),
    прочитані з t.me/s. Вони точніші за наш журнал: у каналі можуть бути й пости,
    опубліковані вручну, а редактор має бачити все, щоб не повторюватись."""
    live_posts = live_posts or {}
    local = _local_now(config, now)
    day_start = start_of_local_day(config, now)
    recent_from = now - dt.timedelta(hours=RECENT_POSTS_HOURS)
    channels = []
    for ch in config.active_channels:
        today = [p for p in state.posts_since(ch.channel_id, day_start) if p.get("kind") != "alert"]
        recent = state.posts_since(ch.channel_id, recent_from)
        channels.append(
            {
                "channel_id": ch.channel_id,
                "name": ch.name,
                "posts_today": len(today),
                "remaining_today": max(0, ch.daily_limit - len(today)),
                "max_posts_this_run": ch.max_posts_per_run,
                "national_remaining_today": max(
                    0, config.settings.national_daily_limit - len([p for p in today if p.get("kind") == "national"])
                ),
                "rubrics_due": rubrics_due(config, state, ch, now),
                "address_hint": address_hint(config, state, ch),
                "recent_posts": (
                    [
                        {
                            "at": _aware(i.published_at).astimezone(local.tzinfo).strftime("%d.%m %H:%M") if i.published_at else None,
                            "text": i.text[:500],
                        }
                        for i in live_posts[ch.channel_id]
                        if i.text
                        and not is_alert_signal(i.text)
                        and (i.published_at is None or _aware(i.published_at) >= recent_from)
                    ]
                    if ch.channel_id in live_posts
                    else [
                        {
                            "at": parse_iso(p["published_at"]).astimezone(local.tzinfo).strftime("%d.%m %H:%M"),
                            "kind": p.get("kind"),
                            "text": p["text"][:500],
                        }
                        for p in recent
                        if p.get("kind") != "alert"
                    ]
                )[-RECENT_POSTS_LIMIT:],
            }
        )

    def _age_min(c: dict) -> int | None:
        return int((now - parse_iso(c["published_at"])).total_seconds() // 60) if c.get("published_at") else None

    candidates = [
        {**{k: v for k, v in c.items() if k != "added_at"}, "age_minutes": _age_min(c)}
        for c in sorted(state.pool.values(), key=lambda c: c.get("published_at") or c["added_at"], reverse=True)
    ]
    return {
        "generated_at": iso(now),
        "local_time": local.strftime("%Y-%m-%d %H:%M %A"),
        "quiet_hours_now": is_quiet(config, now),
        "stats": stats or {},
        "problems": source_problems(config, state, now),
        "channels": channels,
        "candidates": candidates,
    }


def source_problems(config: Config, state: State, now: dt.datetime, silent_days: float = 3) -> list[str]:
    problems = []
    for source in config.sources:
        if not source.active or source.type == "facebook_page":
            continue
        health = state.sources.get(source.key)
        if not health:
            continue
        if health.get("fails", 0) >= 3:
            problems.append(f"{source.name} ({source.handle_or_url}): {health['fails']} збоїв поспіль — {health.get('last_error', '')}")
        elif health.get("last_item") and now - parse_iso(health["last_item"]) > dt.timedelta(days=silent_days):
            days = (now - parse_iso(health["last_item"])).days
            problems.append(f"{source.name} ({source.handle_or_url}): {days} дн. без нових матеріалів")
    return problems
