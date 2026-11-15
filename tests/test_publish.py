import datetime as dt

from telegram_net.signature import signature_html
from helpers import NOW, make_config

from telegram_net.publish import drain, format_report, prepare, publish_run
from telegram_net.state import State, iso


class FakePublisher:
    def __init__(self, fail_for=()):
        self.sent = []
        self.fail_for = set(fail_for)

    def publish(self, chat_id, text, media_batches):
        if chat_id in self.fail_for:
            raise RuntimeError("chat not found")
        self.sent.append((chat_id, text, media_batches))
        return [100 + len(self.sent)]

    def check_chat(self, chat_id):
        return f"Канал {chat_id}"


def _state(tmp_path, *cands):
    state = State(tmp_path)
    for cid, scope in cands or (("c1", "local"),):
        state.pool[cid] = {
            "id": cid,
            "scope": scope,
            "channel_id": "malyn" if scope == "local" else None,
            "source": "Джерело",
            "url": f"https://src/{cid}",
            "added_at": iso(NOW),
            "published_at": iso(NOW),
            "text": "текст джерела",
            "media": [{"type": "photo", "url": f"https://cdn/{cid}-1.jpg"}, {"type": "photo", "url": f"https://cdn/{cid}-2.jpg"}],
        }
    return state


def _post(**kw):
    return {"channel_id": "malyn", "kind": "local", "candidate_ids": ["c1"], "text": "У Малині відкрили міст.", **kw}


def _run(posts, **kw):
    return {"run_id": "r1", "posts": posts, "keep": [], **kw}


def _errors(tmp_path, post, now=NOW, state=None, config=None):
    _, errors = prepare(_run([post]), config or make_config(), state or _state(tmp_path), now)
    return " ".join(errors)


def test_valid_post_is_rendered_with_bold_headline_and_escaped_html(tmp_path):
    prepared, errors = prepare(
        _run([_post(headline="Міст <відкрито>", text="Рух & пішоходи.")]), make_config(), _state(tmp_path), NOW
    )
    assert errors == []
    (p,) = prepared
    assert p.html_text == "<b>Міст &lt;відкрито&gt;</b>\n\nРух &amp; пішоходи.\n\n" + signature_html(p.channel)
    assert p.plain_text == "Міст <відкрито>\n\nРух & пішоходи."
    assert [m.url for m in p.media] == ["https://cdn/c1-1.jpg", "https://cdn/c1-2.jpg"]
    assert p.source_urls == ["https://src/c1"]


def test_media_modes(tmp_path):
    config, state = make_config(), _state(tmp_path)
    first, _ = prepare(_run([_post(media="first")]), config, state, NOW)
    none, _ = prepare(_run([_post(media="none")]), config, state, NOW)
    assert len(first[0].media) == 1 and none[0].media == ()


def test_stop_topic_is_blocked_even_if_editor_wrote_it(tmp_path):
    assert "стоп-тема" in _errors(tmp_path, _post(text="У Малині ТЦК проводить перевірки."))
    assert "стоп-тема" in _errors(tmp_path, _post(headline="Мобілізація: що змінилось"))


def test_quiet_hours_block_everything_except_urgent(tmp_path):
    night = dt.datetime(2026, 11, 14, 23, 30, tzinfo=dt.timezone.utc)  # 01:30 за Києвом
    assert "нічна тиша" in _errors(tmp_path, _post(), now=night)
    assert _errors(tmp_path, _post(urgent=True), now=night) == ""


def test_basic_validation_errors(tmp_path):
    assert "канал" in _errors(tmp_path, _post(channel_id="nope"))
    assert "канал" in _errors(tmp_path, _post(channel_id="off"))
    assert "kind" in _errors(tmp_path, _post(kind="alert"))
    assert "порожній" in _errors(tmp_path, _post(text="  "))
    assert "немає в пулі" in _errors(tmp_path, _post(candidate_ids=["zzz"]))
    assert "без candidate_ids" in _errors(tmp_path, _post(candidate_ids=[]))
    assert "задовгий" in _errors(tmp_path, _post(text="а" * 1001))
    assert _errors(tmp_path, _post(text="а" * 1001, media="none")) == ""  # без медіа ліміт більший


