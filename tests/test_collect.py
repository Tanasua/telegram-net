import datetime as dt

from helpers import NOW, FakeFetcher, item, make_config, src

import pytest

import telegram_net.collect as collect_module
from telegram_net.clock import is_quiet
from telegram_net.collect import (
    address_hint,
    brief_text,
    build_brief,
    collect,
    is_alert_signal,
    is_noise,
    nothing_to_do,
    rubrics_due,
    strip_footer,
)
from telegram_net.fetchers.article import Article
from telegram_net.state import State, iso


@pytest.fixture(autouse=True)
def short_texts_allowed(monkeypatch):
    """У тестах тексти короткі для читабельності; сам поріг перевіряє окремий тест."""
    monkeypatch.setattr(collect_module, "MIN_SOCIAL_CHARS", 20)


def _run(tmp_path, sources, items_by_handle, articles=None, state=None, config=None, **kw):
    config = config or make_config(sources)
    state = state or State(tmp_path)
    fetcher = FakeFetcher(items_by_handle)
    fetchers = {"telegram": fetcher, "website": fetcher, "web_scrape": fetcher}
    brief = collect(config, state, fetchers, articles, now=NOW, **kw)
    return brief, state, fetcher


def test_new_item_becomes_candidate_with_media_and_age(tmp_path):
    s = src("А")
    brief, state, _ = _run(tmp_path, [s], {"@А": [item(1, "У Малині відкрили оновлений міст через Іршу", media=["https://cdn/p.jpg"])]})
    (cand,) = brief["candidates"]
    assert cand["scope"] == "local" and cand["channel_id"] == "malyn" and cand["age_minutes"] == 30
    assert cand["media"] == [{"type": "photo", "url": "https://cdn/p.jpg"}]
    assert state.sources[s.key]["cursor"] == "next" and state.sources[s.key]["fails"] == 0
    assert brief["stats"]["new"] == 1


def test_stale_alert_stop_topic_empty_and_duplicate_are_filtered(tmp_path):
    brief, _, _ = _run(
        tmp_path,
        [src("А"), src("Б")],
        {
            "@А": [
                item(1, "Стара новина про ремонт дороги в центрі міста", minutes_ago=13 * 60),
                item(2, "🟡 Коростенський район — повітряна тривога, жовтий рівень: Дронова загроза"),
                item(3, "У Малині працівники ТЦК перевіряли документи на ринку"),
                item(4, "Фото"),
                item(5, "У Малині відкрили оновлений міст через Іршу після ремонту"),
            ],
            # Той самий текст з іншого джерела (інша пунктуація/регістр) — точний дубль.
            "@Б": [item(9, "у малині відкрили оновлений міст через іршу після ремонту!")],
        },
    )
    assert [c["text"] for c in brief["candidates"]] == ["У Малині відкрили оновлений міст через Іршу після ремонту"]
    stats = brief["stats"]
    assert (stats["stale"], stats["alert"], stats["stop_topic"], stats["empty"], stats["duplicate"]) == (1, 1, 1, 1, 1)


def test_alert_detection_does_not_swallow_long_news():
    assert is_alert_signal("🟢 Житомирський район — відбій повітряної тривоги")
    assert is_alert_signal("Відбій повітряної тривоги. Житомир. Реактивний БпЛА.")
    long_news = "За минулу добу в області п'ять разів оголошували повітряну тривогу. " + "Подробиці атаки та наслідки. " * 15
    assert not is_alert_signal(long_news)


def test_oblast_source_is_read_once_and_not_bound_to_channel(tmp_path):
    police = src("Поліція", scope="oblast", handle="@zt_police")
    brief, _, fetcher = _run(tmp_path, [police, src("Місцеве")], {"@zt_police": [item(1, "Поліція затримала крадія в Коростені")]})
    assert [h for h, _ in fetcher.calls].count("@zt_police") == 1
    (cand,) = brief["candidates"]
    assert cand["scope"] == "oblast" and cand["channel_id"] is None


