import datetime as dt

from telegram_net.config import ChannelConfig, Config, Settings, SourceConfig
from telegram_net.fetchers.base import FetchedItem, FetchedMedia

# Субота 14.11.2026, 15:00 за Києвом (13:00 UTC) — день, не тиха година.
NOW = dt.datetime(2026, 11, 14, 13, 0, tzinfo=dt.timezone.utc)


def make_config(sources=(), **settings):
    channels = [
        ChannelConfig("malyn", "Малин", "@mymalyn", alert_district="Коростенський", subscribe_cta="Підписатися на «Мій Малин»", name_genitive="Малина"),
        ChannelConfig("zhytomyr", "Житомир", "@myzhytomyr", alert_district="Житомирський", subscribe_cta="Підписатися на «Мій Житомир»", name_genitive="Житомира"),
        ChannelConfig("off", "Вимкнений", None),
    ]
    return Config(settings=Settings(**settings), channels=channels, sources=list(sources))


def src(name="Джерело", channel_id="malyn", scope="local", type="telegram", handle=None, kind="media"):
    return SourceConfig(
        channel_id=channel_id if scope == "local" else None,
        type=type,
        name=name,
        handle_or_url=handle or f"@{name}",
        scope=scope,
        kind=kind,
    )


def item(ext_id, text, minutes_ago=30, media=(), url=None, dated=True):
    return FetchedItem(
        external_id=str(ext_id),
        text=text,
        url=url or f"https://t.me/x/{ext_id}",
        published_at=NOW - dt.timedelta(minutes=minutes_ago) if dated else None,
        media=tuple(FetchedMedia(type="photo", url=m) for m in media),
    )


class FakeFetcher:
    """Віддає заготовлені елементи за ключем джерела й рахує виклики."""

    def __init__(self, by_handle):
        self.by_handle = by_handle
        self.calls = []

    def fetch_new(self, source, cursor):
        self.calls.append((source.handle_or_url, cursor))
        result = self.by_handle.get(source.handle_or_url, [])
        if isinstance(result, Exception):
            raise result
        return list(result), "next"
