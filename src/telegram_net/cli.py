"""Командний рядок пайплайна. Запуск: PYTHONPATH=src python -m telegram_net.cli <команда>

    collect            зібрати кандидатів → work/brief.md (читає редактор)
    show    <id…>      повний текст кандидатів
    check   <файл>     перевірити файл рішень редактора, нічого не надсилаючи
    publish <файл…>    опублікувати рішення (потрібен TELEGRAM_BOT_TOKEN)
    attack             ранкова інфографіка «Нічна атака» в усі канали
    markets            вечірня інфографіка «Курс валют і пальне»
    weather            ранкова інфографіка погоди в усі канали
    drain              надіслати з черги пости, чий час настав
    report             стан мережі: пости за добу, проблемні джерела
    alerts             цикл сигналів тривоги/відбою (потрібен TELEGRAM_BOT_TOKEN)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import sys
from pathlib import Path

from telegram_net.config import DEFAULT_CONFIG_PATH, load_config
from telegram_net.state import SERVICE_KINDS, State, now_utc

logger = logging.getLogger("telegram_net")


def _publisher(required: bool):
    from telegram_net.publisher import TelegramPublisher

    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        if required:
            sys.exit("TELEGRAM_BOT_TOKEN не задано")
        return None
    return TelegramPublisher(token)


def cmd_collect(args) -> int:
    from telegram_net.clock import local_now
    from telegram_net.collect import brief_text, collect, nothing_to_do
    from telegram_net.fetchers.article import ArticleFetcher
    from telegram_net.fetchers.scrape_fetcher import HtmlListFetcher
    from telegram_net.fetchers.tme_fetcher import TmeFetcher
    from telegram_net.fetchers.web_fetcher import WebFetcher

    config = load_config(args.config)
    state = State(args.state)
    tme = TmeFetcher()
    brief = collect(
        config,
        state,
        {"telegram": tme, "website": WebFetcher(), "web_scrape": HtmlListFetcher()},
        ArticleFetcher(),
        own_channel_reader=tme.latest,
    )
    state.save()
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
    text_out = out.with_suffix(".md")
    text_out.write_text(brief_text(brief), encoding="utf-8")
    print(f"Статистика збору: {brief['stats']}")
    if nothing_to_do(brief):
        # Порожній файл рішень пишемо самі: тоді редактору не треба нічого читати,
        # лише закомітити — а курсори джерел однаково збережуться.
        run_id = local_now(config, now_utc()).strftime("%Y-%m-%dT%H-%M")
        empty = Path("outbox") / f"{run_id}.json"
        empty.parent.mkdir(parents=True, exist_ok=True)
        empty.write_text(json.dumps({"run_id": run_id, "cleanup_branch": True, "posts": [], "keep": []}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"НІЧОГО НОВОГО: кандидатів і рубрик немає. Створено {empty} — лише закоміть і запуш state/ та outbox/.")
    else:
        print(f"Кандидатів: {len(brief['candidates'])}. Читай {text_out}")
    return 0


def cmd_show(args) -> int:
    state = State(args.state)
    for cid in args.ids:
        cand = state.pool.get(cid)
        if cand is None:
            print(f"[{cid}] немає в пулі")
            continue
        print(f"[{cid}] {cand['source']} · {cand['url']}\n{cand['text']}\n")
    return 0


def cmd_check(args) -> int:
    from telegram_net.publish import format_report, load_decisions, publish_run

    config = load_config(args.config)
    state = State(args.state)
    decisions = load_decisions(args.file)
    report = publish_run({**decisions, "dry_run": True}, config, state, None)
    print(format_report(report))
    return 0 if report.ok else 1


def cmd_publish(args) -> int:
    from telegram_net.publish import format_report, load_decisions, publish_run

    config = load_config(args.config)
    state = State(args.state)
    publisher = _publisher(required=True)
    failed = save_state = cleanup = False
    for path in args.files:
        decisions = load_decisions(path)
        report = publish_run(decisions, config, state, publisher)
        print(format_report(report))
        failed = failed or not report.ok
        if not report.dry_run:
            # Стан зберігаємо й після прогону без постів: у ньому курсори джерел
            # і пул, які зібрав collect у сесії редактора.
            state.save()
            save_state = True
            cleanup = cleanup or bool(decisions.get("cleanup_branch"))
    _github_output(save_state=save_state, cleanup_branch=cleanup)
    return 1 if failed else 0


def cmd_drain(args) -> int:
    from telegram_net.publish import drain

    config = load_config(args.config)
    state = State(args.state)
    if not state.queue:
        print("Черга порожня")
        _github_output(save_state=False)
        return 0
    publisher = _publisher(required=True)
    before = len(state.queue)
    sent, errors = drain(config, state, publisher)
    for line in sent:
        print(f"  + {line}")
    for line in errors:
        print(f"  ! {line}")
    print(f"У черзі лишилось: {len(state.queue)}")
    changed = len(state.queue) != before or bool(errors)
    if changed:
        state.save()
    _github_output(save_state=changed)
    return 1 if errors and not sent else 0


def cmd_weather(args) -> int:
    from telegram_net.weather import publish_weather

    config = load_config(args.config)
    state = State(args.state)
    publisher = None if args.dry_run else _publisher(required=True)
    lines = publish_weather(config, state, publisher, force=args.force, out_dir=Path(args.out) if args.out else None)
    for line in lines:
        print(line)
    sent = any("надіслано" in line for line in lines)
    if sent:
        state.save()
    _github_output(save_state=sent)
    return 1 if any("ПОМИЛКА" in line for line in lines) else 0


def cmd_attack(args) -> int:
    from telegram_net.attack import publish_attack

    config = load_config(args.config)
    state = State(args.state)
    publisher = None if args.dry_run else _publisher(required=True)
    lines = publish_attack(config, state, publisher, force=args.force, out_dir=Path(args.out) if args.out else None)
    for line in lines:
        print(line)
    sent = any("надіслано" in line for line in lines)
    if sent:
        state.save()
    _github_output(save_state=sent)
    return 1 if any("ПОМИЛКА" in line for line in lines) else 0


def cmd_markets(args) -> int:
    from telegram_net.markets import publish_markets

    config = load_config(args.config)
    state = State(args.state)
    publisher = None if args.dry_run else _publisher(required=True)
    lines = publish_markets(config, state, publisher, force=args.force, out_dir=Path(args.out) if args.out else None)
    for line in lines:
        print(line)
    sent = any("надіслано" in line for line in lines)
    if sent:
        state.save()
    _github_output(save_state=sent)
    return 1 if any("ПОМИЛКА" in line for line in lines) else 0


def _github_output(**values: bool) -> None:
    """Сигнали наступним крокам workflow (publish.yml): чи зберігати стан, чи видаляти гілку."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for key, value in values.items():
            fh.write(f"{key}={'true' if value else 'false'}\n")