def test_rubric_needs_no_candidates_but_goes_once_a_day(tmp_path):
    state = _state(tmp_path)
    post = {"channel_id": "malyn", "kind": "evening", "text": "Головне за день у Малині."}
    assert _errors(tmp_path, post, state=state) == ""
    state.log_post({"channel_id": "malyn", "published_at": iso(NOW), "kind": "evening", "text": "т", "candidate_ids": []})
    assert "вже виходила" in _errors(tmp_path, post, state=state)


def test_daily_per_run_and_national_limits(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"), ("c3", "local"), ("c4", "local"), ("n1", "national"))
    _, errors = prepare(_run([_post(candidate_ids=[c]) for c in ("c1", "c2", "c3", "c4")]), config, state, NOW)
    assert len(errors) == 1 and "ліміт новин за прогін" in errors[0]

    for _ in range(4):
        state.log_post({"channel_id": "malyn", "published_at": iso(NOW), "kind": "national", "text": "т", "candidate_ids": []})
    assert "всеукраїнських" in _errors(tmp_path, _post(kind="national", candidate_ids=["n1"]), state=state)

    for _ in range(8):
        state.log_post({"channel_id": "malyn", "published_at": iso(NOW), "kind": "local", "text": "т", "candidate_ids": []})
    assert "добовий ліміт каналу" in _errors(tmp_path, _post(), state=state)
    # Сигнали тривоги в добовий ліміт не входять, інший канал — окремий лічильник.
    assert _errors(tmp_path, _post(channel_id="zhytomyr"), state=state) == ""


def test_same_candidate_cannot_be_posted_twice_to_one_channel_but_can_go_to_several(tmp_path):
    config, state = make_config(), _state(tmp_path, ("o1", "oblast"))
    posts = [
        _post(kind="oblast", candidate_ids=["o1"]),
        _post(kind="oblast", candidate_ids=["o1"], channel_id="zhytomyr"),
        _post(kind="oblast", candidate_ids=["o1"]),
    ]
    prepared, errors = prepare(_run(posts), config, state, NOW)
    assert [p.channel.channel_id for p in prepared] == ["malyn", "zhytomyr"]
    assert len(errors) == 1 and "вже виходила" in errors[0]


def test_every_post_ends_with_three_line_channel_signature(tmp_path):
    config, state = make_config(), _state(tmp_path)
    (p,), _ = prepare(_run([_post()]), config, state, NOW)
    assert p.html_text.endswith(
        "\n\n<b>❤️ Мій Малин | Новини Малина 🇺🇦</b>\n\n⚡️Новини нашого району⚡️"
        '\n\n<a href="https://t.me/mymalyn"><b>👉👉👉 Підписатися 👈👈👈</b></a>'
    )


def test_publish_run_sends_logs_and_trims_pool(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"), ("c3", "local"))
    publisher = FakePublisher()
    report = publish_run(_run([_post()], keep=["c2", "c1"]), config, state, publisher, NOW)

    assert report.ok and len(publisher.sent) == 1
    chat_id, text, batches = publisher.sent[0]
    assert chat_id == "@mymalyn" and text == "У Малині відкрили міст.\n\n" + signature_html(config.channel("malyn")) and len(batches[0]) == 2
    (entry,) = state.published
    assert entry["channel_id"] == "malyn" and entry["kind"] == "local" and entry["candidate_ids"] == ["c1"]
    assert entry["message_ids"] == [101] and entry["run_id"] == "r1" and entry["source_urls"] == ["https://src/c1"]
    # c1 опубліковано, c3 редактор не згадав (= відхилив), c2 попросив притримати.
    assert list(state.pool) == ["c2"]


def test_same_run_is_not_published_twice(tmp_path):
    config, state, publisher = make_config(), _state(tmp_path), FakePublisher()
    publish_run(_run([_post()]), config, state, publisher, NOW)
    state.save()
    again = publish_run(_run([_post()]), config, State(tmp_path), publisher, NOW)
    assert again.skipped_run and len(publisher.sent) == 1
    assert "уже оброблено" in format_report(again)


def test_telegram_failure_keeps_post_in_queue_for_retry_then_drops_it(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"))
    publisher = FakePublisher(fail_for={"@mymalyn"})
    report = publish_run(
        _run([_post(), _post(channel_id="zhytomyr", candidate_ids=["c2"])]), config, state, publisher, NOW
    )
    assert not report.ok and "помилка Telegram" in report.errors[0]
    assert [p["channel_id"] for p in state.published] == ["zhytomyr"]
    assert [q["channel_id"] for q in state.queue] == ["malyn"] and state.queue[0]["attempts"] == 1
    for _ in range(2):
        drain(config, state, publisher, NOW)
    assert state.queue == []  # після трьох невдалих спроб пост прибрано, черга не забивається


def test_invalid_post_does_not_block_valid_ones(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"))
    publisher = FakePublisher()
    report = publish_run(
        _run([_post(text="Про мобілізацію."), _post(candidate_ids=["c2"])]), config, state, publisher, NOW
    )
    assert len(publisher.sent) == 1 and len(report.errors) == 1


def test_dry_run_sends_nothing_and_changes_nothing(tmp_path):
    config, state, publisher = make_config(), _state(tmp_path), FakePublisher()
    report = publish_run(_run([_post()], dry_run=True), config, state, publisher, NOW)
    assert publisher.sent == [] and state.published == [] and list(state.pool) == ["c1"] and state.runs == []
    text = format_report(report)
    assert "пробний" in text and "бот може публікувати в @mymalyn" in text and "У Малині відкрили міст." in text

    offline = publish_run(_run([_post()]), config, state, None, NOW)  # без токена — теж лише перевірка
    assert offline.dry_run and state.published == []


def test_media_comes_from_first_candidate_that_has_it(tmp_path):
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"))
    state.pool["c1"]["media"] = []
    (p,), _ = prepare(_run([_post(candidate_ids=["c1", "c2"])]), make_config(), state, NOW)
    assert [m.url for m in p.media] == ["https://cdn/c2-1.jpg", "https://cdn/c2-2.jpg"]


def test_publish_step_needs_only_httpx():
    """publish.yml ставить лише httpx. Якщо крок публікації потягне bs4, lxml чи
    feedparser (через імпорт модулів збору), workflow впаде ще до першого посту."""
    import subprocess
    import sys

    code = (
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('bs4', 'lxml', 'feedparser'):\n"
        "            raise ImportError('заборонено в кроці публікації: ' + name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "import telegram_net.publish, telegram_net.publisher, telegram_net.cli\n"
        "telegram_net.cli.main(['check', sys.argv[1]])\n"
    )
    import json
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as fh:
        json.dump({"run_id": "x", "posts": []}, fh)
    result = subprocess.run([sys.executable, "-c", code, fh.name], capture_output=True, text=True, env={"PYTHONPATH": "src"})
    assert result.returncode == 0, result.stderr


def test_posts_of_one_channel_are_spaced_and_sent_by_drain(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"), ("c3", "local"), ("o1", "oblast"))
    publisher = FakePublisher()
    posts = [
        _post(candidate_ids=["c1"]),
        _post(candidate_ids=["c2"]),
        _post(kind="oblast", candidate_ids=["o1"], channel_id="zhytomyr"),
        _post(candidate_ids=["c3"]),
    ]
    report = publish_run(_run(posts), config, state, publisher, NOW)
    assert report.ok
    # Одразу — лише перший пост кожного каналу; решта Малина — через 40 і 80 хв.
    assert [chat for chat, _, _ in publisher.sent] == ["@mymalyn", "@myzhytomyr"]
    due = sorted((q["due_at"], q["text"]) for q in state.queue)
    assert [d for d, _ in due] == [iso(NOW + dt.timedelta(minutes=40)), iso(NOW + dt.timedelta(minutes=80))]
    assert "заплановано на 15:40" in format_report(report)

    drain(config, state, publisher, NOW + dt.timedelta(minutes=30))
    assert len(publisher.sent) == 2  # ще рано
    drain(config, state, publisher, NOW + dt.timedelta(minutes=45))
    assert len(publisher.sent) == 3 and len(state.queue) == 1
    drain(config, state, publisher, NOW + dt.timedelta(minutes=90))
    assert len(publisher.sent) == 4 and state.queue == []
    assert [p["channel_id"] for p in state.published] == ["malyn", "zhytomyr", "malyn", "malyn"]


def test_spacing_continues_from_previous_post_and_queue(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"))
    state.log_post({"channel_id": "malyn", "published_at": iso(NOW - dt.timedelta(minutes=10)), "kind": "local", "text": "т", "candidate_ids": []})
    publisher = FakePublisher()
    publish_run(_run([_post()]), config, state, publisher, NOW)
    assert publisher.sent == [] and state.queue[0]["due_at"] == iso(NOW + dt.timedelta(minutes=30))


def test_queued_posts_count_toward_limits_and_are_not_repeated(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"))
    publish_run(_run([_post(candidate_ids=["c1"]), _post(candidate_ids=["c2"])]), config, state, FakePublisher(), NOW)
    assert len(state.queue) == 1
    # Наступний прогін бачить заплановане: той самий кандидат удруге не піде.
    state.pool["c2"] = {**state.pool.get("c2", {}), "id": "c2", "url": "https://src/c2", "media": [], "added_at": iso(NOW), "published_at": iso(NOW), "text": "т", "scope": "local", "channel_id": "malyn", "source": "Д"}
    _, errors = prepare({"run_id": "r2", "posts": [_post(candidate_ids=["c2"])], "keep": []}, config, state, NOW + dt.timedelta(minutes=5))
    assert errors and "вже виходила" in errors[0]


def test_post_scheduled_into_quiet_hours_waits_for_morning(tmp_path):
    config = make_config()
    evening = dt.datetime(2026, 11, 14, 20, 50, tzinfo=dt.timezone.utc)  # 22:50 за Києвом
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"))
    publisher = FakePublisher()
    publish_run(_run([_post(candidate_ids=["c1"]), _post(candidate_ids=["c2"])]), config, state, publisher, evening)
    assert len(publisher.sent) == 1
    assert state.queue[0]["due_at"] == "2026-11-15T05:00:00+00:00"  # 07:00 за Києвом, а не 23:30


def test_urgent_post_skips_spacing(tmp_path):
    config = make_config()
    night = dt.datetime(2026, 11, 14, 23, 30, tzinfo=dt.timezone.utc)
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"))
    state.log_post({"channel_id": "malyn", "published_at": iso(night - dt.timedelta(minutes=5)), "kind": "local", "text": "т", "candidate_ids": []})
    publisher = FakePublisher()
    publish_run(_run([_post(candidate_ids=["c1"], urgent=True)]), config, state, publisher, night)
    assert len(publisher.sent) == 1 and state.queue == []


def test_national_has_own_run_slot_beyond_local_limit(tmp_path):
    config = make_config()
    state = _state(tmp_path, ("c1", "local"), ("c2", "local"), ("c3", "local"), ("n1", "national"), ("n2", "national"))
    posts = [_post(candidate_ids=[c]) for c in ("c1", "c2", "c3")] + [_post(kind="national", candidate_ids=["n1"])]
    prepared, errors = prepare(_run(posts), config, state, NOW)
    assert errors == [] and len(prepared) == 4
    # але не більше однієї всеукраїнської за прогін на канал
    _, errors = prepare(_run([_post(kind="national", candidate_ids=["n1"]), _post(kind="national", candidate_ids=["n2"])]), config, state, NOW)
    assert len(errors) == 1 and "не більше однієї всеукраїнської" in errors[0]
