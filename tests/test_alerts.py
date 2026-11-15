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
