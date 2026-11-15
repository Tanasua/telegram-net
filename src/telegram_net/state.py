"""Стан між прогонами — три невеликі JSON-файли в state/ (комітяться в репозиторій).

Раніше тут була SQLite-база: бінарний файл на 2,5 МБ, що переписувався цілком
щогодини й роздував історію git. JSON дає звичайні текстові дифи, і його можна
прочитати очима.

- state.json      — курсори джерел, їхнє «здоров'я», пул кандидатів, бачені хеші;
- published.json  — журнал опублікованого за останні 30 днів (для дедупу за
                    змістом, добових лімітів, рубрик і звітів);
- queue.json      — черга запланованих постів (publish.py): щоб пости одного
                    каналу не виходили підряд, вони розносяться в часі.

state.json пише лише збір і редактор; published.json і queue.json — лише крок
публікації в GitHub Actions. Тому редактор і черга не перетирають стан одне одного.

Сигнали тривоги (alerts.py) стану на диску не мають — відновлюють його з
останніх повідомлень джерела під час старту.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

SEEN_TTL_DAYS = 14
SERVICE_KINDS = ("alert", "weather", "attack")  # службові пости: не входять у ліміти, ротації й «вже було»
PUBLISHED_TTL_DAYS = 30


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> dt.datetime:
    ts = dt.datetime.fromisoformat(value)
    return ts if ts.tzinfo else ts.replace(tzinfo=dt.timezone.utc)


def post_time(entry: dict) -> dt.datetime:
    return parse_iso(entry.get("published_at") or entry["due_at"])


def _read(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")


class State:
    def __init__(self, directory: str | Path = "state"):
        self.dir = Path(directory)
        data = _read(self.dir / "state.json", {})
        self.sources: dict[str, dict] = data.get("sources", {})  # key -> {cursor, last_ok, last_item, fails, last_error}
        self.seen: dict[str, str] = data.get("seen", {})  # content_hash -> iso час
        self.media_seen: dict[str, list[str]] = data.get("media_seen", {})  # url картинки -> id новин, де траплялась
        self.pool: dict[str, dict] = data.get("pool", {})  # id кандидата -> кандидат
        self.runs: list[str] = data.get("runs", [])  # run_id уже оброблених файлів рішень
        self.published: list[dict] = _read(self.dir / "published.json", [])
        self.queue: list[dict] = _read(self.dir / "queue.json", [])

    # --- джерела ---
    def source(self, key: str) -> dict:
        return self.sources.setdefault(key, {"cursor": None, "fails": 0})

    # --- журнал публікацій ---
    def posts_since(self, channel_id: str, since: dt.datetime, kind: str | None = None) -> list[dict]:
        """Опубліковане й заплановане в каналі від `since` (заплановане — за часом виходу)."""
        return [
            p
            for p in self.published + self.queue
            if p["channel_id"] == channel_id
            and post_time(p) >= since
            and (kind is None or p.get("kind") == kind)
        ]

    def channel_post_count(self, channel_id: str) -> int:
        """Скільки новинних постів каналу вийшло або чекає в черзі (без сигналів тривоги)."""
        return len([p for p in self.published + self.queue if p["channel_id"] == channel_id and p.get("kind") not in SERVICE_KINDS])

    def log_post(self, entry: dict) -> None:
        self.published.append(entry)

    # --- збереження ---
    def prune(self, now: dt.datetime | None = None) -> None:
        now = now or now_utc()
        seen_cutoff = now - dt.timedelta(days=SEEN_TTL_DAYS)
        self.seen = {h: t for h, t in self.seen.items() if parse_iso(t) >= seen_cutoff}
        pub_cutoff = now - dt.timedelta(days=PUBLISHED_TTL_DAYS)
        self.published = [p for p in self.published if parse_iso(p["published_at"]) >= pub_cutoff]
        if len(self.media_seen) > 3000:
            self.media_seen = dict(list(self.media_seen.items())[-2000:])

    def save(self) -> None:
        self.prune()
        _write(
            self.dir / "state.json",
            {"sources": self.sources, "seen": self.seen, "media_seen": self.media_seen, "pool": self.pool, "runs": self.runs},
        )
        _write(self.dir / "published.json", self.published)
        _write(self.dir / "queue.json", self.queue)
