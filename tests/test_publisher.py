import httpx
import pytest
import respx

from telegram_net.fetchers.base import FetchedMedia
from telegram_net.publisher import TelegramPublisher

TOKEN = "123:test-token"
BASE = f"https://api.telegram.org/bot{TOKEN}"


@respx.mock
def test_send_text():
    respx.post(f"{BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    publisher = TelegramPublisher(TOKEN)
    message_id = publisher.send_text("-1001", "привіт")
    assert message_id == 1


@respx.mock
def test_publish_without_media_sends_plain_text():
    respx.post(f"{BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 5}})
    )
    publisher = TelegramPublisher(TOKEN)
    ids = publisher.publish("-1001", "текст без медіа", media_batches=[])
    assert ids == [5]


@respx.mock
def test_publish_single_photo_sends_caption_with_photo():
    route = respx.post(f"{BASE}/sendPhoto").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 7}})
    )
    publisher = TelegramPublisher(TOKEN)
    media = [(FetchedMedia(type="photo", url="https://example.com/a.jpg"),)]

    ids = publisher.publish("-1001", "підпис", media_batches=media)

    assert ids == [7]
    sent_payload = route.calls.last.request.content
    assert b"\xd0\xbf\xd1\x96\xd0\xb4\xd0\xbf\xd0\xb8\xd1\x81" in sent_payload  # "підпис" utf-8


@respx.mock
def test_publish_gallery_uses_media_group_with_caption_on_first_item():
    route = respx.post(f"{BASE}/sendMediaGroup").mock(
        return_value=httpx.Response(
            200, json={"ok": True, "result": [{"message_id": 1}, {"message_id": 2}, {"message_id": 3}]}
        )
    )
    publisher = TelegramPublisher(TOKEN)
    batch = tuple(FetchedMedia(type="photo", url=f"https://example.com/{i}.jpg") for i in range(3))

    ids = publisher.publish("-1001", "галерея", media_batches=[batch])

    assert ids == [1, 2, 3]
    sent_media = route.calls.last.request.content
    assert b"\xd0\xb3\xd0\xb0\xd0\xbb\xd0\xb5\xd1\x80\xd0\xb5\xd1\x8f" in sent_media  # "галерея"


@respx.mock
def test_publish_multiple_batches_only_first_gets_caption():
    media_group_route = respx.post(f"{BASE}/sendMediaGroup").mock(
        side_effect=[
            httpx.Response(200, json={"ok": True, "result": [{"message_id": i} for i in range(1, 11)]}),
            httpx.Response(200, json={"ok": True, "result": [{"message_id": i} for i in range(11, 16)]}),
        ]
    )
    publisher = TelegramPublisher(TOKEN)
    batch1 = tuple(FetchedMedia(type="photo", url=f"https://example.com/{i}.jpg") for i in range(10))
    batch2 = tuple(FetchedMedia(type="photo", url=f"https://example.com/{i}.jpg") for i in range(5))

    ids = publisher.publish("-1001", "перший підпис", media_batches=[batch1, batch2])

    assert ids == list(range(1, 16))
    first_call_media = media_group_route.calls[0].request.content
    second_call_media = media_group_route.calls[1].request.content
    assert b"caption" in first_call_media
    assert b"caption" not in second_call_media


@respx.mock
def test_raises_on_telegram_api_error():
    respx.post(f"{BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": False, "description": "chat not found"})
    )
    publisher = TelegramPublisher(TOKEN)
    with pytest.raises(RuntimeError, match="chat not found"):
        publisher.send_text("-100999", "текст")


@respx.mock
def test_error_description_surfaces_even_with_4xx_http_status():
    """Регресія: Telegram майже завжди повертає 4xx РАЗОМ з інформативним JSON
    ("chat not found" тощо) — старий код кликав raise_for_status() до парсингу
    тіла, тож опис губився і лишався лише голий '400 Bad Request'."""
    respx.post(f"{BASE}/sendMessage").mock(
        return_value=httpx.Response(
            400, json={"ok": False, "error_code": 400, "description": "Bad Request: chat not found"}
        )
    )
    publisher = TelegramPublisher(TOKEN)
    with pytest.raises(RuntimeError, match="chat not found"):
        publisher.send_text("-100999", "текст")