def test_sources_of_inactive_channel_and_unsupported_types_are_skipped(tmp_path):
    _, _, fetcher = _run(
        tmp_path,
        [src("Вимк", channel_id="off"), src("FB", type="facebook_page"), src("Ок")],
        {},
    )
    assert [h for h, _ in fetcher.calls] == ["@Ок"]


def test_failing_source_does_not_stop_run_and_is_reported(tmp_path):
    bad = src("Зламане")
    state = State(tmp_path)
    for _ in range(3):
        brief, state, _ = _run(tmp_path, [bad, src("Ок")], {"@Зламане": RuntimeError("403"), "@Ок": []}, state=state)
    assert state.sources[bad.key]["fails"] == 3
    assert brief["stats"]["sources_ok"] == 1 and brief["stats"]["sources_failed"] == 1
    assert any("Зламане" in p and "3 збоїв" in p for p in brief["problems"])


def test_website_item_is_enriched_with_article_text_image_and_date(tmp_path):
    class Articles:
        def fetch(self, url):
            return Article(text="Роботи коштували 12 мільйонів гривень.", image_url="https://site/photo.jpg", published_at=NOW - dt.timedelta(hours=1))

    site = src("Сайт", type="web_scrape", handle="https://site/news")
    brief, _, _ = _run(tmp_path, [site], {"https://site/news": [item(1, "У Малині відкрили міст", dated=False, url="https://site/1")]}, articles=Articles())
    (cand,) = brief["candidates"]
    assert cand["text"] == "У Малині відкрили міст\n\nРоботи коштували 12 мільйонів гривень."
    assert cand["media"] == [{"type": "photo", "url": "https://site/photo.jpg"}] and cand["age_minutes"] == 60


def test_article_date_makes_old_scraped_item_stale(tmp_path):
    class Articles:
        def fetch(self, url):
            return Article(text="Текст статті, якій уже три дні.", image_url=None, published_at=NOW - dt.timedelta(days=3))

    site = src("Сайт", type="web_scrape", handle="https://site/news")
    brief, _, _ = _run(tmp_path, [site], {"https://site/news": [item(1, "Давня новина зі сторінки", dated=False)]}, articles=Articles())
    assert brief["candidates"] == [] and brief["stats"]["stale"] == 1


def test_logo_and_repeated_image_are_not_used_as_news_photo(tmp_path):
    brief, _, _ = _run(
        tmp_path,
        [src("А")],
        {
            "@А": [
                item(1, "Перша новина про подію в місті сьогодні", media=["https://site/images/logo_small.png"]),
                item(2, "Друга новина про зовсім іншу подію в місті", media=["https://site/og-share.jpg"]),
                item(3, "Третя новина, знову з тією самою картинкою", media=["https://site/og-share.jpg", "https://site/real3.jpg"]),
            ]
        },
    )
    media = {c["text"][:6]: [m["url"] for m in c["media"]] for c in brief["candidates"]}
    assert media == {"Перша ": [], "Друга ": [], "Третя ": ["https://site/real3.jpg"]}


def test_national_source_is_capped_to_freshest_items(tmp_path):
    nat = src("Агенція", scope="national")
    items = [item(i, f"Всеукраїнська новина номер {i} про важливу подію", minutes_ago=100 - i) for i in range(1, 11)]
    brief, _, _ = _run(tmp_path, [nat], {"@Агенція": items})
    assert len(brief["candidates"]) == 4 and brief["stats"]["capped"] == 6
    assert "номер 10" in brief["candidates"][0]["text"]  # найсвіжіша — перша


def test_rss_markup_leftovers_are_cleaned(tmp_path):
    raw = "Заголовок новини про пожежу\n\nВогонь знищив речі. <p>The post Заголовок first appeared on Zhitomir-OnLine .</p>"
    brief, _, _ = _run(tmp_path, [src("А")], {"@А": [item(1, raw)]})
    assert brief["candidates"][0]["text"] == "Заголовок новини про пожежу\n\nВогонь знищив речі."


