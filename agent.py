#!/usr/bin/env python3
"""Ежедневный ИИ-агент идей для контента бренда.

Каждое утро:
  1. Берёт профиль бренда из config/brand.json.
  2. Находит ближайшие праздники и памятные даты (data/holidays.json).
  3. Собирает свежие заголовки из RSS-лент (config/sources.json).
  4. Просит Claude предложить 3 идеи постов с учётом инфоповодов и праздников.
  5. Сохраняет дайджест в output/YYYY-MM-DD.md и (опционально) шлёт в Telegram.

Зависимости: только стандартная библиотека Python 3.9+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
OUTPUT_DIR = ROOT / "output"

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MODEL = "claude-haiku-5-5"
USER_AGENT = "content-ideas-agent/1.0"

log = logging.getLogger("content-agent")


# ---------------------------------------------------------------- helpers

def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def http_get(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ---------------------------------------------------------------- holidays

def upcoming_holidays(today: dt.date, holidays: list[dict], markets: list[str],
                      horizon_days: int) -> list[dict]:
    """Праздники в ближайшие `horizon_days` дней (включая сегодня)."""
    found = []
    for h in holidays:
        countries = h.get("countries", ["INT"])
        if "INT" not in countries and not set(countries) & set(markets):
            continue
        try:
            month, day = (int(x) for x in h["date"].split("-"))
        except (KeyError, ValueError):
            log.warning("Некорректная дата праздника: %s", h)
            continue
        for year in (today.year, today.year + 1):
            try:
                date = dt.date(year, month, day)
            except ValueError:
                continue
            delta = (date - today).days
            if 0 <= delta <= horizon_days:
                found.append({"name": h["name"], "date": date.isoformat(),
                              "in_days": delta, "countries": countries})
                break
    return sorted(found, key=lambda x: x["in_days"])


# ---------------------------------------------------------------- news

def parse_feed(xml_bytes: bytes, limit: int) -> list[dict]:
    """Разбирает RSS 2.0 и Atom. Возвращает список {title, link, published}."""
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []

    for it in root.iter("item"):  # RSS 2.0
        title = (it.findtext("title") or "").strip()
        if title:
            items.append({
                "title": title,
                "link": (it.findtext("link") or "").strip(),
                "published": (it.findtext("pubDate") or "").strip(),
            })

    atom = "{http://www.w3.org/2005/Atom}"
    for entry in root.iter(atom + "entry"):  # Atom
        title = (entry.findtext(atom + "title") or "").strip()
        if title:
            link_el = entry.find(atom + "link")
            items.append({
                "title": title,
                "link": link_el.get("href", "") if link_el is not None else "",
                "published": (entry.findtext(atom + "updated") or "").strip(),
            })

    return items[:limit]


def fetch_news(sources: list[str], per_feed: int) -> list[dict]:
    """Скачивает все ленты. Ошибка одной ленты не останавливает агента.

    Источник может быть http(s)-URL или путь к локальному файлу (удобно для тестов).
    """
    news: list[dict] = []
    for src in sources:
        try:
            if src.startswith(("http://", "https://")):
                data = http_get(src)
            else:
                data = Path(src).read_bytes()
            items = parse_feed(data, per_feed)
            for item in items:
                item["source"] = urllib.parse.urlparse(src).netloc or src
            news.extend(items)
            log.info("Лента %s: %d заголовков", src, len(items))
        except (urllib.error.URLError, OSError, ET.ParseError) as exc:
            log.warning("Лента недоступна %s: %s", src, exc)
    return news


# ---------------------------------------------------------------- LLM

SYSTEM_PROMPT = """Ты — опытный SMM-стратег и копирайтер. Твоя задача — каждое утро \
предлагать бренду 3 идеи постов, которые опираются на актуальные инфоповоды \
и праздники и при этом органично вписываются в бренд.

Правила:
- Идеи должны быть конкретными: тема, крючок (первая строка), формат, площадка.
- Не выдумывай новости. Опирайся только на переданные заголовки и праздники; \
если инфоповодов нет — предлагай evergreen-идеи и честно укажи это в why_now.
- Не предлагай темы, которые противоречат ограничениям бренда или чувствительны \
(политика, трагедии, религиозные конфликты), если бренд явно не работает в этой нише.
- Пиши на языке, указанном в профиле бренда.
- Ответ — ТОЛЬКО JSON-массив из 3 объектов без пояснений вокруг, формат:
[
  {"title": "...", "hook": "первая строка поста", "format": "карусель/рилс/пост/сторис/...",
   "platform": "...", "why_now": "почему это актуально именно сегодня",
   "draft": "черновик текста поста (3-5 предложений)"}
]"""


def build_user_prompt(brand: dict, holidays: list[dict], news: list[dict],
                      today: dt.date) -> str:
    news_lines = "\n".join(
        f"- [{n['source']}] {n['title']}" for n in news[:60]
    ) or "(свежих заголовков не получено — используй evergreen-идеи)"
    hol_lines = "\n".join(
        f"- {h['name']} — {h['date']}"
        + (" (сегодня)" if h["in_days"] == 0 else f" (через {h['in_days']} дн.)")
        for h in holidays
    ) or "(в ближайшие дни праздников нет)"
    return f"""Сегодня: {today.isoformat()}

