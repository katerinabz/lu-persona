#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Адаптер к дневнику. Единственное место, которое знает, где лежат записи.

Источник выбирается переменной окружения:

    LU_DIARY_FILE   путь к JSON-файлу с записями (так устроены проверки и демо)
    LU_DIARY_DB     путь к базе дневника, когда он поднят
    LU_DIARY_URL    адрес API дневника, когда ходим по сети

Пока ничего не задано, функции поднимают NotConfigured с текстом, который персонажу
понятно читать: он отличает «записей нет» от «инструмент не работает».

Формат JSON-файла: список объектов {"date": "2026-06-03", "text": "...", "topics": ["работа"]}.
"""
import io
import json
import os
from collections import Counter


class NotConfigured(Exception):
    pass


def _load():
    path = os.environ.get('LU_DIARY_FILE')
    if path:
        if not os.path.exists(path):
            raise NotConfigured('файл дневника не найден: %s' % path)
        return json.loads(io.open(path, encoding='utf-8').read())
    if os.environ.get('LU_DIARY_DB') or os.environ.get('LU_DIARY_URL'):
        raise NotConfigured('чтение из базы и по сети ещё не реализовано, задайте LU_DIARY_FILE')
    raise NotConfigured(
        'дневник не подключен: не задана ни одна из LU_DIARY_FILE, LU_DIARY_DB, LU_DIARY_URL. '
        'Это настройка окружения, а не отсутствие записей.')


def entries(topic=None, since=None, until=None, limit=50):
    """Записи дневника. Возвращает список словарей: date, text, topics."""
    rows = _load()
    out = []
    for r in rows:
        if topic and topic not in r.get('topics', []):
            continue
        if since and r['date'] < since:
            continue
        if until and r['date'] > until:
            continue
        out.append(r)
    out.sort(key=lambda r: r['date'])
    return out[:limit]


def topics(since=None, until=None):
    """Сколько записей по каждой теме за период. Возвращает список пар (тема, число)."""
    c = Counter()
    for r in entries(None, since, until, limit=10 ** 6):
        for t in r.get('topics', []):
            c[t] += 1
    return c.most_common()


def link(topic_a, topic_b):
    """Сколько раз две темы встречались в одной записи. Возвращает число и список дат."""
    dates = [r['date'] for r in entries(None, None, None, limit=10 ** 6)
             if topic_a in r.get('topics', []) and topic_b in r.get('topics', [])]
    return len(dates), dates
