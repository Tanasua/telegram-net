import datetime as dt
from io import BytesIO
from zoneinfo import ZoneInfo

from helpers import make_config

from telegram_net.attack import (
    build_report,
    caption,
    is_threat,
    oblasts_in,
    parse_summary,
    post_caption,
    publish_attack,
    render,
    site_for,
)
from telegram_net.fetchers.base import FetchedItem
from telegram_net.state import State

KYIV = ZoneInfo("Europe/Kyiv")

SUMMARY = """⚡️ ЗБИТО/ПОДАВЛЕНО 106 ВОРОЖИХ ЦІЛЕЙ

У ніч на 15 листопада (з 18:00 14 листопада) противник атакував 132 ударними БпЛА типу Shahed (78 із них - реактивні), Гербера, дронами-імітаторами типу “Пародія” із напрямків: Брянськ, Орел, Курськ, Міллерово, Приморсько-Ахтарськ- рф, ТОТ Донецьк, ТОТ АР Крим – Гвардійське.

Повітряний напад відбивали авіація, зенітні ракетні війська, підрозділи РЕБ.

💥 За попередніми даними, станом на 08:00, протиповітряною обороною збито/подавлено 106 ворожих БпЛА типу Shahed (61 із них - реактивний).

Зафіксовано влучання засобів повітряного нападу противника на 20 локаціях."""

MISSILES = """У ніч на 3 листопада противник атакував 4 балістичними ракетами Іскандер-М, 20 крилатими ракетами Х-101 та 90 ударними БпЛА із напрямків: Курськ, Брянськ.

Повітряний напад відбивали.
Збито/подавлено 95 цілей."""


def _item(msg_id, text, hour, minute=0, day=15):
    return FetchedItem(
        external_id=str(msg_id),
        text=text,
        url=f"https://t.me/kpszsu/{msg_id}",
        published_at=dt.datetime(2026, 11, day, hour, minute, tzinfo=KYIV),
    )


def test_summary_numbers_and_directions():
    s = parse_summary(_item(1, SUMMARY, 8, 1), KYIV)
    assert (s.night_of, s.date) == ("15 листопада", dt.date(2026, 11, 15))
    assert (s.drones, s.jet_drones, s.missiles, s.downed, s.hits) == (132, 78, None, 106, 20)
    assert s.directions[:5] == ["Брянськ", "Орел", "Курськ", "Міллерово", "Приморсько-Ахтарськ"]
    assert "Донецьк" in s.directions and {site_for(d) for d in s.directions} >= {"Крим", "Донецьк", "Брянськ"}


def test_summary_with_missiles_sums_them():
    s = parse_summary(_item(2, MISSILES, 8, day=3), KYIV)
    assert (s.missiles, s.drones, s.downed) == (24, 90, 95)


def test_not_a_summary():
    assert parse_summary(_item(3, "🏍 Житомир: Реактивний БпЛА курсом на місто з півночі", 2), KYIV) is None


def test_oblast_detection_and_threat_filter():
    assert oblasts_in("🏍 Реактивний БпЛА з Київщини на Житомирщину (Коростень)") == {"UA-32", "UA-18"}
    assert oblasts_in("🏍 Кременчук: Реактивний БпЛА курсом на місто") == {"UA-53"}
    assert oblasts_in("🏍 Харків: Ударний БпЛА курсом на місто з півночі") == {"UA-63"}
    assert is_threat("🏍 Одещина: Реактивний БпЛА курсом на Ананьїв.")
    assert is_threat("🚀 Крилаті ракети курсом на Київщину")
    assert not is_threat("💣 КАБи на Сумщину.")  # керовані бомби біля фронту — не на карту
    assert not is_threat("✊ Тримаймо небо!")


