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


def _item(external_id="1", text="Новина про дорогу"):
    return FetchedItem(external_id=external_id, text=text, url=f"https://example.com/{external_id}", published_at=None)


_RELEVANT = ClassificationResult(
    is_advertisement=False, relevance_to_region=0.9, local_news_interest=0.9, reason="ok"
)
_AD = ClassificationResult(
    is_advertisement=True, relevance_to_region=0.9, local_news_interest=0.1, reason="реклама"
)
_IRRELEVANT = ClassificationResult(
    is_advertisement=False, relevance_to_region=0.1, local_news_interest=0.5, reason="не про це місто"
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
