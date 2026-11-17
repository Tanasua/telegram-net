import datetime as dt
from io import BytesIO

import httpx
import respx
from helpers import make_config

from telegram_net.markets import (
    FUEL_URL,
    NBU_URL,
    PRIVAT_URL,
    Fuel,
    Markets,
    Rate,
    caption,
    fetch_markets,
    fmt_change,
    parse_fuel_page,
    post_caption,
    publish_markets,
    render,
)
from telegram_net.state import State

# Структура двох таблиць як на index.minfin.com.ua (у рядку мережі — зайва порожня клітинка після назви).
FUEL_HTML = """<html><body>
<table><tr><th>Вид палива</th><th>Ціна (грн.)</th></tr>
<tr><td>Бензин А-95 преміум</td><td>90,73</td><td>-1.00</td><td>-1.090%</td></tr>
<tr><td>Бензин А-95</td><td>87,55</td><td>-0.38</td><td>-0.427%</td></tr>
<tr><td>Бензин А-92</td><td>84,90</td><td>0.00</td><td>0%</td></tr>
<tr><td>Дизельне паливо</td><td>102,49</td><td>-0.38</td><td>-0.365%</td></tr>
<tr><td>Газ авто&shy;мобільний</td><td>61,36</td><td>0.14</td><td>0.233%</td></tr></table>
<table><tr><th>Оператор</th><th>А&nbsp;95+</th><th>А&nbsp;95</th><th>А&nbsp;92</th><th>ДП</th><th>Газ</th></tr>
<tr><td>WOG</td><td></td><td>93,90</td><td>90,90</td><td></td><td>105,90</td><td>63,50</td></tr>
<tr><td>БРСМ-Нафта</td><td></td><td></td><td>85,99</td><td></td><td>99,99</td><td>58,99</td></tr>
<tr><td>Фактор</td><td></td><td></td><td>85,00</td><td></td><td>99,50</td><td></td></tr></table>
</body></html>"""


def test_fuel_table_parsing():
    fuels, cheapest = parse_fuel_page(FUEL_HTML)
    assert [(f.name, f.price, f.change) for f in fuels] == [
        ("А-95+", 90.73, -1.0), ("А-95", 87.55, -0.38), ("А-92", 84.9, 0.0), ("ДП", 102.49, -0.38), ("Газ", 61.36, 0.14),
    ]
    assert cheapest == {"А-95": ("Фактор", 85.0), "ДП": ("Фактор", 99.5)}


def test_change_format():
    assert (fmt_change(0.0425), fmt_change(-0.1159), fmt_change(0.001), fmt_change(None)) == ("+0,04", "−0,12", "без змін", "без змін")


@respx.mock
def test_fetch_combines_nbu_privat_and_fuel():
    def nbu(request):
        date = request.url.params.get("date")
        usd = 45.38 if date else 45.4274  # попередній день — інший курс
        return httpx.Response(200, json=[
            {"cc": "USD", "rate": usd, "exchangedate": "16.11.2026"},
            {"cc": "EUR", "rate": 50.222, "exchangedate": "16.11.2026"},
            {"cc": "PLN", "rate": 11.39, "exchangedate": "16.11.2026"},
        ])

    respx.get(NBU_URL).mock(side_effect=nbu)
    respx.get(PRIVAT_URL).mock(return_value=httpx.Response(200, json=[{"ccy": "USD", "buy": "45.30", "sale": "45.95"}]))
    respx.get(FUEL_URL).mock(return_value=httpx.Response(200, text=FUEL_HTML))
    m = fetch_markets(httpx.Client())
    assert m.rate_date == dt.date(2026, 11, 16)
    usd = m.rates[0]
    assert (usd.code, usd.buy, usd.sell) == ("USD", 45.30, 45.95) and round(usd.change, 4) == 0.0474
    assert m.rates[1].buy is None  # ПриватБанк не дав євро — лише офіційний курс
    assert len(m.fuels) == 5


def _markets():
    return Markets(
        rate_date=dt.date(2026, 11, 16),
        rates=[Rate("USD", 45.4274, 0.04, 45.3, 45.95), Rate("EUR", 50.222, -0.12, 50.1, 51.1), Rate("PLN", 11.39, None)],
        fuels=[Fuel("А-95", 87.55, -0.38), Fuel("ДП", 102.49, -0.38), Fuel("Газ", 61.36, 0.14)],
        cheapest={"А-95": ("Фактор", 85.0)},
    )


def test_caption_and_render():
    m = _markets()
    assert caption(m).startswith("Курс НБУ на 16 листопада: USD 45,43 грн, готівка 45,30/45,95; EUR 50,22 грн")
    from PIL import Image

    assert Image.open(BytesIO(render(m, "Мій Малин", dt.date(2026, 11, 15)))).size == (1080, 1080)


class FakePhotoPublisher:
    def __init__(self):
        self.sent = []

    def send_photo_bytes(self, chat_id, image, caption=None, filename="x.png"):
        self.sent.append((chat_id, caption))
        return len(self.sent)


SEVEN_PM = dt.datetime(2026, 11, 15, 17, 3, tzinfo=dt.timezone.utc)  # 19:03 за Києвом


def test_publish_once_a_day_at_seven(tmp_path):
    config, state, publisher = make_config(), State(tmp_path), FakePhotoPublisher()
    early = publish_markets(config, state, publisher, now=SEVEN_PM - dt.timedelta(hours=1), fetch=_markets)
    assert publisher.sent == [] and "пропускаю" in early[0]

    publish_markets(config, state, publisher, now=SEVEN_PM, fetch=_markets)
    assert [c for c, _ in publisher.sent] == ["@mymalyn", "@myzhytomyr"]
    assert publisher.sent[0][1].startswith("<b>Курс валют</b>\n")
    assert state.channel_post_count("malyn") == 0  # службовий пост — не забирає ліміт

    again = publish_markets(config, state, publisher, now=SEVEN_PM + dt.timedelta(minutes=30), fetch=_markets, force=True)
    assert len(publisher.sent) == 2 and "вже був" in again[0]


def test_data_failure_is_reported(tmp_path):
    def broken():
        raise httpx.ConnectError("нема мережі")

    lines = publish_markets(make_config(), State(tmp_path), FakePhotoPublisher(), now=SEVEN_PM, fetch=broken)
    assert "ПОМИЛКА" in lines[0]
