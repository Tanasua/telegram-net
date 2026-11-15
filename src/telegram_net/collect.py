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

from telegram_net.clock import is_quiet, local_now, start_of_local_day
from telegram_net.config import ChannelConfig, Config, SourceConfig
from telegram_net.dedup import content_hash
from telegram_net.fetchers.article import Article, looks_like_logo
from telegram_net.fetchers.base import FetchedItem, Fetcher
from telegram_net.state import SERVICE_KINDS, State, iso, now_utc, parse_iso
from telegram_net.stoplist import find_stop_topic

logger = logging.getLogger(__name__)

MAX_CANDIDATE_CHARS = 2500
MAX_NATIONAL_PER_SOURCE = 4
MAX_OFFICIAL_PER_SOURCE = 3  # пресслужби пишуть багато й нудно — лише найсвіжіше
KIND_PRIORITY = {"public": 0, "media": 1, "official": 2}
KIND_LABEL = {"public": "паблік", "media": "медіа", "official": "офіційне"}  # всеукраїнські стрічки дуже щільні — беремо лише найсвіжіше
MAX_ARTICLES_PER_SOURCE = 8
MIN_TEXT_CHARS = 20
RECENT_POSTS_HOURS = 36
RECENT_POSTS_LIMIT = 30

# Сигнали тривоги публікує окремий процес (alerts.py) за шаблоном і за хвилину,
# а не редактор раз на годину — тому в кандидати вони не потрапляють.
_ALERT_RE = re.compile(r"^\W*(?:[\w,\s]+ район\w*|[\w\s]+ област\w*)?\s*[—-]?\s*(?:повітрян\w+ тривог|відбій)", re.IGNORECASE)
_ALERT_ANY_RE = re.compile(r"повітрян\w+ тривог\w*|відбій (?:повітрян\w+ )?тривог\w*", re.IGNORECASE)


# Шум, який редактор однаково відхиляє, — відсіюємо кодом, щоб модель не
# витрачала на нього токени: моніторинг повітряних цілей, календарі свят,
# привітання, зведення втрат, гороскопи.
_NOISE_RE = re.compile(
    r"повітряна загроза станом на|бпла\W{0,3}(?:курс|в напрямку|у напрямку|на )|реактивн\w+ (?:бпла|на )|"
    r"дорозвідка|шахед\w* (?:курс|в напрямку|на )|"
    r"бойові втрати противника|втрати противника|ще один день до перемоги|"
    r"доброго ранку|добрий ранок|на добраніч|гороскоп|схід сонця|день в історії|цей день в історії|"
    r"яке сьогодні свято|свята сьогодні|курс валют",
    re.IGNORECASE,
)
_NOISE_MAX_CHARS = 700  # довгий текст із таким словом — уже новина, а не зведення
MIN_SOCIAL_CHARS = 80  # коротший пост із телеграм-каналу — репліка, писати з неї нема чого

# Службові хвости джерел: «Підписатися | Надіслати новину», «Читати повністю»…
_FOOTER_LINE_RE = re.compile(
    r"підпи(?:сатис|шись|шіться|суйся|суйтесь)|надіслати (?:новину|контент)|читати (?:повністю|далі)|деталі на сайті|подробиці тут|"
    r"facebook\s*\||\|\s*youtube|карта загроз|джерело: за даними|телеграм канал|дивитись відео|^деталі\.?$",
    re.IGNORECASE,
)


# Пресслужбова «нудятина»: нагадування, заклики, наради, привітання, звіти,
# профілактика, донорство, загальні поради з безпеки. Відсікається лише в
# офіційних джерелах і лише якщо в тексті немає справжньої події.
_PR_RE = re.compile(
    r"нагаду\w*|заклика\w*|просимо\s+бути|будьте\s+(?:обережн|пильн|уважн)|профілактичн|роз.яснювальн|"
    r"робоч\w*\s+(?:зустріч|нарад|поїздк)|нарад[аиі]|семінар|засідан|тренінг|навчанн|"
    r"вітаємо|вітає|привітал|урочист|нагородил|нагороджен|подяк|відзначил|"
    r"донор\w*|звітує|звіт\s+про|підсумки\s+роботи|за\s+тиждень|упродовж\s+тижня|"
    r"правил\w*\s+безпеки|пам.ятк|не\s+чіпайте|підозріл\w*\s+(?:знахідк|предмет)|вибухонебезпечн|"
    r"телефонуйте\s+10[12]|акці\w*\s+[«\"]|відкрит\w*\s+урок|мобільн\w*\s+клас|зустрі\w+\s+з\s+(?:учн|школяр|студент)",
    re.IGNORECASE,
)
_INCIDENT_RE = re.compile(
    r"загину|загибл|постраждал|травм|госпіталіз|дтп|зіткнен|пожеж|загорі|затрима|викра|крадіжк|пограбу|"
    r"зник|розшук|вибух(?!онебезпечн)|аварі|відключен|без\s+(?:світла|води|газу|тепла)|перекрит|обмеженн\w*\s+руху|"
    r"влучанн|пошкоджен|обстріл|атак|ремонт\s+(?:дорог|мост)|повідомлено\s+про\s+підозр",
    re.IGNORECASE,
)


