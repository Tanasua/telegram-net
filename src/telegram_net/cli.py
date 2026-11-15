"""Командний рядок пайплайна. Запуск: PYTHONPATH=src python -m telegram_net.cli <команда>

    collect            зібрати кандидатів → work/brief.json (читає редактор)
    check   <файл>     перевірити файл рішень редактора, нічого не надсилаючи
    publish <файл…>    опублікувати рішення (потрібен TELEGRAM_BOT_TOKEN)
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
from telegram_net.state import State, now_utc

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
    from telegram_net.collect import collect
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
    out.write_text(json.dumps(brief, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"Кандидатів у пулі: {len(brief['candidates'])} → {out}")
    print(f"Статистика збору: {brief['stats']}")
    for problem in brief["problems"]:
        print(f"Проблема: {problem}")
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
        posts = [p for p in state.posts_since(ch.channel_id, day_ago) if p.get("kind") != "alert"]
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
    return 0


def cmd_alerts(args) -> int:
    from telegram_net.alerts import AlertRelay, run_loop
    from telegram_net.fetchers.tme_fetcher import TmeFetcher, parse_channel_page

    import httpx

    config = load_config(args.config)
    sender = None if args.dry_run else _publisher(required=True)
    relay = AlertRelay(config, sender)
    http = httpx.Client(timeout=15)
    url = f"https://t.me/s/{config.settings.alerts_source}"

    def fetch():
        resp = http.get(url)
        resp.raise_for_status()
        return [item for _, item in parse_channel_page(resp.text)]

    logger.info("Слухаю сигнали тривоги з %s кожні %s с", url, args.interval)
    run_loop(relay, fetch, interval_sec=args.interval, max_runtime_min=args.max_minutes)
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

    p = sub.add_parser("check")
    p.add_argument("file")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("publish")
    p.add_argument("files", nargs="+")
    p.set_defaults(func=cmd_publish)

    p = sub.add_parser("report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("alerts")
    p.add_argument("--interval", type=float, default=30)
    p.add_argument("--max-minutes", type=float, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_alerts)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
