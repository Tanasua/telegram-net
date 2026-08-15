import datetime as dt

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from telegram_net.classifier import ClassificationResult
from telegram_net.config import ChannelConfig, SourceConfig
from telegram_net.db import GeneratedPost, RawItem, init_db, make_session_factory
from telegram_net.fetchers.base import FetchedItem
from telegram_net.pipeline import PipelineDeps, run_once


class FakeConfigSource:
    def __init__(self, channels, sources):
        self._channels = channels
        self._sources = sources

    def load_channels(self):
        return self._channels

    def load_sources(self):
        return self._sources


class FakeFetcher:
    def __init__(self, items):
        self._items = items

    def fetch_new(self, source, cursor):
        items, self._items = self._items, []  # повертаємо тільки раз, як реальний cursor-фетч
        return items, "done"


class FakeClassifier:
    def __init__(self, result):
        self.result = result
        self.model = "fake-classifier"

    def classify(self, item, region_name):
        return self.result


class FakeGenerator:
    def __init__(self, text="Готовий пост"):
        self.text = text

    def generate(self, item, style, region_name):
        return self.text


class FakePublisher:
    def __init__(self):
        self.published = []

    def publish(self, chat_id, text, media_batches):
        self.published.append((chat_id, text, media_batches))
        return [111]


def _make_deps(items, classification, channel_kwargs=None):
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    init_db(engine)
    session_factory = make_session_factory(engine)

    defaults = {"telegram_chat_id": "-1001"}
    defaults.update(channel_kwargs or {})
    channel = ChannelConfig(channel_id="zhytomyr", name="Житомир", **defaults)
    source = SourceConfig(channel_id="zhytomyr", type="telegram", name="test", handle_or_url="@test")

    publisher = FakePublisher()
    deps = PipelineDeps(
        config=FakeConfigSource([channel], [source]),
        session_factory=session_factory,
        fetchers={"telegram": FakeFetcher(items)},
        classifier=FakeClassifier(classification),
        generator=FakeGenerator(),
        publisher=publisher,
    )
    return deps, session_factory, publisher


def _item(external_id="1", text="Новина про дорогу", published_at=None):
    return FetchedItem(
        external_id=external_id, text=text, url=f"https://example.com/{external_id}", published_at=published_at
    )


_RELEVANT = ClassificationResult(
    is_advertisement=False, relevance_to_region=0.9, local_news_interest=0.9, reason="ok"
)
_AD = ClassificationResult(
    is_advertisement=True, relevance_to_region=0.9, local_news_interest=0.1, reason="реклама"
)
_IRRELEVANT = ClassificationResult(
    is_advertisement=False, relevance_to_region=0.1, local_news_interest=0.5, reason="не про це місто"
)
_BORING_INSTITUTIONAL_FILLER = ClassificationResult(
    is_advertisement=False,
    relevance_to_region=0.9,  # формально про це місто...
    local_news_interest=0.05,  # ...але не подія, а опис організації
    reason="опис сайту громади, не новина",
)


def test_relevant_item_is_classified_generated_and_published():
    deps, session_factory, publisher = _make_deps([_item()], _RELEVANT)

    run_once(deps)

    with session_factory() as session:
        raw = session.query(RawItem).one()
        assert raw.classification.is_advertisement is False
        post = session.query(GeneratedPost).one()
        assert post.status == "published"
        assert post.text == "Готовий пост"
    assert publisher.published == [("-1001", "Готовий пост", [])]


def test_advertisement_is_classified_but_not_generated_or_published():
    deps, session_factory, publisher = _make_deps([_item()], _AD)

    run_once(deps)

    with session_factory() as session:
        assert session.query(RawItem).count() == 1
        assert session.query(GeneratedPost).count() == 0
    assert publisher.published == []


def test_below_min_relevance_is_not_published():
    deps, session_factory, publisher = _make_deps(
        [_item()], _IRRELEVANT, channel_kwargs={"min_relevance": 0.5}
    )

    run_once(deps)

    with session_factory() as session:
        assert session.query(GeneratedPost).count() == 0
    assert publisher.published == []


