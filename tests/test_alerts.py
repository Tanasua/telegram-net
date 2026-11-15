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


class Sender:
    def __init__(self):
        self.sent = []

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return 1


def test_relay_bootstraps_silently_then_publishes_new_signals_to_matching_channels():
    sender = Sender()
    relay = AlertRelay(make_config(), sender)
    history = [
        item(1, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=20),
        item(2, "Звичайна новина ОВА", minutes_ago=15),
    ]
    relay.process(history, NOW)
    assert sender.sent == [] and relay.cursor == 2 and "Коростенський" in relay.active  # старт: лише відновили стан

    relay.process(history, NOW)
    assert sender.sent == []  # нічого нового

    relay.process(
        history
        + [
            item(3, "🟢 Коростенський район — відбій повітряної тривоги", minutes_ago=1),
            item(4, "🔴 Житомирський район — повітряна тривога, червоний рівень: Ракетна загроза (червоний рівень)", minutes_ago=0),
            item(5, "🟡 Бердичівський район — повітряна тривога, жовтий рівень", minutes_ago=0),  # каналів цього району немає
        ],
        NOW,
    )
    assert [chat for chat, _ in sender.sent] == ["@mymalyn", "@myzhytomyr"]
    assert "Відбій" in sender.sent[0][1] and "Тривога тривала 19 хв." in sender.sent[0][1]
    assert "червоний рівень" in sender.sent[1][1] and "Ракетна загроза." in sender.sent[1][1]
    assert relay.cursor == 5 and set(relay.active) == {"Житомирський", "Бердичівський"}


def test_stale_signals_after_downtime_are_not_published():
    sender = Sender()
    relay = AlertRelay(make_config(), sender)
    relay.process([item(1, "новина", minutes_ago=60)], NOW)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=25)], NOW)
    assert sender.sent == [] and "Коростенський" in relay.active


def test_send_failure_does_not_break_relay():
    class Broken:
        def send_text(self, chat_id, text):
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


def test_state_survives_restart_and_catches_signals_from_the_gap():
    """GitHub Actions перезапускає цикл кожні ~5 год. Сигнал, що прийшов під час
    перезапуску, має вийти, а вже надіслані — не повторитись."""
    first = AlertRelay(make_config(), Sender())
    first.process([item(1, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=20)], NOW)
    first.process([item(1, "x", 20), item(2, "🟡 Житомирський район — повітряна тривога, жовтий рівень", minutes_ago=5)], NOW)
    saved = first.dump_state()
    assert saved["cursor"] == 2 and set(saved["active"]) == {"Коростенський", "Житомирський"}

    sender = Sender()
    second = AlertRelay(make_config(), sender)
    second.load_state(saved)
    history = [item(1, "x", 20), item(2, "x", 5), item(3, "🟢 Коростенський район — відбій повітряної тривоги", minutes_ago=1)]
    second.process(history, NOW)
    assert [chat for chat, _ in sender.sent] == ["@mymalyn"]  # лише відбій із «розриву», без повтору тривог
    assert "Тривога тривала 19 хв." in sender.sent[0][1]


class PhotoSender(Sender):
    def __init__(self):
        super().__init__()
        self.photos = []

    def send_photo_bytes(self, chat_id, image, caption=None, filename="image.png"):
        self.photos.append((chat_id, image, caption))
        return 2


def _relay_with_map(provider):
    sender = PhotoSender()
    relay = AlertRelay(make_config(), sender, provider)
    relay.process([item(1, "новина", minutes_ago=30)], NOW)  # старт без сигналів
    return sender, relay


def test_map_is_sent_when_a_new_alert_starts_and_refreshed_while_it_lasts():
    shots = []

    def provider():
        shots.append(1)
        return b"PNG"

    sender, relay = _relay_with_map(provider)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    assert [chat for chat, _, _ in sender.photos] == ["@mymalyn"] and len(shots) == 1
    assert "Карта повітряних загроз" in sender.photos[0][2] and "neptun.in.ua" in sender.photos[0][2]

    # Те саме повідомлення про рівень не дублює карту, коротка пауза теж.
    relay.process([item(3, "🔴 Коростенський район — повітряна тривога, червоний рівень", minutes_ago=0)], NOW + dt.timedelta(minutes=5))
    relay.process([], NOW + dt.timedelta(minutes=20))
    assert len(sender.photos) == 1

    relay.process([], NOW + dt.timedelta(minutes=31))  # тривога триває — свіжа карта
    assert len(sender.photos) == 2

    relay.process([item(4, "🟢 Коростенський район — відбій повітряної тривоги", minutes_ago=0)], NOW + dt.timedelta(minutes=40))
    relay.process([], NOW + dt.timedelta(minutes=80))
    assert len(sender.photos) == 2  # після відбою карт нема


def test_map_failure_does_not_block_alert_text():
    sender, relay = _relay_with_map(lambda: None)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    assert len(sender.sent) == 1 and sender.photos == []

    class BrokenPhoto(PhotoSender):
        def send_photo_bytes(self, *a, **k):
            raise RuntimeError("boom")

    relay = AlertRelay(make_config(), BrokenPhoto(), lambda: b"PNG")
    relay.process([item(1, "новина", minutes_ago=30)], NOW)
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    assert relay.cursor == 2


def test_map_state_survives_restart():
    sender, relay = _relay_with_map(lambda: b"PNG")
    relay.process([item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень", minutes_ago=0)], NOW)
    restored = AlertRelay(make_config(), sender, lambda: b"PNG")
    restored.load_state(relay.dump_state())
    assert restored.last_map == relay.last_map
