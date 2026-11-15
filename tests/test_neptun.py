import dataclasses
import datetime as dt
import json

from helpers import NOW, item, make_config

from telegram_net.alerts import AlertRelay
from telegram_net.config import ChannelConfig
from telegram_net.neptun import MapView, Threat, ThreatBook, bearing_deg, compass, describe_threat, haversine_km, nearby, parse_threat, render_caption

BERDYCHIV = MapView(49.8994, 28.6025, 120, "Бердичева")


def raw(id="t1", lat=50.9543, lon=28.6447, **extra):
    base = {"id": id, "type": "uav", "title": "БпЛА", "region": "Житомирська область", "locality": "Коростень", "lat": lat, "lon": lon, "status": "active"}
    return {**base, **extra}


def threat(**kw):
    return parse_threat(raw(**kw))


def test_distance_and_bearing_from_berdychiv_to_korosten():
    d = haversine_km(49.8994, 28.6025, 50.9543, 28.6447)
    assert 115 < d < 120
    assert compass(bearing_deg(49.8994, 28.6025, 50.9543, 28.6447)) == "північ"
    assert compass(bearing_deg(49.8994, 28.6025, 49.5, 29.5)) == "південний схід"
    assert compass(300, genitive_from=True) == "північного заходу"


def test_nearby_filters_by_radius_and_expiry_and_sorts():
    far = threat(id="far", lat=48.0, lon=35.0)  # Дніпропетровщина
    old = threat(id="old", lat=50.0, lon=28.6, expiresAt="2026-11-14T12:00:00Z")
    close = threat(id="close", lat=49.95, lon=28.65, expiresAt="2026-11-14T13:10:00Z")
    kor = threat(id="kor")
    found = nearby([far, old, kor, close], BERDYCHIV, NOW)
    assert [t.id for t in found] == ["close", "kor"]
    assert found[0].distance_km < found[1].distance_km


def test_describe_threat_with_target_heading_and_direction():
    t = nearby([threat(to={"name": "Київ", "dir8": "N"}, **{"from": {"name": "Чернігівська область", "kind": "direction"}})], BERDYCHIV, NOW)[0]
    text = describe_threat(t, BERDYCHIV)
    assert text == "• БпЛА — Коростень (Житомирська обл.), ≈115 км на північ від Бердичева; курс на Київ (із напрямку: Чернігівська область)"
    by_heading = nearby([threat(heading=180.0)], BERDYCHIV, NOW)[0]
    assert describe_threat(by_heading, BERDYCHIV).endswith("; рухається на південь")
    group = nearby([threat(title="Група БпЛА", count=3)], BERDYCHIV, NOW)[0]
    assert "Група БпЛА ×3" in describe_threat(group, BERDYCHIV)
    only_from = nearby([threat(**{"from": {"name": "Київська область"}})], BERDYCHIV, NOW)[0]
    assert describe_threat(only_from, BERDYCHIV).endswith("; із напрямку: Київська область")


def test_no_course_when_target_is_the_same_place():
    t = nearby([threat(to={"name": "Коростень"}, **{"from": {"name": "Київська область"}})], BERDYCHIV, NOW)[0]
    assert describe_threat(t, BERDYCHIV).endswith("від Бердичева; із напрямку: Київська область")


def test_caption_fits_budget_and_reports_overflow():
    many = nearby([threat(id=str(i), lat=49.9 + i * 0.01) for i in range(9)], BERDYCHIV, NOW)
    caption = render_caption(many, BERDYCHIV, NOW, 600)
    assert len(caption) <= 600 and "…і ще 4 на мапі" in caption and "Нептуна" in caption
    empty = render_caption([], BERDYCHIV, NOW, 600)
    assert "цілей не видно" in empty


def test_threat_book_applies_snapshot_then_upserts_and_removals():
    book = ThreatBook()
    book.feed(json.dumps({"type": "snapshot", "data": {"threats": [raw("a"), raw("b", status="expired")]}}))
    assert [t.id for t in book.threats()] == ["a"]  # неактивні відсіюються
    book.feed(json.dumps({"type": "batch", "data": {"threats": {"upsert": [raw("c", lat=50.0)]}}}))
    book.feed(json.dumps({"type": "batch", "data": {"threats": {"remove": ["a"]}}}))
    assert [t.id for t in book.threats()] == ["c"]
    book.feed("не json")  # сміття не ламає потік
    book.feed(json.dumps({"type": "heartbeat"}))
    assert [t.id for t in book.threats()] == ["c"]


# --- інтеграція з циклом тривог ---

