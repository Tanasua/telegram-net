import datetime as dt
from dataclasses import dataclass

from telegram_net.fetchers.telegram_fetcher import TelethonFetcher, group_messages_into_items


@dataclass
class FakeVideo:
    duration: float


@dataclass
class FakeMessage:
    id: int
    date: dt.datetime
    message: str | None = None
    grouped_id: int | None = None
    photo: object | None = None
    video: FakeVideo | None = None


def test_single_text_message_becomes_one_item_without_media():
    msg = FakeMessage(id=1, date=dt.datetime(2026, 8, 14), message="Просто текст")
    items = group_messages_into_items([msg], "some_channel")
    assert len(items) == 1
    assert items[0].text == "Просто текст"
    assert items[0].media == ()
    assert items[0].url == "https://t.me/some_channel/1"


def test_single_photo_message_has_one_media():
    msg = FakeMessage(id=2, date=dt.datetime(2026, 8, 14), message="Фото новини", photo=object())
    items = group_messages_into_items([msg], "some_channel")
    assert len(items) == 1
    assert len(items[0].media) == 1
    assert items[0].media[0].type == "photo"


def test_album_messages_with_same_grouped_id_merge_into_one_item():
    caption_msg = FakeMessage(id=10, date=dt.datetime(2026, 8, 14, 12, 0), message="Підпис галереї", grouped_id=555, photo=object())
    photo2 = FakeMessage(id=11, date=dt.datetime(2026, 8, 14, 12, 0), grouped_id=555, photo=object())
    photo3 = FakeMessage(id=12, date=dt.datetime(2026, 8, 14, 12, 0), grouped_id=555, photo=object())

    items = group_messages_into_items([photo3, caption_msg, photo2], "some_channel")

    assert len(items) == 1
    item = items[0]
    assert item.text == "Підпис галереї"
    assert len(item.media) == 3
    assert all(m.type == "photo" for m in item.media)
    assert item.url == "https://t.me/some_channel/10"  # перше повідомлення групи за id


def test_video_message_carries_duration():
    msg = FakeMessage(id=20, date=dt.datetime(2026, 8, 14), message="Відео з подій", video=FakeVideo(duration=42.0))
    items = group_messages_into_items([msg], "some_channel")
    assert items[0].media[0].type == "video"
    assert items[0].media[0].duration_sec == 42.0


def test_mixed_singles_and_group_in_one_batch():
    single = FakeMessage(id=1, date=dt.datetime(2026, 8, 14), message="Одиночний пост")
    group_a = FakeMessage(id=2, date=dt.datetime(2026, 8, 14), message="Галерея", grouped_id=99, photo=object())
    group_b = FakeMessage(id=3, date=dt.datetime(2026, 8, 14), grouped_id=99, photo=object())

    items = group_messages_into_items([single, group_a, group_b], "some_channel")

    assert len(items) == 2
    texts = {item.text for item in items}
    assert texts == {"Одиночний пост", "Галерея"}


class FakeTelethonClient:
    def __init__(self, messages):
        self._messages = messages
        self.iter_messages_calls = []

    def iter_messages(self, handle, min_id=0, reverse=True):
        self.iter_messages_calls.append((handle, min_id, reverse))
        return [m for m in self._messages if m.id > min_id]


def test_telethon_fetcher_returns_new_cursor_as_max_message_id():
    messages = [
        FakeMessage(id=1, date=dt.datetime(2026, 8, 14), message="перше"),
        FakeMessage(id=2, date=dt.datetime(2026, 8, 14), message="друге"),
    ]
    client = FakeTelethonClient(messages)
    fetcher = TelethonFetcher(client)

    items, cursor = fetcher.fetch_new(
        source=type("S", (), {"handle_or_url": "@some_channel"})(), cursor=None
    )

    assert len(items) == 2
    assert cursor == "2"
    assert client.iter_messages_calls == [("some_channel", 0, True)]


def test_telethon_fetcher_no_new_messages_keeps_cursor():
    client = FakeTelethonClient([])
    fetcher = TelethonFetcher(client)
    items, cursor = fetcher.fetch_new(
        source=type("S", (), {"handle_or_url": "@some_channel"})(), cursor="5"
    )
    assert items == []
    assert cursor == "5"