def test_below_min_interest_is_not_published():
    """Регресія: класифікатор оцінював local_news_interest, але pipeline ніде
    не звіряв цю оцінку з порогом — 'нецікаве' формально-релевантне сміття
    (опис сайту громади) все одно публікувалось."""
    deps, session_factory, publisher = _make_deps(
        [_item()], _BORING_INSTITUTIONAL_FILLER, channel_kwargs={"min_interest": 0.5}
    )

    run_once(deps)

    with session_factory() as session:
        assert session.query(RawItem).count() == 1  # зафіксовано, але не опубліковано
        assert session.query(GeneratedPost).count() == 0
    assert publisher.published == []


def test_duplicate_content_is_processed_only_once():
    item1 = _item(external_id="1", text="Однакова новина про дорогу")
    item2 = _item(external_id="2", text="Однакова новина про дорогу")
    deps, session_factory, publisher = _make_deps([item1, item2], _RELEVANT)

    run_once(deps)

    with session_factory() as session:
        assert session.query(RawItem).count() == 1
    assert len(publisher.published) == 1


def test_channel_without_telegram_chat_id_keeps_draft_and_skips_publish():
    deps, session_factory, publisher = _make_deps([_item()], _RELEVANT, channel_kwargs={"telegram_chat_id": None})

    run_once(deps)

    with session_factory() as session:
        post = session.query(GeneratedPost).one()
        assert post.status == "draft"
    assert publisher.published == []


def test_inactive_channel_is_skipped_entirely():
    deps, session_factory, publisher = _make_deps([_item()], _RELEVANT, channel_kwargs={"active": False})

    run_once(deps)

    with session_factory() as session:
        assert session.query(RawItem).count() == 0
    assert publisher.published == []


def test_max_posts_per_run_caps_publishing_even_with_several_relevant_items():
    """Регресія: один активний канал не повинен вивалювати десятки постів за
    раз, поки решта каналів чекають своєї черги — ліміт за замовчуванням 1."""
    items = [
        _item(external_id="1", text="Перша новина"),
        _item(external_id="2", text="Друга новина"),
        _item(external_id="3", text="Третя новина"),
    ]
    deps, session_factory, publisher = _make_deps(items, _RELEVANT, channel_kwargs={"max_posts_per_run": 1})

    run_once(deps)

    with session_factory() as session:
        assert session.query(RawItem).count() == 3  # усі три зафіксовані...
        assert session.query(GeneratedPost).filter_by(status="published").count() == 1  # ...але опубліковано 1
    assert len(publisher.published) == 1


def test_max_posts_per_run_higher_limit_allows_more():
    items = [
        _item(external_id="1", text="Перша новина"),
        _item(external_id="2", text="Друга новина"),
    ]
    deps, session_factory, publisher = _make_deps(items, _RELEVANT, channel_kwargs={"max_posts_per_run": 2})

    run_once(deps)

    assert len(publisher.published) == 2


def test_stale_item_is_not_published_or_recorded():
    """Новина старша за max_age_minutes (дефолт 60) не публікується взагалі —
    свіжість перевіряється до дедупу/класифікації, щоб не витрачати виклики."""
    old_item = _item(published_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3))
    deps, session_factory, publisher = _make_deps([old_item], _RELEVANT)

    run_once(deps)

    with session_factory() as session:
        assert session.query(RawItem).count() == 0
    assert publisher.published == []


def test_fresh_item_within_window_is_published_normally():
    fresh_item = _item(published_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10))
    deps, session_factory, publisher = _make_deps([fresh_item], _RELEVANT)

    run_once(deps)

    assert len(publisher.published) == 1


def test_custom_max_age_minutes_is_respected():
    item_30_min_old = _item(published_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=30))
    deps, session_factory, publisher = _make_deps(
        [item_30_min_old], _RELEVANT, channel_kwargs={"max_age_minutes": 15}
    )

    run_once(deps)

    assert publisher.published == []


def test_subscribe_cta_appended_after_publish_when_configured():
    deps, session_factory, publisher = _make_deps(
        [_item()], _RELEVANT, channel_kwargs={"subscribe_cta": "Підписуйся на Мій Житомир"}
    )

    run_once(deps)

    assert publisher.published == [("-1001", "Готовий пост\n\nПідписуйся на Мій Житомир", [])]
    with session_factory() as session:
        post = session.query(GeneratedPost).one()
        assert post.text == "Готовий пост\n\nПідписуйся на Мій Житомир"


def test_no_subscribe_cta_appended_when_not_configured():
    deps, session_factory, publisher = _make_deps([_item()], _RELEVANT)

    run_once(deps)

    assert publisher.published == [("-1001", "Готовий пост", [])]
