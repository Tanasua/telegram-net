import datetime as dt
from zoneinfo import ZoneInfo

from helpers import make_config

from telegram_net.ticker import Ticker, due_jobs, jobs_for

KYIV = ZoneInfo("Europe/Kyiv")


def at(h, m=0):
    return dt.datetime(2026, 11, 16, h, m, tzinfo=KYIV)


def test_windows_and_intervals():
    jobs = jobs_for(make_config())
    names = lambda t, last=None: [j.workflow for j in due_jobs(jobs, t, last or {})]  # noqa: E731
    assert names(at(3)) == []  # ніч — нічого
    assert names(at(7, 1)) == ["drain.yml"]
    assert names(at(8, 5)) == ["drain.yml", "weather.yml"]
    assert names(at(8, 40)) == ["drain.yml", "weather.yml"]  # «Нічна атака» вимкнена
    assert names(at(19, 2)) == ["drain.yml", "markets.yml"]
    assert names(at(23, 45)) == []
    # Нещодавно запущений — чекає свого інтервалу.
    assert names(at(8, 10), {"drain.yml": at(8, 0).isoformat(), "weather.yml": at(8, 5).isoformat()}) == []
    assert names(at(8, 16), {"drain.yml": at(8, 0).isoformat(), "weather.yml": at(8, 5).isoformat()}) == ["drain.yml"]


def test_ticker_dispatches_once_per_interval_and_survives_errors():
    calls = []

    def dispatch(wf):
        calls.append(wf)
        if wf == "weather.yml" and calls.count(wf) == 1:
            raise RuntimeError("422")

    t = Ticker(make_config(), dispatch, KYIV)
    assert t.tick(at(8, 1).astimezone(dt.timezone.utc)) == ["drain.yml"]  # погода впала — спробує наступної хвилини
    assert t.tick(at(8, 2).astimezone(dt.timezone.utc)) == ["weather.yml"]
    assert t.tick(at(8, 3).astimezone(dt.timezone.utc)) == []
    assert Ticker(make_config(), None, KYIV).tick(at(8, 1)) == []  # поза GitHub Actions — нічого