ПРОФИЛЬ БРЕНДА:
{json.dumps(brand, ensure_ascii=False, indent=2)}

БЛИЖАЙШИЕ ПРАЗДНИКИ И ПАМЯТНЫЕ ДАТЫ:
{hol_lines}

СВЕЖИЕ ИНФОПОВОДЫ:
{news_lines}

Предложи 3 идеи постов на сегодня в JSON, как описано в системной инструкции."""


def call_claude(system: str, user: str, model: str, api_key: str,
                max_tokens: int = 2000) -> str:
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    req = urllib.request.Request(
        ANTHROPIC_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        payload = json.load(resp)
    return "".join(b.get("text", "") for b in payload.get("content", [])
                   if b.get("type") == "text")


def parse_ideas(text: str) -> list[dict]:
    """Достаёт JSON-массив из ответа модели (даже если он обёрнут в ```json)."""
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        raise ValueError("В ответе модели нет JSON-массива")
    ideas = json.loads(match.group(0))
    if not isinstance(ideas, list):
        raise ValueError("JSON-ответ не является массивом")
    return ideas[:3]


# ---------------------------------------------------------------- output

def render_markdown(today: dt.date, brand: dict, holidays: list[dict],
                    news: list[dict], ideas: list[dict] | None) -> str:
    lines = [f"# Идеи для контента — {today.isoformat()}", "",
             f"**Бренд:** {brand.get('brand', '—')}", ""]

    lines += ["## Праздники и даты", ""]
    lines += [f"- {h['name']} — {h['date']}" for h in holidays] or ["- Ближайших дат нет"]
    lines.append("")

    if ideas:
        lines += ["## 3 идеи на сегодня", ""]
        for i, idea in enumerate(ideas, 1):
            lines += [
                f"### {i}. {idea.get('title', 'Без названия')}",
                f"- **Крючок:** {idea.get('hook', '—')}",
                f"- **Формат / площадка:** {idea.get('format', '—')} / {idea.get('platform', '—')}",
                f"- **Почему сейчас:** {idea.get('why_now', '—')}",
                "", f"> {idea.get('draft', '—')}", "",
            ]
    else:
        lines += ["## Идеи", "",
                  "_ИИ-идеи не сгенерированы (нет ANTHROPIC_API_KEY или ошибка API). "
                  "Ниже — исходные данные для ручного брейнсторма._", ""]

    lines += ["## Инфоповоды (исходные данные)", ""]
    lines += [f"- [{n['source']}] {n['title']}" + (f" — {n['link']}" if n['link'] else "")
              for n in news[:30]] or ["- Лент не получено"]
    return "\n".join(lines) + "\n"


def send_telegram(text: str) -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": chat_id, "text": text[:4000],
                                   "disable_web_page_preview": "true"}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30):
            log.info("Дайджест отправлен в Telegram")
    except urllib.error.URLError as exc:
        log.warning("Не удалось отправить в Telegram: %s", exc)


# ---------------------------------------------------------------- main

def run(args: argparse.Namespace) -> int:
    brand = load_json(CONFIG_DIR / "brand.json")
    sources = load_json(CONFIG_DIR / "sources.json")
    holidays_all = load_json(DATA_DIR / "holidays.json")

    today = dt.date.fromisoformat(args.date) if args.date else dt.date.today()
    model = os.getenv("CONTENT_AGENT_MODEL") or brand.get("model", DEFAULT_MODEL)

    holidays = upcoming_holidays(today, holidays_all, brand.get("markets", []),
                                 brand.get("holiday_horizon_days", 7))
    news = fetch_news(sources, brand.get("news_per_feed", 8))

    ideas = None
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if args.dry_run:
        log.info("Dry-run: вызов модели пропущен")
    elif not api_key:
        log.warning("ANTHROPIC_API_KEY не задан — идеи не сгенерированы")
    else:
        try:
            raw = call_claude(SYSTEM_PROMPT,
                              build_user_prompt(brand, holidays, news, today),
                              model, api_key)
            ideas = parse_ideas(raw)
        except (urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
            log.error("Ошибка генерации идей: %s", exc)

    markdown = render_markdown(today, brand, holidays, news, ideas)

    OUTPUT_DIR.mkdir(exist_ok=True)
    out_file = OUTPUT_DIR / f"{today.isoformat()}.md"
    out_file.write_text(markdown, encoding="utf-8")
    log.info("Сохранено: %s", out_file)

    if not args.no_telegram:
        send_telegram(markdown)

    print(markdown)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Агент идей для контента")
    parser.add_argument("--date", help="дата YYYY-MM-DD (по умолчанию сегодня)")
    parser.add_argument("--dry-run", action="store_true",
                        help="не вызывать модель, только собрать данные")
    parser.add_argument("--no-telegram", action="store_true",
                        help="не отправлять дайджест в Telegram")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s",
                        stream=sys.stderr)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
