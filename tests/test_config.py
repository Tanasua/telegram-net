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


def test_load_sources(seed_path):
    sources = LocalJSONConfigSource(seed_path).load_sources()
    assert len(sources) == 2
    active = [s for s in sources if s.active]
    assert len(active) == 1
    assert active[0].type == "telegram"


def test_real_seed_file_loads_and_is_internally_consistent():
    """data/sources_seed.json — реальний сід, зібраний дослідженням; тест
    ловить регресії формату, а не конкретний вміст."""
    source = LocalJSONConfigSource("data/sources_seed.json")
    channels = source.load_channels()
    sources = source.load_sources()

    assert len(channels) == 23
    channel_ids = {c.channel_id for c in channels}
    assert len(channel_ids) == 23  # усі унікальні

    for s in sources:
        assert s.channel_id in channel_ids
        assert s.type in ("telegram", "website", "facebook_page")
        assert s.handle_or_url
