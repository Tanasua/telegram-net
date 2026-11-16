"""«Годинник» мережі: запускає щоденні workflow у потрібний час.

Розклад GitHub Actions (cron) на практиці ненадійний: запуски запізнюються на
години або зникають зовсім (15–16.11: черга постів — 3 запуски за ніч замість
щопівгодини, погода — жодного). Тому час відлічує цикл сигналів тривоги, який і
так працює безперервно: щохвилини він перевіряє цей розклад і запускає потрібні
workflow через API (workflow_dispatch з GITHUB_TOKEN — GitHub дозволяє).

Самі workflow ідемпотентні: публікують раз на добу й лише у своєму вікні, тож
повторний запуск у межах вікна нічого не дублює — лише страхує від збою.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Callable

import httpx

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Job:
    workflow: str  # файл у .github/workflows
    start: dt.time  # вікно за Києвом
    end: dt.time
    every_min: int  # як часто запускати всередині вікна


def jobs_for(config) -> list[Job]:
    s = config.settings
    wh, mh = s.weather_hour, getattr(s, "markets_hour", 19)
    return [
        # Черга постів: увесь день, плюс одразу після нічної тиші.
        Job("drain.yml", dt.time(s.quiet_to_hour, 0), dt.time(23, 30), 15),
        Job("weather.yml", dt.time(wh, 0), dt.time(min(wh + 2, 23), 0), 20),
        # «Нічна атака» вимкнена (рішення власника 16.11.2026: нудно); код лишився для ручного запуску.
        Job("markets.yml", dt.time(mh, 0), dt.time(min(mh + 2, 23), 0), 20),
    ]


def due_jobs(jobs: list[Job], local_now: dt.datetime, last_run: dict[str, str]) -> list[Job]:
    """Які workflow запустити зараз: у своєму вікні й не раніше ніж через every_min після минулого запуску."""
    due = []
    for job in jobs:
        if not (job.start <= local_now.time() < job.end):
            continue
        last = last_run.get(job.workflow)
        if last:
            last_dt = dt.datetime.fromisoformat(last)
            if local_now - last_dt < dt.timedelta(minutes=job.every_min):
                continue
        due.append(job)
    return due


def github_dispatcher() -> Callable[[str], None] | None:
    """Запуск workflow через GitHub API або None, якщо ми не в GitHub Actions."""
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("TICKER_REF") or os.environ.get("GITHUB_REF_NAME")
    if not (token and repo and ref):
        return None
    http = httpx.Client(timeout=15, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})

    def dispatch(workflow: str) -> None:
        resp = http.post(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches", json={"ref": ref})
        if resp.status_code >= 300:
            raise RuntimeError(f"{workflow}: {resp.status_code} {resp.text[:200]}")

    return dispatch


def code_update_checker(
    every_sec: float = 300,
    clock: Callable[[], float] | None = None,
    http: httpx.Client | None = None,
) -> Callable[[], bool] | None:
    """True, коли на гілці в GitHub змінився код (`src/`) порівняно з тим, з яким
    стартував цей запуск (CODE_TREE = `git rev-parse HEAD:src`). Без цього правка
    коду (напр. підпису карти NEPTUN) доходила в канали лише за кілька годин.
    Дивимось саме на дерево `src/`: бот комітить стан і чергу кожні ~15 хв, і
    перезапускатися через кожен такий коміт не треба."""
    import time

    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("TICKER_REF") or os.environ.get("GITHUB_REF_NAME")
    tree = os.environ.get("CODE_TREE")
    if not (token and repo and ref and tree):
        return None
    clock = clock or time.monotonic
    http = http or httpx.Client(timeout=15, headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    last = {"at": clock()}

    def changed() -> bool:
        if clock() - last["at"] < every_sec:
            return False
        last["at"] = clock()
        resp = http.get(f"https://api.github.com/repos/{repo}/git/trees/{ref}")
        if resp.status_code != 200:
            return False
        remote = next((e["sha"] for e in resp.json().get("tree", []) if e.get("path") == "src"), None)
        if remote and remote != tree:
            logger.info("У %s новий код src/ (%s ≠ %s) — перезапуск із ним", ref, remote[:7], tree[:7])
            return True
        return False

    return changed


class Ticker:
    def __init__(self, config, dispatch: Callable[[str], None] | None, tz):
        self._jobs = jobs_for(config)
        self._dispatch = dispatch
        self._tz = tz
        self.last_run: dict[str, str] = {}

    def tick(self, now: dt.datetime | None = None) -> list[str]:
        if self._dispatch is None:
            return []
        local = (now or dt.datetime.now(dt.timezone.utc)).astimezone(self._tz)
        started = []
        for job in due_jobs(self._jobs, local, self.last_run):
            try:
                self._dispatch(job.workflow)
            except Exception as exc:
                logger.warning("Не вдалося запустити %s: %s", job.workflow, exc)
                continue
            self.last_run[job.workflow] = local.isoformat()
            started.append(job.workflow)
            logger.info("Годинник: запущено %s", job.workflow)
        return started