def test_pool_entries_expire(tmp_path):
    state = State(tmp_path)
    state.pool["old"] = {"id": "old", "added_at": iso(NOW - dt.timedelta(hours=11)), "published_at": None, "text": "x", "media": []}
    state.pool["fresh"] = {"id": "fresh", "added_at": iso(NOW - dt.timedelta(hours=1)), "published_at": None, "text": "y", "media": []}
    brief, state, _ = _run(tmp_path, [], {}, state=state)
    assert list(state.pool) == ["fresh"]


def test_brief_shows_limits_and_live_channel_posts(tmp_path):
    config = make_config([])
    state = State(tmp_path)
    for kind in ("local", "national", "alert"):
        state.log_post({"channel_id": "malyn", "published_at": iso(NOW - dt.timedelta(hours=2)), "kind": kind, "text": f"пост {kind}", "candidate_ids": []})
    live = {"malyn": [item(1, "Пост, опублікований вручну"), item(2, "🟢 Коростенський район — відбій повітряної тривоги")]}
    brief = build_brief(config, state, NOW, live_posts=live)
    malyn, zhytomyr = brief["channels"]
    assert (malyn["posts_today"], malyn["remaining_today"], malyn["national_remaining_today"]) == (2, 10, 3)
    assert [p["text"] for p in malyn["recent_posts"]] == ["Пост, опублікований вручну"]
    assert zhytomyr["posts_today"] == 0 and zhytomyr["recent_posts"] == []
    assert brief["quiet_hours_now"] is False and brief["local_time"].startswith("2026-11-14 15:00")


def test_quiet_hours_and_rubrics(tmp_path):
    config = make_config([])
    kyiv = lambda h: dt.datetime(2026, 11, 14, h - 2, 0, tzinfo=dt.timezone.utc)  # noqa: E731 — взимку Київ = UTC+2
    assert is_quiet(config, kyiv(23)) and is_quiet(config, kyiv(3)) and not is_quiet(config, kyiv(7)) and not is_quiet(config, kyiv(22))

    state = State(tmp_path)
    malyn = config.channel("malyn")
    assert rubrics_due(config, state, malyn, kyiv(8)) == ["morning"]
    assert rubrics_due(config, state, malyn, kyiv(21)) == []  # за день нічого не виходило — підсумку немає з чого робити
    for i in range(3):
        state.log_post({"channel_id": "malyn", "published_at": iso(kyiv(10 + i)), "kind": "local", "text": "т", "candidate_ids": []})
    assert rubrics_due(config, state, malyn, kyiv(21)) == ["evening"]
    state.log_post({"channel_id": "malyn", "published_at": iso(kyiv(20)), "kind": "evening", "text": "т", "candidate_ids": []})
    assert rubrics_due(config, state, malyn, kyiv(21)) == []
    friday = dt.datetime(2026, 11, 13, 14, 0, tzinfo=dt.timezone.utc)
    assert rubrics_due(config, state, malyn, friday) == ["afisha"]


def test_full_article_replaces_rss_teaser_but_keeps_title(tmp_path):
    body = "У Бердичівській міській лікарні стартували роботи з утеплення фасаду. " + "Подробиці ремонту й терміни. " * 10

    class Articles:
        def fetch(self, url):
            return Article(text=body.strip(), image_url=None, published_at=None)

    site = src("Сайт", type="website", handle="https://site/feed")
    rss = "У лікарні розпочали утеплення фасаду\n\nУ Бердичівській міській лікарні стартували роботи […] Запис У лікарні розпочали утеплення фасаду спершу з'явиться на Бердичів.biz ."
    brief, _, _ = _run(tmp_path, [site], {"https://site/feed": [item(1, rss)]}, articles=Articles())
    text = brief["candidates"][0]["text"]
    assert text.startswith("У лікарні розпочали утеплення фасаду\n\nУ Бердичівській міській лікарні стартували роботи з утеплення фасаду.")
    assert "Запис" not in text and "[…]" not in text and text.count("стартували роботи") == 1


