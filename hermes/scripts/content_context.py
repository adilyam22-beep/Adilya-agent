#!/usr/bin/env python3
"""Обёртка для Hermes: печатает контекст дня (бренд, праздники, новости).

Hermes вызывает этот скрипт по расписанию (cron) или по запросу, а его stdout
передаёт модели как входные данные для идей постов. Сам скрипт лежит в репозитории;
в ~/.hermes/scripts/ на него указывает символическая ссылка (см. hermes/setup.sh).
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

import agent  # noqa: E402

if __name__ == "__main__":
    sys.exit(agent.main(["--context"]))