def cmd_report(args) -> int:
    from telegram_net.collect import source_problems

    config = load_config(args.config)
    state = State(args.state)
    now = now_utc()
    day_ago = now - dt.timedelta(hours=24)
    print("Постів за останні 24 години (без сигналів тривоги):")
    for ch in config.active_channels:
        posts = [p for p in state.posts_since(ch.channel_id, day_ago) if p.get("kind") not in SERVICE_KINDS]
        kinds: dict[str, int] = {}
        for p in posts:
            kinds[p.get("kind", "?")] = kinds.get(p.get("kind", "?"), 0) + 1
        flag = "  ← МОВЧИТЬ" if not posts else ""
        print(f"  {ch.name}: {len(posts)} {kinds if kinds else ''}{flag}")
    problems = source_problems(config, state, now)
    print("Проблемні джерела:" if problems else "Проблемних джерел немає.")
    for problem in problems:
        print(f"  {problem}")
    print(f"Кандидатів у пулі: {len(state.pool)}")
    print(f"Заплановано в черзі: {len(state.queue)}")
    for q in sorted(state.queue, key=lambda q: q["due_at"]):
        print(f"  {q['due_at']} {q['channel_id']}: {q['text'][:70]!r}")
    return 0


def cmd_alerts(args) -> int:
    from telegram_net.alerts import AlertRelay, run_loop
    from telegram_net.fetchers.tme_fetcher import TmeFetcher, parse_channel_page

    import httpx

    config = load_config(args.config)
    sender = None if args.dry_run else _publisher(required=True)
    map_provider = None
    if not args.no_map:
        from telegram_net.neptun import capture_map

        map_provider = capture_map
    relay = AlertRelay(config, sender, map_provider)
    http = httpx.Client(timeout=15)
    url = f"https://t.me/s/{config.settings.alerts_source}"

    def fetch():
        resp = http.get(url)
        resp.raise_for_status()
        return [item for _, item in parse_channel_page(resp.text)]

    state_file = Path(args.state_file) if args.state_file else None
    if state_file and state_file.exists():
        relay.load_state(json.loads(state_file.read_text(encoding="utf-8")))
        logger.info("Продовжую з курсора %s", relay.cursor)

    saved = {"value": None}

    def save_state() -> None:
        if state_file is None:
            return
        current = relay.dump_state()
        if current != saved["value"]:
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")
            saved["value"] = current

    logger.info("Слухаю сигнали тривоги з %s кожні %s с", url, args.interval)
    run_loop(relay, fetch, interval_sec=args.interval, max_runtime_min=args.max_minutes, after_tick=save_state)
    save_state()
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="telegram_net")
    parser.add_argument("--config", default=os.environ.get("LOCAL_CONFIG_PATH", DEFAULT_CONFIG_PATH))
    parser.add_argument("--state", default="state")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect")
    p.add_argument("--out", default="work/brief.json")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("show")
    p.add_argument("ids", nargs="+")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("check")
    p.add_argument("file")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("publish")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("drain")
    p.set_defaults(func=cmd_drain)

    p = sub.add_parser("weather")
    p.add_argument("--force", action="store_true", help="не перевіряти годину")
    p.add_argument("--dry-run", action="store_true", help="нічого не надсилати")
    p.add_argument("--out", default=None, help="куди зберегти картинки")
    p.set_defaults(func=cmd_weather)

    p = sub.add_parser("attack")
    p.add_argument("--force", action="store_true", help="не перевіряти годину")
    p.add_argument("--dry-run", action="store_true", help="нічого не надсилати")
    p.add_argument("--out", default=None, help="куди зберегти картинки")
    p.set_defaults(func=cmd_attack)

    p = sub.add_parser("markets")
    p.add_argument("--force", action="store_true", help="не перевіряти годину")
    p.add_argument("--dry-run", action="store_true", help="нічого не надсилати")
    p.add_argument("--out", default=None, help="куди зберегти картинки")
    p.set_defaults(func=cmd_markets)

    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("alerts")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--max-minutes", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--state-file", default=None, help="де зберігати курсор між перезапусками")
    p.add_argument("--no-map", action="store_true", help="не знімати карту NEPTUN")
    p.set_defaults(func=cmd_alerts)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