def test_address_hint_comes_every_third_post_and_rotates(tmp_path):
    config = make_config([], addresses=("Котики", "Друзі", "Сонечки"))
    state = State(tmp_path)
    malyn, zhytomyr = config.channel("malyn"), config.channel("zhytomyr")
    hints = []
    for _ in range(9):
        hints.append(address_hint(config, state, malyn))
        state.log_post({"channel_id": "malyn", "published_at": iso(NOW), "kind": "local", "text": "т", "candidate_ids": []})
    assert [h is not None for h in hints] == [True, False, False] * 3
    assert sorted(h for h in hints if h) == ["Друзі", "Котики", "Сонечки"]  # по колу, без повторів
    # Сигнали тривоги лічильник не зсувають; вимкнений список — жодних підказок.
    state.log_post({"channel_id": "zhytomyr", "published_at": iso(NOW), "kind": "alert", "text": "т", "candidate_ids": []})
    assert address_hint(config, state, zhytomyr) is not None
    state.log_post({"channel_id": "zhytomyr", "published_at": iso(NOW), "kind": "local", "text": "т", "candidate_ids": []})
    assert address_hint(config, state, zhytomyr) is None
    assert address_hint(make_config([], addresses=()), state, malyn) is None
    assert "address_hint" in build_brief(config, state, NOW)["channels"][0]


def test_noise_is_filtered_by_code_before_the_editor_sees_it(tmp_path, monkeypatch):
    monkeypatch.setattr(collect_module, "MIN_SOCIAL_CHARS", 80)
    noise = [
        "Житомирщина, повітряна загроза станом на 21:42\n\nРеактивний БпЛА. Курс: Малин.",
        "⚠️Реактивний БпЛА в напрямку Малина",
        "🗓 15 листопада, неділя\n✔️ ще один день до Перемоги\n✔️ День працівників сільського господарства",
        "Доброго ранку🤗! Неділя — не для того, щоб переживати через незроблене за тиждень. Видихніть.",
        "Загальні бойові втрати противника з 24.02.22 по 15.11.26",
    ]
    assert all(is_noise(t) for t in noise)
    real = "У Радомишлі прорвало дамбу пивзаводу: стоки потрапили в річку Тетерів. Екологи вже відібрали проби води й ґрунту."
    assert not is_noise(real)
    # Довгий матеріал, де таке слово трапилось побіжно, — це вже новина.
    assert not is_noise("Вночі оголошували тривогу: реактивний БпЛА курсом на місто. " + "Подробиці наслідків атаки. " * 30)

    items = [item(i, t) for i, t in enumerate(noise)] + [item(50, "Перелітає 🛸"), item(51, real)]
    brief, _, _ = _run(tmp_path, [src("А")], {"@А": items})
    assert [c["text"] for c in brief["candidates"]] == [real]
    assert brief["stats"]["noise"] == 5 and brief["stats"]["empty"] == 1


def test_source_footers_are_stripped():
    text = "🚒 У Житомирі горіла квартира\n\nОбійшлося без травмованих.\n\n❤️Телеграм канал PZHYTOMYR |\nПідписатися | Надіслати новину🆘"
    assert strip_footer(text) == "🚒 У Житомирі горіла квартира\n\nОбійшлося без травмованих."
    assert strip_footer("Вибухи в Дніпрі.\n\n📲 Підпишись на Укрінформ | Facebook | X | YouTube") == "Вибухи в Дніпрі."
    assert strip_footer("Мер закликав мешканців підписати петицію щодо ремонту мосту через річку.") .startswith("Мер закликав")