def test_report_counts_night_messages_and_zhytomyr_places():
    items = [
        _item(10, "🏍 Чернігівщина: реактивний БпЛА повз Славутич курсом на Київщину.", 19, day=14),
        _item(11, "🏍 Реактивний БпЛА з Київщини на Житомирщину (Коростень)", 2),
        _item(12, "🏍 Житомир: Реактивний БпЛА курсом на місто з півночі", 3),
        _item(13, "🏍 Житомирщина: БпЛА курсом на Малин/Коростень", 4),
        _item(14, "💣 КАБи на Сумщину.", 5),
        _item(15, "🏍 Київщина: БпЛА на Бровари", 15, day=14),  # до 18:00 — це вже не ця ніч
        _item(16, SUMMARY, 8, 1),
    ]
    r = build_report(items, KYIV, dt.date(2026, 11, 15))
    assert r.messages == 4
    assert r.oblast_counts["UA-18"] == 3 and r.oblast_counts["UA-32"] == 2 and r.oblast_counts["UA-74"] == 1
    assert "UA-59" not in r.oblast_counts
    assert r.zhytomyr_places[0] == "Коростень" and "Житомир" in r.zhytomyr_places and "Малин" in r.zhytomyr_places
    text = caption(r)
    assert text.startswith("У ніч на 15 листопада росія атакувала Україну: 132 ударні БпЛА (з них 78 реактивних).")
    assert "Житомирщина фігурувала в 3 повідомленнях" in text
    assert post_caption(r, make_config().channels[0]).startswith("<b>Нічна атака: підсумок</b>\n")


def test_no_summary_yet_means_no_report():
    assert build_report([_item(11, "🏍 БпЛА на Житомирщину", 2)], KYIV, dt.date(2026, 11, 15)) is None


def test_render_is_portrait_png():
    from PIL import Image

    r = build_report([_item(11, "🏍 Реактивний БпЛА з Київщини на Житомирщину (Коростень)", 2), _item(16, SUMMARY, 8, 1)], KYIV, dt.date(2026, 11, 15))
    png = render(r, "Мій Житомир")
    assert Image.open(BytesIO(png)).size == (1080, 1350)


class FakePhotoPublisher:
    def __init__(self):
        self.sent = []

    def send_photo_bytes(self, chat_id, image, caption=None, filename="x.png"):
        self.sent.append(chat_id)
        return len(self.sent)


NOW = dt.datetime(2026, 11, 15, 6, 40, tzinfo=dt.timezone.utc)  # 08:40 за Києвом


def _feed(with_summary=True):
    items = [_item(11, "🏍 Реактивний БпЛА з Київщини на Житомирщину (Коростень)", 2)]
    if with_summary:
        items.append(_item(16, SUMMARY, 8, 1))
    return lambda since: items


def test_publish_once_a_day_after_summary(tmp_path):
    config, state, publisher = make_config(), State(tmp_path), FakePhotoPublisher()

    early = publish_attack(config, state, publisher, now=NOW - dt.timedelta(minutes=30), fetch=_feed())
    assert publisher.sent == [] and "пропускаю" in early[0]  # 08:10 — ще рано

    waiting = publish_attack(config, state, publisher, now=NOW, fetch=_feed(with_summary=False))
    assert publisher.sent == [] and "ще немає" in waiting[0]

    publish_attack(config, state, publisher, now=NOW, fetch=_feed())
    assert publisher.sent == ["@mymalyn", "@myzhytomyr"]
    assert [p["kind"] for p in state.published] == ["attack", "attack"]
    assert state.channel_post_count("malyn") == 0  # не забирає ліміт і не зсуває ротації

    again = publish_attack(config, state, publisher, now=NOW + dt.timedelta(hours=1), fetch=_feed())
    assert len(publisher.sent) == 2 and "вже був" in again[0]


def test_dry_run_writes_images(tmp_path):
    lines = publish_attack(make_config(), State(tmp_path), None, now=NOW, out_dir=tmp_path / "out", fetch=_feed())
    assert (tmp_path / "out" / "attack_malyn.png").exists() and "[пробний]" in lines[0]


def test_northern_launch_site_stays_below_the_stat_tiles():
    """Шаталово лежить північніше за плитки з цифрами: точка й підпис не мають налазити на них."""
    from telegram_net.attack import LAUNCH_SITES

    assert "Шаталово" in LAUNCH_SITES
    summary = SUMMARY.replace("Брянськ, Орел", "Шаталово, Брянськ, Орел")
    r = build_report([_item(16, summary, 8, 1)], KYIV, dt.date(2026, 11, 15))
    assert "Шаталово" in r.summary.directions
    png = render(r, "Мій Житомир")  # не падає, коли підпис ставиться збоку
    assert png.startswith(b"\x89PNG")
