import datetime as dt
from zoneinfo import ZoneInfo

import httpx
import respx
from helpers import make_config

from telegram_net.config import ChannelConfig
from telegram_net.state import State, iso
from telegram_net.weather import Hour, caption, fetch_met, fmt_temp, met_sky, post_caption, publish_weather, render, summarize

KYIV = ZoneInfo("Europe/Kyiv")
DAY = dt.date(2026, 11, 16)


def _hours(sky_by_hour=None, temp=lambda h: 2 + h / 3, wind=2.0):
    """Погодинний прогноз на DAY і ніч після нього."""
    sky_by_hour = sky_by_hour or {}
    start = dt.datetime(2026, 11, 16, 0, tzinfo=KYIV)
    return [
        Hour(time=start + dt.timedelta(hours=i), temp=temp(i % 24), sky=sky_by_hour.get(i % 24, "cloudy"), wind=wind)
        for i in range(30)
    ]


def test_met_symbols_and_temperatures_format():
    assert met_sky("clearsky_day") == "clear" and met_sky("partlycloudy_night") == "partly"
    assert met_sky("lightrainshowers_day") == "showers" and met_sky("heavyrainandthunder") == "thunder"
    assert met_sky("lightsleet") == "sleet" and met_sky("snowshowers_day") == "snow"
    assert (fmt_temp(5), fmt_temp(0), fmt_temp(-3)) == ("+5°", "0°", "−3°")


def test_summary_has_four_parts_hero_and_tip():
    f = summarize(_hours(), DAY, "Радомишлі")
    assert [p.name for p in f.parts] == ["Ранок", "День", "Вечір", "Ніч"]
    assert f.hero_icon == "cloudy" and f.phrase == "хмарно"
    assert f.day_max == round(max(2 + h / 3 for h in range(8, 22)))
    assert f.parts[3].temp == round(min([2 + h / 3 for h in (22, 23)] + [2 + h / 3 for h in range(0, 6)]))
    assert f.tip == "Гарного дня!"  # ранок близько +5°, без опадів
    cold_morning = summarize(_hours(temp=lambda h: 1 if h < 11 else 9), DAY, "Радомишлі")
    assert cold_morning.tip == "Зранку холодно — вдягніться тепліше"
    assert caption(f).startswith("Хмарно, вдень до +")
    assert post_caption(f).startswith("<b>Погода у Радомишлі сьогодні</b>\n")


def test_rain_wins_over_clouds_and_suggests_umbrella():
    rain = {h: "rain" for h in (12, 13, 14)}
    f = summarize(_hours(rain, temp=lambda h: 10), DAY, "Малині")
    assert f.hero_icon == "rain" and f.tip == "Візьміть парасольку"
    assert next(p for p in f.parts if p.name == "День").icon == "rain"


def test_clear_evening_and_night_show_the_moon_and_frost_tip():
    clear = {h: "clear" for h in range(24)}
    f = summarize(_hours(clear, temp=lambda h: -5), DAY, "Житомирі")
    icons = {p.name: p.icon for p in f.parts}
    assert icons == {"Ранок": "clear-day", "День": "clear-day", "Вечір": "clear-night", "Ніч": "clear-night"}
    assert f.tip == "Морозно — вдягайтеся тепло" and fmt_temp(f.day_max) == "−5°"


def test_render_produces_square_png():
    png = render(summarize(_hours(), DAY, "Коростишеві"), "Мій Коростишів")
    assert png[:8] == b"\x89PNG\r\n\x1a\n"
    from io import BytesIO

    from PIL import Image

    assert Image.open(BytesIO(png)).size == (1080, 1080)


@respx.mock
def test_fetch_met_parses_timeseries_into_kyiv_time():
    respx.get("https://api.met.no/weatherapi/locationforecast/2.0/compact").mock(
        return_value=httpx.Response(
            200,
            json={
                "properties": {
                    "timeseries": [
                        {
                            "time": "2026-11-16T06:00:00Z",
                            "data": {
                                "instant": {"details": {"air_temperature": 3.4, "wind_speed": 4.0}},
                                "next_1_hours": {"summary": {"symbol_code": "lightrain"}},
                            },
                        },
                        {"time": "2026-11-26T06:00:00Z", "data": {"instant": {"details": {"air_temperature": 1}}}},
                    ]
                }
            },
        )
    )
    (hour,) = fetch_met(50.5, 29.2, KYIV, httpx.Client())
    assert hour.time.hour == 8 and hour.temp == 3.4 and hour.sky == "rain"
    assert "telegram-net" in respx.calls.last.request.headers["user-agent"]


class FakePhotoPublisher:
    def __init__(self):
        self.sent = []

    def send_photo_bytes(self, chat_id, image, caption=None, filename="x.png"):
        self.sent.append((chat_id, caption))
        return 500 + len(self.sent)


def _weather_config():
    config = make_config()
    channels = [
        ChannelConfig("malyn", "Малин", "@mymalyn", subscribe_cta="Підписатися на «Мій Малин»", weather={"lat": 50.77, "lon": 29.24, "city_loc": "Малині"}),
        ChannelConfig("zhytomyr", "Житомир", "@myzhytomyr"),  # без координат — погоди немає
    ]
    return type(config)(settings=config.settings, channels=channels, sources=[])


EIGHT = dt.datetime(2026, 11, 16, 6, 5, tzinfo=dt.timezone.utc)  # 08:05 за Києвом


def test_publish_weather_once_a_day_at_eight(tmp_path):
    config, state, publisher = _weather_config(), State(tmp_path), FakePhotoPublisher()
    fetch = lambda lat, lon, tz: _hours()  # noqa: E731

    early = publish_weather(config, state, publisher, now=EIGHT - dt.timedelta(hours=1), fetch=fetch)
    assert publisher.sent == [] and "пропускаю" in early[0]  # 07:05 — ще не час

    lines = publish_weather(config, state, publisher, now=EIGHT, fetch=fetch)
    assert publisher.sent == [("@mymalyn", publisher.sent[0][1])] and "Погода у Малині сьогодні" in publisher.sent[0][1]
    assert "надіслано" in lines[0]
    (entry,) = state.published
    assert entry["kind"] == "weather" and entry["published_at"] == iso(EIGHT)

    publish_weather(config, state, publisher, now=EIGHT + dt.timedelta(minutes=30), fetch=fetch, force=True)
    assert len(publisher.sent) == 1  # другий раз за день — ні

    # Погода не забирає ліміт і не зсуває ротацію закликів/звертань.
    assert state.channel_post_count("malyn") == 0


def test_publish_weather_dry_run_saves_images(tmp_path):
    lines = publish_weather(_weather_config(), State(tmp_path), None, now=EIGHT, out_dir=tmp_path / "out", fetch=lambda *a: _hours())
    assert (tmp_path / "out" / "weather_malyn.png").exists() and "[пробний]" in lines[0]


def test_failed_fetch_is_reported_not_raised(tmp_path):
    def broken(*a):
        raise httpx.ConnectError("нема мережі")

    lines = publish_weather(_weather_config(), State(tmp_path), FakePhotoPublisher(), now=EIGHT, fetch=broken)
    assert "ПОМИЛКА" in lines[0]


def test_every_icon_used_by_the_code_exists():
    from telegram_net.weather import _SKY, ASSETS

    tips = {"lightning-bolt", "snowflake", "umbrella", "wind", "thermometer-colder", "thermometer-warmer", "sunrise"}
    names = {c.icon for c in _SKY.values()} | {c.night_icon for c in _SKY.values()} | tips
    missing = [n for n in names if not (ASSETS / "icons" / f"{n}.png").exists()]
    assert missing == []
