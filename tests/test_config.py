import json

import pytest

from telegram_net.config import load_config


def test_seed_config_loads_and_is_consistent():
    config = load_config("data/sources_seed.json")
    assert len(config.active_channels) == 7
    assert all(c.alert_district for c in config.active_channels)
    channel_ids = {c.channel_id for c in config.channels}
    for s in config.sources:
        if s.scope == "local":
            assert s.channel_id in channel_ids
        else:
            assert s.channel_id is None
    # Обласні джерела не повинні дублюватись по каналах — інакше новину
    # «забирає» перший канал, а решта її не бачить (стара помилка).
    oblast = [s.handle_or_url for s in config.sources if s.scope == "oblast"]
    assert len(oblast) == len(set(oblast)) and "@zt_police" in oblast
    assert any(s.scope == "national" for s in config.sources)


def test_source_key_is_shared_for_non_local_scope(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(
        json.dumps(
            {
                "channels": [{"channel_id": "a", "name": "А", "telegram_chat_id": "@a"}],
                "sources": [
                    {"scope": "oblast", "type": "telegram", "name": "П", "handle_or_url": "@p"},
                    {"channel_id": "a", "type": "telegram", "name": "Л", "handle_or_url": "@l"},
                ],
            }
        ),
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.sources[0].key == "oblast|telegram|@p"
    assert config.sources[1].key == "a|telegram|@l"
    assert config.channels[0].subscribe_cta == "Підписуйся на А"


def test_local_source_requires_channel(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"channels": [], "sources": [{"type": "telegram", "name": "x", "handle_or_url": "@x"}]}))
    with pytest.raises(ValueError):
        load_config(path)
