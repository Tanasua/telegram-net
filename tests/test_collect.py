import datetime as dt

from helpers import NOW, FakeFetcher, item, make_config, src

from telegram_net.collect import address_hint, build_brief, collect, is_alert_signal, is_quiet, rubrics_due
from telegram_net.fetchers.article import Article
from telegram_net.state import State, iso


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
    assert len(brief["candidates"]) == 6 and brief["stats"]["capped"] == 4
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
