import datetime as dt

import httpx
import pytest
import respx

from telegram_net.config import SourceConfig
from telegram_net.fetchers.tme_fetcher import TmeFetcher, channel_handle, parse_channel_page, parse_duration


def _msg(msg_id, text="", extra="", service=False):
    cls = "tgme_widget_message service_message" if service else "tgme_widget_message text_not_supported_wrap"
    text_html = f'<div class="tgme_widget_message_text js-message_text" dir="auto">{text}</div>' if text else ""
    return f"""
    <div class="tgme_widget_message_wrap"><div class="{cls}" data-post="chan/{msg_id}">
      {extra}{text_html}
      <a class="tgme_widget_message_date" href="https://t.me/chan/{msg_id}">
        <time datetime="2026-11-14T08:{msg_id % 60:02d}:00+00:00">08:00</time></a>
    </div></div>"""


def _page(*messages):
    return "<html><body>" + "".join(messages) + "</body></html>"


_PHOTO = """<a class="tgme_widget_message_photo_wrap" style="width:800px;background-image:url('https://cdn.example/p{n}.jpg')"></a>"""
_VIDEO = """<a class="tgme_widget_message_video_player" href="#">
  <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn.example/thumb.jpg')"></i>
  <video src="https://cdn.example/v.mp4" class="tgme_widget_message_video"></video>
  <time class="message_video_duration">1:14</time></a>"""
_BIG_VIDEO = """<a class="tgme_widget_message_video_player not_supported" href="#">
  <i class="tgme_widget_message_video_thumb" style="background-image:url('https://cdn.example/thumb.jpg')"></i>
  <time class="message_video_duration">12:00</time></a>"""


def _source(handle="@chan"):
    return SourceConfig(channel_id="x", type="telegram", name="T", handle_or_url=handle)


def test_handle_and_duration_helpers():
    assert channel_handle("@chan") == channel_handle("https://t.me/chan/") == channel_handle("t.me/chan") == "chan"
    assert parse_duration("1:14") == 74 and parse_duration("1:00:05") == 3605 and parse_duration("x") is None


def test_parse_text_album_video_and_service_messages():
    html = _page(
        _msg(1, "Перший рядок<br/>Другий <b>рядок</b>"),
        _msg(2, "Альбом", extra=_PHOTO.format(n=1) + _PHOTO.format(n=2)),
        _msg(3, "Відео", extra=_VIDEO),
        _msg(4, "Велике відео", extra=_BIG_VIDEO),
        _msg(5, "Канал закріпив повідомлення", service=True),
        _msg(6),  # ні тексту, ні медіа
    )
    parsed = dict(parse_channel_page(html))

    assert sorted(parsed) == [1, 2, 3, 4]
    assert parsed[1].text == "Перший рядок\nДругий рядок"
    assert parsed[1].url == "https://t.me/chan/1"
    assert parsed[1].published_at == dt.datetime(2026, 11, 14, 8, 1, tzinfo=dt.timezone.utc)
    assert [m.url for m in parsed[2].media] == ["https://cdn.example/p1.jpg", "https://cdn.example/p2.jpg"]
    assert (parsed[3].media[0].type, parsed[3].media[0].url, parsed[3].media[0].duration_sec) == (
        "video",
        "https://cdn.example/v.mp4",
        74.0,
    )
    # Велике відео без посилання на файл — беремо обкладинку як фото.
    assert (parsed[4].media[0].type, parsed[4].media[0].url) == ("photo", "https://cdn.example/thumb.jpg")


@respx.mock
def test_first_fetch_returns_page_and_sets_cursor():
    respx.get("https://t.me/s/chan").mock(return_value=httpx.Response(200, text=_page(_msg(10, "а"), _msg(11, "б"))))
    items, cursor = TmeFetcher().fetch_new(_source(), None)
    assert [i.external_id for i in items] == ["10", "11"] and cursor == "11"


@respx.mock
def test_next_fetch_returns_only_newer_messages():
    respx.get("https://t.me/s/chan").mock(
        return_value=httpx.Response(200, text=_page(_msg(10, "а"), _msg(11, "б"), _msg(12, "в")))
    )
    items, cursor = TmeFetcher().fetch_new(_source(), "11")
    assert [i.external_id for i in items] == ["12"] and cursor == "12"

    items, cursor = TmeFetcher().fetch_new(_source(), "12")
    assert items == [] and cursor == "12"


@respx.mock
def test_reads_older_page_when_gap_is_larger_than_one_page():
    route = respx.get("https://t.me/s/chan")
    route.side_effect = lambda request: httpx.Response(
        200,
        text=_page(_msg(5, "стара"), _msg(6, "ще")) if "before" in str(request.url) else _page(_msg(7, "а"), _msg(8, "б")),
    )
    items, cursor = TmeFetcher().fetch_new(_source(), "4")
    assert [i.external_id for i in items] == ["5", "6", "7", "8"] and cursor == "8"


@respx.mock
def test_private_or_missing_channel_raises():
    respx.get("https://t.me/s/chan").mock(return_value=httpx.Response(302, headers={"location": "https://t.me/chan"}))
    with pytest.raises(RuntimeError):
        TmeFetcher().fetch_new(_source(), None)
