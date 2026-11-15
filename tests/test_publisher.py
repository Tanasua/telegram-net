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


@respx.mock
def test_text_is_sent_as_html():
    route = respx.post(f"{BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})
    )
    TelegramPublisher(TOKEN).send_text("@c", "<b>Заголовок</b>")
    assert b'"parse_mode":"HTML"' in route.calls.last.request.content.replace(b" ", b"")


@respx.mock
def test_photo_is_uploaded_when_telegram_cannot_fetch_url():
    """Telegram не завжди може сам завантажити картинку за посиланням (сайт
    блокує його сервери) — тоді файл завантажуємо й надсилаємо напряму."""
    route = respx.post(f"{BASE}/sendPhoto")
    route.side_effect = [
        httpx.Response(400, json={"ok": False, "description": "Bad Request: failed to get HTTP URL content"}),
        httpx.Response(200, json={"ok": True, "result": {"message_id": 9}}),
    ]
    respx.get("https://example.com/a.jpg").mock(return_value=httpx.Response(200, content=b"JPEGDATA"))
    media = [(FetchedMedia(type="photo", url="https://example.com/a.jpg"),)]

    ids = TelegramPublisher(TOKEN).publish("@c", "підпис", media_batches=media)

    assert ids == [9] and route.call_count == 2
    upload = route.calls.last.request
    assert b"multipart/form-data" in upload.headers["content-type"].encode() and b"JPEGDATA" in upload.content


@respx.mock
def test_gallery_is_uploaded_when_urls_fail():
    route = respx.post(f"{BASE}/sendMediaGroup")
    route.side_effect = [
        httpx.Response(400, json={"ok": False, "description": "Bad Request: wrong file identifier/HTTP URL specified"}),
        httpx.Response(200, json={"ok": True, "result": [{"message_id": 1}, {"message_id": 2}]}),
    ]
    for i in range(2):
        respx.get(f"https://example.com/{i}.jpg").mock(return_value=httpx.Response(200, content=b"IMG%d" % i))
    batch = tuple(FetchedMedia(type="photo", url=f"https://example.com/{i}.jpg") for i in range(2))

    ids = TelegramPublisher(TOKEN).publish("@c", "галерея", media_batches=[batch])

    assert ids == [1, 2]
    body = route.calls.last.request.content
    assert b"attach://file0" in body and b"attach://file1" in body and b"IMG0" in body and b"IMG1" in body


@respx.mock
def test_post_goes_out_as_text_when_media_fails_completely():
    respx.post(f"{BASE}/sendPhoto").mock(
        return_value=httpx.Response(400, json={"ok": False, "description": "Bad Request: failed to get HTTP URL content"})
    )
    respx.get("https://example.com/a.jpg").mock(return_value=httpx.Response(404))
    text_route = respx.post(f"{BASE}/sendMessage").mock(
        return_value=httpx.Response(200, json={"ok": True, "result": {"message_id": 3}})
    )
    media = [(FetchedMedia(type="photo", url="https://example.com/a.jpg"),)]

    ids = TelegramPublisher(TOKEN).publish("@c", "новина важливіша за картинку", media_batches=media)

    assert ids == [3] and text_route.called


def _mock_membership(status, can_post=None):
    respx.post(f"{BASE}/getChat").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"title": "Мій Малин"}}))
    respx.post(f"{BASE}/getMe").mock(return_value=httpx.Response(200, json={"ok": True, "result": {"id": 42}}))
    member = {"status": status}
    if can_post is not None:
        member["can_post_messages"] = can_post
    respx.post(f"{BASE}/getChatMember").mock(return_value=httpx.Response(200, json={"ok": True, "result": member}))


@respx.mock
def test_check_chat_passes_for_admin_with_post_right():
    _mock_membership("administrator", can_post=True)
    assert TelegramPublisher(TOKEN).check_chat("@mymalyn") == "Мій Малин"


@respx.mock
@pytest.mark.parametrize("status,can_post", [("administrator", False), ("member", None), ("left", None)])
def test_check_chat_fails_when_bot_cannot_post(status, can_post):
    """getChat для публічного каналу успішний і без прав — перевіряти треба саме право публікації."""
    _mock_membership(status, can_post)
    with pytest.raises(RuntimeError, match="не адміністратор"):
        TelegramPublisher(TOKEN).check_chat("@mymalyn")