def is_press_release(text: str) -> bool:
    return bool(_PR_RE.search(text)) and not _INCIDENT_RE.search(text)


def is_noise(text: str) -> bool:
    return len(text) <= _NOISE_MAX_CHARS and bool(_NOISE_RE.search(text))


def strip_footer(text: str) -> str:
    lines = text.rstrip().split("\n")
    while len(lines) > 1 and (not lines[-1].strip() or (len(lines[-1]) < 120 and _FOOTER_LINE_RE.search(lines[-1]))):
        lines.pop()
    return "\n".join(lines).strip()


class ArticleSource(Protocol):
    def fetch(self, url: str) -> Article | None: ...


def is_alert_signal(text: str) -> bool:
    head = text.strip()[:200]
    return bool(_ALERT_RE.search(head)) or (len(text) < 300 and bool(_ALERT_ANY_RE.search(text)))


def candidate_id(source: SourceConfig, external_id: str) -> str:
    return hashlib.sha256(f"{source.key}|{external_id}".encode("utf-8")).hexdigest()[:8]


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
    stats = {"sources_ok": 0, "sources_failed": 0, "new": 0, "stale": 0, "duplicate": 0, "stop_topic": 0, "alert": 0, "noise": 0, "press_release": 0, "empty": 0, "capped": 0}

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

        cap = MAX_NATIONAL_PER_SOURCE if source.scope == "national" else MAX_OFFICIAL_PER_SOURCE if source.kind == "official" else None
        if cap is not None and len(items) > cap:
            stats["capped"] += len(items) - cap
            items = items[-cap:]  # фетчери віддають від старішого до новішого

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

    text = strip_footer(_clean_text(item.text))
    if is_noise(text):
        return {"result": "noise"}
    if source.kind == "official" and is_press_release(text):
        return {"result": "press_release"}
    if source.type == "telegram" and len(text) < MIN_SOCIAL_CHARS:
        return {"result": "empty"}
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
        "kind": source.kind,
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
    local = local_now(config, now)
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


def national_due(config: Config, state: State, channel: ChannelConfig, now: dt.datetime) -> int:
    """Скільки всеукраїнських новин каналу вже «винні» на цю мить.

    Кожна година з `national_slot_hours`, що настала, додає одну; віднімаємо вже
    опубліковані за добу. Так «Україна» не залежить від настрою редактора й не
    витісняється місцевим."""
    settings = config.settings
    local = local_now(config, now)
    slots = len([h for h in settings.national_slot_hours if local.hour >= h and h < settings.quiet_from_hour])
    day_start = start_of_local_day(config, now)
    done = len([p for p in state.posts_since(channel.channel_id, day_start) if p.get("kind") == "national"])
    return max(0, min(slots, settings.national_daily_limit) - done)


