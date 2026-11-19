import datetime as dt

from helpers import NOW, item, make_config

from telegram_net.alerts import AlertEvent, AlertRelay, format_duration, parse_alert, render_alert, run_loop


def test_parse_real_formats_of_the_oblast_administration_channel():
    assert parse_alert("🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)") == AlertEvent(
        "alert", ("Коростенський",), "yellow", "Дронова загроза"
    )
    assert parse_alert("🔴 Житомирський, Коростенський, Звягельський райони — повітряна тривога, червоний рівень: ракетна загроза.") == AlertEvent(
        "alert", ("Житомирський", "Коростенський", "Звягельський"), "red", "Ракетна загроза"
    )
    assert parse_alert("🟡 Звягельський район — повітряна тривога, жовтий рівень") == AlertEvent("alert", ("Звягельський",), "yellow", None)
    # У відбої нижче перелічено райони, де тривога ще триває, — їх чіпати не можна.
    clear = parse_alert("🟢 Житомирський район — відбій повітряної тривоги\n\n⚠️ Зверніть увагу, повітряна тривога досі триває у:\n- Бердичівський район")
    assert clear == AlertEvent("clear", ("Житомирський",))
    assert len(parse_alert("🔴 Житомирська область — повітряна тривога").districts) == 4


def test_ordinary_posts_are_not_alerts():
    assert parse_alert("Начальник ОВА провів нараду щодо підготовки до зими — подробиці згодом") is None
    assert parse_alert("За добу в області п'ять разів оголошували повітряну тривогу") is None
    assert parse_alert("Київська область — повітряна тривога") is None
    assert parse_alert("Сумський район — повітряна тривога") is None


def test_render_and_duration():
    start = NOW - dt.timedelta(minutes=29)
    alert = AlertEvent("alert", ("Коростенський",), "red", "Ракетна загроза")
    assert render_alert(alert, "Коростенський", None, NOW) == "🔴 <b>Повітряна тривога, червоний рівень</b>\nКоростенський район\nРакетна загроза."
    clear = AlertEvent("clear", ("Коростенський",))
    assert render_alert(clear, "Коростенський", start, NOW) == "🟢 <b>Відбій повітряної тривоги</b>\nКоростенський район\nТривога тривала 29 хв."
    assert render_alert(clear, "Коростенський", None, NOW) == "🟢 <b>Відбій повітряної тривоги</b>\nКоростенський район"
    assert format_duration(dt.timedelta(minutes=135)) == "2 год 15 хв" and format_duration(dt.timedelta(hours=1)) == "1 год"


class PhotoSender:
    def __init__(self):
        self.sent = []
        self.photos = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return 1

    def send_photo_bytes(self, chat_id, image, caption=None, filename="image.png"):
        self.photos.append((chat_id, image, caption))
        return 2


def test_relay_bootstraps_silently_then_publishes_new_signals_to_matching_channels():
    sender = PhotoSender()
    relay = AlertRelay(make_config(), sender)
    history = [
        item(1, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=20),
        item(2, "Звичайна новина ОВА", minutes_ago=15),
    ]
    relay.process(history, NOW)
    assert sender.photos == [] and relay.cursor == 2 and "Коростенський" in relay.active

    relay.process(history, NOW)
    assert sender.photos == []

    relay.process(
        history
        + [
            item(3, "🟢 Коростенський район — відбій повітряної тривоги", minutes_ago=1),
            item(4, "🔴 Житомирський район — повітряна тривога, червоний рівень: Ракетна загроза (червоний рівень)", minutes_ago=0),
            item(5, "🟡 Бердичівський район — повітряна тривога, жовтий рівень", minutes_ago=0),  # каналів цього району немає
        ],
        NOW,
    )
    assert [chat for chat, _, _ in sender.photos] == ["@mymalyn", "@myzhytomyr"]
    assert "Відбій" in sender.photos[0][2] and "Тривога тривала 19 хв." in sender.photos[0][2]
    assert "червоний рівень" in sender.photos[1][2] and "Ракетна загроза." in sender.photos[1][2]
    assert relay.cursor == 5 and set(relay.active) == {"Житомирський", "Бердичівський"}


def test_stale_signals_after_downtime_are_not_published():
    sender = PhotoSender()
    relay = AlertRelay(make_config(), sender)
    relay.process([item(1, "новина", minutes_ago=60)], NOW)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=25)], NOW)
    assert sender.photos == [] and "Коростенський" in relay.active


def test_send_failure_does_not_break_relay():
    class Broken:
        def send_photo_bytes(self, *a, **k):
            raise RuntimeError("boom")

    relay = AlertRelay(make_config(), Broken())
    relay.process([item(1, "x")], NOW)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    assert relay.cursor == 2


