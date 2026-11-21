import dataclasses
import datetime as dt
import json

from helpers import NOW, item, make_config

from telegram_net.alerts import AlertRelay
from telegram_net.config import ChannelConfig
from telegram_net.neptun_view import MapView, Threat, ThreatBook, bearing_deg, compass, describe_threat, haversine_km, nearby, parse_threat, render_caption

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
    assert len(caption) <= 600 and "…і ще 4 на мапі" in caption and "http" not in caption  # без посилань і застережень
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
        self.photos = []  # (chat, фото чи список фото, підпис)

    def send_photo_bytes(self, chat_id, image, caption=None, filename="image.png"):
        self.photos.append((chat_id, image, caption))
        return 1

    def send_photo_group_bytes(self, chat_id, images, caption=None):
        self.photos.append((chat_id, list(images), caption))
        return [2, 3]


def berdychiv_config():
    cfg = make_config()
    ch = ChannelConfig(
        "berdychiv", "Бердичів", "@myberdychivnews", alert_district="Бердичівський",
        subscribe_cta="Підписатися на «Мій Бердичів»", name_genitive="Бердичева",
        neptun={"lat": 49.8994, "lon": 28.6025, "radius_km": 120, "wide_radius_km": 350},
    )
    return dataclasses.replace(cfg, channels=[*cfg.channels, ch])


ALERT = "🟡 Бердичівський район — повітряна тривога, жовтий рівень: Дронова загроза"


def start_alert(relay, text=ALERT, msg_id=2):
    relay.process([item(1, "старе", minutes_ago=20)], NOW)  # старт — тихо
    relay.process([item(msg_id, text, minutes_ago=0)], NOW)


def album(chat_photos):
    return [p for p in chat_photos if isinstance(p[1], list)]


def test_berdychiv_gets_local_album_with_description_instead_of_generic_map():
    sender, generic_calls, views = Sender(), [], []

    def local(view):
        views.append(view)
        return [b"CLOSE", b"WIDE"], [threat(to={"name": "Київ"})]

    relay = AlertRelay(berdychiv_config(), sender, map_provider=lambda: generic_calls.append(1) or b"GENERIC", local_maps=local)
    start_alert(relay)
    maps = [p for p in sender.photos if p[2] and "Що на мапі" in p[2]]
    assert len(maps) == 1 and maps[0][0] == "@myberdychivnews"
    assert maps[0][1] == [b"CLOSE", b"WIDE"]  # альбом: крупний і широкий кадри
    assert "курс на Київ" in maps[0][2] and "Нептуна" not in maps[0][2] and "http" not in maps[0][2]
    assert views[0].radius_km == 120 and views[0].wide_radius_km == 350 and views[0].place_genitive == "Бердичева"
    assert generic_calls == []  # загальний знімок цьому каналу не потрібен


def test_falls_back_to_generic_map_when_no_threats_nearby_or_local_capture_fails():
    sender = Sender()
    relay = AlertRelay(berdychiv_config(), sender, map_provider=lambda: b"GENERIC", local_maps=lambda v: ([b"X"], [threat(lat=48.0, lon=35.0)]))
    start_alert(relay)
    assert [p[1] for p in sender.photos if p[1] == b"GENERIC"] == [b"GENERIC"]  # цілей поблизу немає

    def boom(view):
        raise RuntimeError("браузер впав")

    sender2 = Sender()
    relay2 = AlertRelay(berdychiv_config(), sender2, map_provider=lambda: b"GENERIC", local_maps=boom)
    start_alert(relay2)
    assert any(p[1] == b"GENERIC" for p in sender2.photos)  # і сигнал, і загальна карта на місці


def test_channels_without_neptun_config_keep_generic_map_only():
    sender, local_calls = Sender(), []
    relay = AlertRelay(make_config(), sender, map_provider=lambda: b"GENERIC", local_maps=lambda v: local_calls.append(v) or ([], []))
    start_alert(relay, "🟡 Коростенський район — повітряна тривога, жовтий рівень")
    assert local_calls == [] and any(p[1] == b"GENERIC" for p in sender.photos)


def test_local_map_only_at_alert_start_and_not_on_clear():
    sender, calls = Sender(), []

    def local(view):
        calls.append(1)
        return [b"A"], [threat()]

    relay = AlertRelay(berdychiv_config(), sender, local_maps=local)
    start_alert(relay)
    relay.process([item(3, "🔴 Бердичівський район — повітряна тривога, червоний рівень: ракетна загроза", minutes_ago=0)], NOW)  # вже триває
    relay.process([item(4, "🟢 Бердичівський район — відбій повітряної тривоги", minutes_ago=0)], NOW)
    assert len(calls) == 1
