import datetime as dt
from io import BytesIO

from helpers import make_config

from telegram_net.fuel import caption, publish_fuel, render
from telegram_net.markets import Fuel, Markets, Rate
from telegram_net.state import State


def _markets():
    return Markets(
        rate_date=dt.date(2026, 11, 16),
        rates=[Rate("USD", 45.4274, 0.04, 45.3, 45.95), Rate("EUR", 50.222, -0.12)],
        fuels=[Fuel("А-95", 87.55, -0.38), Fuel("ДП", 102.49, -0.38), Fuel("Газ", 61.36, 0.14)],
        cheapest={"А-95": ("Фактор", 85.0)},
    )


def test_caption():
    assert "А-95 — 87,55" in caption(_markets())
    assert "ДП — 102,49" in caption(_markets())


def test_render_produces_square_png_for_each_variant():
    m = _markets()
    from PIL import Image

    for v in (0, 1, 2):
        png = render(m, "Мій Малин", dt.date(2026, 11, 15), variant=v)
        assert png[:8] == b"\x89PNG\r\n\x1a\n"
        assert Image.open(BytesIO(png)).size == (1080, 1080)


class FakePhotoPublisher:
    def __init__(self):
        self.sent = []

    def send_photo_bytes(self, chat_id, image, caption=None, filename="x.png"):
        self.sent.append((chat_id, caption))
        return len(self.sent)


NOON = dt.datetime(2026, 11, 15, 10, 3, tzinfo=dt.timezone.utc)  # 12:03 за Києвом


def test_publish_fuel_at_noon(tmp_path):
    config, state, publisher = make_config(), State(tmp_path), FakePhotoPublisher()
    early = publish_fuel(config, state, publisher, now=NOON - dt.timedelta(hours=1), fetch=_markets)
    assert publisher.sent == [] and "пропускаю" in early[0]

    publish_fuel(config, state, publisher, now=NOON, fetch=_markets)
    assert [c for c, _ in publisher.sent] == ["@mymalyn", "@myzhytomyr"]
    assert publisher.sent[0][1].startswith("<b>Ціни на пальне</b>\n")
    assert state.channel_post_count("malyn") == 0

    again = publish_fuel(config, state, publisher, now=NOON + dt.timedelta(minutes=30), fetch=_markets, force=True)
    assert len(publisher.sent) == 2 and "вже були" in again[0]