def address_hint(config: Config, state: State, channel: ChannelConfig) -> str | None:
    """Звертання до підписників для наступного посту каналу або None, якщо ще не час.

    Рахуємо від кількості вже опублікованих постів каналу: перший пост і далі
    кожен N-й отримують звертання, а саме слово береться зі списку по колу."""
    settings = config.settings
    if not settings.addresses or settings.address_every <= 0:
        return None
    count = state.channel_post_count(channel.channel_id)
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
    local = local_now(config, now)
    day_start = start_of_local_day(config, now)
    recent_from = now - dt.timedelta(hours=RECENT_POSTS_HOURS)
    channels = []
    for ch in config.active_channels:
        today = [p for p in state.posts_since(ch.channel_id, day_start) if p.get("kind") not in SERVICE_KINDS]
        recent = state.posts_since(ch.channel_id, recent_from)
        channels.append(
            {
                "channel_id": ch.channel_id,
                "name": ch.name,
                "posts_today": len(today),
                "remaining_today": max(0, ch.daily_limit - len([p for p in today if p.get("kind") != "national"])),
                "max_posts_this_run": ch.max_posts_per_run,
                "national_remaining_today": max(
                    0, config.settings.national_daily_limit - len([p for p in today if p.get("kind") == "national"])
                ),
                "national_due": national_due(config, state, ch, now),
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
                        and not i.text.startswith("Погода у ")
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
                        if p.get("kind") not in SERVICE_KINDS and p.get("published_at")
                    ]
                )[-RECENT_POSTS_LIMIT:]
                # Заплановане ще не видно в самому каналі, але повторювати його не можна.
                + [
                    {
                        "at": parse_iso(q["due_at"]).astimezone(local.tzinfo).strftime("%d.%m %H:%M"),
                        "kind": q.get("kind"),
                        "text": q["text"][:500],
                        "scheduled": True,
                    }
                    for q in state.queue
                    if q["channel_id"] == ch.channel_id
                ],
            }
        )

    def _age_min(c: dict) -> int | None:
        return int((now - parse_iso(c["published_at"])).total_seconds() // 60) if c.get("published_at") else None

    candidates = [
        {**{k: v for k, v in c.items() if k != "added_at"}, "age_minutes": _age_min(c)}
        for c in sorted(
            sorted(state.pool.values(), key=lambda c: c.get("published_at") or c["added_at"], reverse=True),
            key=lambda c: KIND_PRIORITY.get(c.get("kind", "media"), 1),
        )
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


BRIEF_TEXT_CHARS = 450
BRIEF_RECENT_CHARS = 110
BRIEF_RECENT_POSTS = 8


def brief_text(brief: dict) -> str:
    """Той самий бриф, але компактним текстом — саме його читає редактор.

    JSON з відступами, посиланнями на медіа й повними текстами коштував
    30–50 тис. токенів на прогін. Тут — лише те, що потрібно для рішення:
    початок тексту кандидата (повний — командою `show <id>`), кількість медіа
    замість посилань, короткі згадки про вже опубліковане."""
    lines = [f"ЧАС: {brief['local_time']} · нічна тиша: {'ТАК' if brief['quiet_hours_now'] else 'ні'}"]
    if brief.get("problems"):
        lines.append("ПРОБЛЕМИ ДЖЕРЕЛ: " + "; ".join(brief["problems"]))
    lines.append("")
    lines.append("КАНАЛИ")
    for ch in brief["channels"]:
        extras = []
        if ch["rubrics_due"]:
            extras.append("рубрики: " + ", ".join(ch["rubrics_due"]))
        if ch.get("national_due"):
            extras.append(f"ВСЕУКР. ОБОВ'ЯЗКОВО: {ch['national_due']} (kind=national, поза лімітом прогону)")
        if ch.get("address_hint"):
            extras.append(f"звертання: {ch['address_hint']}")
        lines.append(
            f"{ch['channel_id']} «{ch['name']}» · лишилось сьогодні {ch['remaining_today']}, за прогін {ch['max_posts_this_run']}, "
            f"всеукр. {ch['national_remaining_today']}" + (" · " + " · ".join(extras) if extras else "")
        )
        published = [p for p in ch["recent_posts"] if not p.get("scheduled")][-BRIEF_RECENT_POSTS:]
        scheduled = [p for p in ch["recent_posts"] if p.get("scheduled")]
        for post in published + scheduled:
            head = " ".join(post["text"].split())[:BRIEF_RECENT_CHARS]
            label = "заплановано" if post.get("scheduled") else "вже було"
            lines.append(f"  {label} {post.get('at') or ''}: {head}")
    lines.append("")
    lines.append(f"КАНДИДАТИ ({len(brief['candidates'])})")
    for c in brief["candidates"]:
        target = c["channel_id"] or "мережа"
        media = c.get("media", [])
        photos = len([m for m in media if m["type"] == "photo"])
        videos = len(media) - photos
        media_note = ", ".join(x for x in (f"фото {photos}" if photos else "", f"відео {videos}" if videos else "") if x) or "без медіа"
        age = f"{c['age_minutes']} хв" if c.get("age_minutes") is not None else "час невідомий"
        text = c["text"]
        tail = ""
        if len(text) > BRIEF_TEXT_CHARS:
            tail = f" …(+{len(text) - BRIEF_TEXT_CHARS} симв., повний текст: show {c['id']})"
            text = text[:BRIEF_TEXT_CHARS]
        lines.append("")
        kind = KIND_LABEL.get(c.get("kind", "media"), "медіа")
        lines.append(f"[{c['id']}] {c['scope']}→{target} · {kind} · {c['source']} · {age} · {media_note}")
        lines.append(text + tail)
    return "\n".join(lines) + "\n"


def nothing_to_do(brief: dict) -> bool:
    """Немає ні кандидатів, ні рубрик — редактору нема чого робити, прогін можна завершити."""
    return not brief["candidates"] and not any(ch["rubrics_due"] for ch in brief["channels"])  # без кандидатів «Україна» теж нічого випускати