class Sender:
    def __init__(self):
        self.texts, self.photos = [], []

    def send_text(self, chat_id, text):
        self.texts.append((chat_id, text))
        return 1

    def send_photo_bytes(self, chat_id, image, caption=None, filename="image.png"):
        self.photos.append((chat_id, image, caption))
        return 2


def berdychiv_config():
    cfg = make_config()
    ch = ChannelConfig(
        "berdychiv", "Бердичів", "@myberdychivnews", alert_district="Бердичівський",
        subscribe_cta="Підписатися на «Мій Бердичів»", name_genitive="Бердичева",
        neptun={"lat": 49.8994, "lon": 28.6025, "radius_km": 120},
    )
    return dataclasses.replace(cfg, channels=[*cfg.channels, ch])


def run_alert(relay, msg_id, text, now=NOW):
    relay.process([item(msg_id, text, minutes_ago=0)], now)
    for f in relay.map_futures:
        f.result(timeout=5)


def test_alert_sends_text_at_once_then_map_with_description():
    sender, calls = Sender(), []

    def capturer(view):
        calls.append(view)
        return b"PNG", [threat(to={"name": "Київ"})]

    relay = AlertRelay(berdychiv_config(), sender, capturer=capturer)
    relay.process([item(1, "Старе повідомлення", minutes_ago=20)], NOW)  # старт — тихо
    run_alert(relay, 2, "🟡 Бердичівський район — повітряна тривога, жовтий рівень: Дронова загроза")
    assert len(sender.texts) == 1 and "Повітряна тривога" in sender.texts[0][1]
    assert len(sender.photos) == 1
    chat, png, caption = sender.photos[0]
    assert chat == "@myberdychivnews" and png == b"PNG"
    assert "Що на мапі поблизу Бердичева" in caption and "курс на Київ" in caption and "Мій Бердичів" in caption
    assert calls[0].place_genitive == "Бердичева" and calls[0].radius_km == 120


def test_no_map_when_no_threats_nearby_or_capturer_fails_and_alert_still_sent():
    sender = Sender()
    relay = AlertRelay(berdychiv_config(), sender, capturer=lambda v: (b"PNG", [threat(lat=48.0, lon=35.0)]))
    relay.process([item(1, "x", minutes_ago=20)], NOW)
    run_alert(relay, 2, "🟡 Бердичівський район — повітряна тривога, жовтий рівень")
    assert len(sender.texts) == 1 and sender.photos == []

    def boom(view):
        raise RuntimeError("браузер впав")

    sender2 = Sender()
    relay2 = AlertRelay(berdychiv_config(), sender2, capturer=boom)
    relay2.process([item(1, "x", minutes_ago=20)], NOW)
    run_alert(relay2, 2, "🟡 Бердичівський район — повітряна тривога, жовтий рівень")
    assert len(sender2.texts) == 1 and sender2.photos == []  # сигнал не постраждав


def test_map_only_on_alert_start_with_cooldown_and_not_on_clear():
    sender, calls = Sender(), []

    def capturer(view):
        calls.append(1)
        return b"PNG", [threat()]

    relay = AlertRelay(berdychiv_config(), sender, capturer=capturer)
    relay.process([item(1, "x", minutes_ago=20)], NOW)
    run_alert(relay, 2, "🟡 Бердичівський район — повітряна тривога, жовтий рівень")
    run_alert(relay, 3, "🔴 Бердичівський район — повітряна тривога, червоний рівень: ракетна загроза")  # тривога вже триває
    assert len(calls) == 1
    run_alert(relay, 4, "🟢 Бердичівський район — відбій повітряної тривоги")
    assert len(calls) == 1
    # нова тривога за 5 хв — у межах кулдауну; за 30 хв — знову знімок
    later = NOW + dt.timedelta(minutes=5)
    relay.process([item(5, "🟡 Бердичівський район — повітряна тривога, жовтий рівень", minutes_ago=-5)], later)
    for f in relay.map_futures: f.result(timeout=5)
    assert len(calls) == 1
    relay.process([item(6, "🟢 Бердичівський район — відбій повітряної тривоги", minutes_ago=-5)], later)
    much_later = NOW + dt.timedelta(minutes=30)
    relay.process([item(7, "🟡 Бердичівський район — повітряна тривога, жовтий рівень", minutes_ago=-30)], much_later)
    for f in relay.map_futures: f.result(timeout=5)
    assert len(calls) == 2


def test_channels_without_neptun_config_get_no_map():
    sender, calls = Sender(), []
    relay = AlertRelay(make_config(), sender, capturer=lambda v: calls.append(v) or (b"", []))
    relay.process([item(1, "x", minutes_ago=20)], NOW)
    run_alert(relay, 2, "🟡 Коростенський район — повітряна тривога, жовтий рівень")
    assert calls == [] and len(sender.texts) == 1