def test_loop_survives_fetch_errors_and_stops_by_runtime():
    relay = AlertRelay(make_config(), None)
    calls = []

    def fetch():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("мережа")
        return [item(1, "x")]

    sleeps = []
    run_loop(relay, fetch, interval_sec=30, max_runtime_min=0, sleep=sleeps.append)
    assert len(calls) == 1 and sleeps == []


def test_loop_stops_early_when_new_code_is_out():
    relay = AlertRelay(make_config(), None)
    answers = iter([False, False, True])
    sleeps = []
    run_loop(relay, lambda: [], interval_sec=30, max_runtime_min=600, sleep=sleeps.append, should_stop=lambda: next(answers))
    assert len(sleeps) == 2


def test_loop_keeps_running_when_stop_check_fails():
    relay = AlertRelay(make_config(), None)
    calls = []

    def should_stop():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("api")
        return True

    sleeps = []
    run_loop(relay, lambda: [], interval_sec=30, max_runtime_min=600, sleep=sleeps.append, should_stop=should_stop)
    assert len(sleeps) == 1


def test_state_survives_restart_and_catches_signals_from_the_gap():
    first = AlertRelay(make_config(), PhotoSender())
    first.process([item(1, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=20)], NOW)
    first.process([item(1, "x", 20), item(2, "🟡 Житомирський район — повітряна тривога, жовтий рівень", minutes_ago=5)], NOW)
    saved = first.dump_state()
    assert saved["cursor"] == 2 and set(saved["active"]) == {"Коростенський", "Житомирський"}

    sender = PhotoSender()
    second = AlertRelay(make_config(), sender)
    second.load_state(saved)
    history = [item(1, "x", 20), item(2, "x", 5), item(3, "🟢 Коростенський район — відбій повітряної тривоги", minutes_ago=1)]
    second.process(history, NOW)
    assert [chat for chat, _, _ in sender.photos] == ["@mymalyn"]
    assert "Тривога тривала 19 хв." in sender.photos[0][2]


def _relay_with_map(provider):
    sender = PhotoSender()
    relay = AlertRelay(make_config(), sender, provider)
    relay.process([item(1, "новина", minutes_ago=30)], NOW)
    return sender, relay


def test_map_is_sent_once_when_a_new_alert_starts():
    shots = []

    def provider():
        shots.append(1)
        return b"PNG"

    sender, relay = _relay_with_map(provider)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    map_photos = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert len(map_photos) == 1 and len(shots) == 1
    assert map_photos[0][2].startswith("🗺 Карта повітряних загроз станом на ")
    assert "Підписатися" not in map_photos[0][2]

    # Level change — map NOT sent again (district was already active)
    relay.process([item(3, "🔴 Коростенський район — повітряна тривога, червоний рівень", minutes_ago=0)], NOW + dt.timedelta(minutes=5))
    map_photos2 = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert len(map_photos2) == 1

    # Time passes — no more map refreshes
    relay.process([], NOW + dt.timedelta(minutes=36))
    map_photos3 = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert len(map_photos3) == 1

    # Clear — no map
    relay.process([item(4, "🟢 Коростенський район — відбій повітряної тривоги", minutes_ago=-40)], NOW + dt.timedelta(minutes=40))
    map_photos4 = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert len(map_photos4) == 1

    # New alert after clear — map sent again
    relay.process([item(5, "🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза", minutes_ago=-50)], NOW + dt.timedelta(minutes=50))
    map_photos5 = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert len(map_photos5) == 2


def test_map_caption_includes_threat_and_district():
    sender, relay = _relay_with_map(lambda: b"PNG")
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза (жовтий рівень)", minutes_ago=0)], NOW)
    map_photos = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert len(map_photos) == 1
    assert "Дронова загроза" in map_photos[0][2] and "Коростенськ" in map_photos[0][2]


def test_map_failure_does_not_block_alert_photo():
    sender, relay = _relay_with_map(lambda: None)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    alert_photos = [p for p in sender.photos if p[2] and "тривога" in p[2].lower()]
    assert len(alert_photos) == 1  # alert photo was sent
    map_photos = [p for p in sender.photos if p[2] and "Карта" in p[2]]
    assert map_photos == []  # map failed gracefully

    class BrokenPhoto(PhotoSender):
        def send_photo_bytes(self, *a, **k):
            raise RuntimeError("boom")

    relay = AlertRelay(make_config(), BrokenPhoto(), lambda: b"PNG")
    relay.process([item(1, "новина", minutes_ago=30)], NOW)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    assert relay.cursor == 2