def test_text_brief_is_compact_and_points_to_full_text(tmp_path):
    long_text = "У Малині відкрили оновлений міст через Іршу. " + "Деталь ремонту номер такий-то. " * 60
    state = State(tmp_path)
    state.log_post({"channel_id": "malyn", "published_at": iso(NOW), "kind": "local", "text": "Учора в Малині " + "щось сталося " * 40, "candidate_ids": []})
    brief, state, _ = _run(tmp_path, [src("А")], {"@А": [item(1, long_text, media=["https://cdn/very/long/url/photo.jpg"])]}, state=state)
    text = brief_text(brief)
    cid = brief["candidates"][0]["id"]
    assert f"[{cid}] local→malyn · медіа · А · 30 хв · фото 1" in text
    assert f"повний текст: show {cid}" in text and "https://cdn" not in text
    assert "вже було" in text and len(text) < 1800
    assert state.pool[cid]["text"].count("Деталь") > 40  # повний текст лишився в пулі
    assert not nothing_to_do(brief)


def test_nothing_to_do_when_no_candidates_and_no_rubrics(tmp_path):
    brief, _, _ = _run(tmp_path, [src("А")], {"@А": []})
    assert nothing_to_do(brief)  # 15:00, субота: рубрик немає
    morning = build_brief(make_config([]), State(tmp_path), dt.datetime(2026, 11, 14, 6, 0, tzinfo=dt.timezone.utc))
    assert not nothing_to_do(morning)  # 08:00 — час ранкової рубрики


def test_press_releases_from_official_sources_are_dropped_but_incidents_kept(tmp_path):
    police = src("Поліція", scope="oblast", handle="@zt_police", kind="official")
    public = src("Паблік", handle="@public", kind="public")
    pr = [
        "⚠️ Сапери ДСНС: підозрілу знахідку не чіпайте — телефонуйте 101. Сапери продовжують пошук вибухонебезпечних предметів.",
        "🩸 Донорство крові — важливий внесок у порятунок життів. Ми спільно з поліцією охорони завітали до центру крові.",
        "Мобільний клас безпеки: поліцейські навчають ліцеїстів важливих правил безпеки.",
    ]
    incident = "У Коростені поліцейські затримали чоловіка, який викрав велосипед біля магазину. Йому повідомлено про підозру."
    items_police = [item(i, t) for i, t in enumerate(pr)] + [item(9, incident)]
    # Та сама «нудятина» в пабліку — не фільтр коду, а смак редактора.
    brief, _, _ = _run(tmp_path, [police, public], {"@zt_police": items_police, "@public": [item(20, pr[0] + " (паблік)")]})
    texts = [c["text"] for c in brief["candidates"]]
    assert incident in texts and not any("Донорство" in t or "Мобільний клас" in t for t in texts)
    assert brief["stats"]["press_release"] + brief["stats"]["capped"] == 3  # найстаріше відсік ліміт офіційних
    # Паблики — першими в брифі.
    assert brief["candidates"][0]["kind"] == "public"
    assert "· паблік ·" in brief_text(brief)


def test_official_sources_are_capped_to_freshest(tmp_path):
    rada = src("Рада", kind="official")
    items = [item(i, f"Рішення виконкому номер {i} про ремонт дороги на вулиці", minutes_ago=100 - i) for i in range(1, 8)]
    brief, _, _ = _run(tmp_path, [rada], {"@Рада": items})
    assert len(brief["candidates"]) == 3 and brief["stats"]["capped"] == 4


def test_national_due_follows_slot_hours(tmp_path):
    from telegram_net.collect import national_due

    config = make_config([])
    ch = config.active_channels[0]
    state = State(tmp_path)
    kyiv = lambda h: dt.datetime(2026, 11, 14, h - 2, 0, tzinfo=dt.timezone.utc)  # noqa: E731 — взимку Київ = UTC+2
    assert [national_due(config, state, ch, kyiv(h)) for h in (9, 12, 17)] == [0, 1, 2]
    state.log_post({"channel_id": ch.channel_id, "published_at": iso(kyiv(12)), "kind": "national", "text": "т", "candidate_ids": []})
    assert [national_due(config, state, ch, kyiv(h)) for h in (13, 17)] == [0, 1]
    assert build_brief(config, state, kyiv(17))["channels"][0]["national_due"] == 1
