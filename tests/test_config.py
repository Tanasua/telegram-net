import json

import pytest

from telegram_net.config import LocalJSONConfigSource


@pytest.fixture
def seed_path(tmp_path):
    data = {
        "channels": [
            {
                "channel_id": "zhytomyr",
                "name": "Житомир",
                "telegram_chat_id": "-1001234567890",
                "active": True,
                "min_relevance": 0.6,
                "length_weights": {"short": 0.5, "medium": 0.3, "long": 0.2},
                "headline_probability": 0.3,
            }
        ],
        "sources": [
            {
                "channel_id": "zhytomyr",
                "type": "telegram",
                "name": "pzhytomyr",
                "handle_or_url": "@pzhytomyr",
                "active": True,
            },
            {
                "channel_id": "zhytomyr",
                "type": "website",
                "name": "Житомир.info",
                "handle_or_url": "https://www.zhitomir.info",
                "active": False,
                "note": "тимчасово вимкнено",
            },
        ],
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def test_load_channels(seed_path):
    channels = LocalJSONConfigSource(seed_path).load_channels()
    assert len(channels) == 1
    channel = channels[0]
    assert channel.channel_id == "zhytomyr"
    assert channel.telegram_chat_id == "-1001234567890"
    assert channel.min_relevance == 0.6
    assert channel.headline_probability == 0.3


def test_subscribe_cta_defaults_to_channel_name_when_not_specified(seed_path):
    channel = LocalJSONConfigSource(seed_path).load_channels()[0]
    assert channel.subscribe_cta == "Підписуйся на Житомир"


def test_subscribe_cta_explicit_value_is_respected(tmp_path):
    data = {
        "channels": [
            {
                "channel_id": "zhytomyr",
                "name": "Житомир",
                "telegram_chat_id": None,
                "subscribe_cta": "Підписуйся на Мій Житомир",
            }
        ],
        "sources": [],
    }
    path = tmp_path / "seed.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    channel = LocalJSONConfigSource(path).load_channels()[0]
    assert channel.subscribe_cta == "Підписуйся на Мій Житомир"


def test_load_sources(seed_path):
    sources = LocalJSONConfigSource(seed_path).load_sources()
    assert len(sources) == 2
    active = [s for s in sources if s.active]
    assert len(active) == 1
    assert active[0].type == "telegram"


def _assert_seed_file_is_internally_consistent(path, expected_channel_count):
    """Спільна перевірка формату (не конкретного вмісту) для робочого й архівного сідів."""
    source = LocalJSONConfigSource(path)
    channels = source.load_channels()
    sources = source.load_sources()

    assert len(channels) == expected_channel_count
    channel_ids = {c.channel_id for c in channels}
    assert len(channel_ids) == expected_channel_count  # усі унікальні

    for s in sources:
        assert s.channel_id in channel_ids
        assert s.type in ("telegram", "website", "web_scrape", "facebook_page")
        assert s.handle_or_url
        if s.type == "web_scrape":
            assert s.scrape_item_selector, f"{s.handle_or_url}: web_scrape без scrape_item_selector"


def test_active_seed_file_has_the_7_channels_in_work():
    """data/sources_seed.json — лише канали, що зараз реально ведуться."""
    _assert_seed_file_is_internally_consistent("data/sources_seed.json", expected_channel_count=7)


def test_active_channels_all_have_a_telegram_chat_id():
    """Реальні канали вже створені й мають @username — жоден не має лишитись
    з null, інакше pipeline мовчки пропустить публікацію (лише лог-warning)."""
    channels = LocalJSONConfigSource("data/sources_seed.json").load_channels()
    for channel in channels:
        assert channel.telegram_chat_id, f"{channel.channel_id} без telegram_chat_id"
        assert channel.telegram_chat_id.startswith("@")


def test_archived_seed_file_has_the_16_parked_channels():
    """data/sources_archive.json — відкладені райцентри; не завантажується
    конвеєром за замовчуванням, але джерела нікуди не зникли."""
    _assert_seed_file_is_internally_consistent("data/sources_archive.json", expected_channel_count=16)


def test_active_and_archived_channels_do_not_overlap():
    active_ids = {c.channel_id for c in LocalJSONConfigSource("data/sources_seed.json").load_channels()}
    archived_ids = {c.channel_id for c in LocalJSONConfigSource("data/sources_archive.json").load_channels()}
    assert active_ids.isdisjoint(archived_ids)
    assert len(active_ids | archived_ids) == 23
