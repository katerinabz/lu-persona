#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Адаптер к дневнику. Единственное место, которое знает, где лежат записи.

Источник выбирается переменной окружения, по порядку:

    LU_DIARY_FILE   путь к JSON-файлу с записями (так устроены проверки и демо)
    LU_DIARY_DB     путь к SQLite-базе продукта (lu_web/backend/lu.db),
                    вместе с LU_DIARY_USER: id пользователя из таблицы user
    LU_DIARY_URL    адрес API дневника (ещё не реализовано)

Пока ничего не задано, функции поднимают NotConfigured с текстом, который персонажу
понятно читать: он отличает «записей нет» от «инструмент не работает».

Формат записи одинаков для обоих источников: {"date": "2026-06-03", "text": "...",
"topics": ["работа"]}. JSON-файл содержит список таких объектов.

Чтение из базы повторяет аналитику тем продукта (routers/themes.py):

- база открывается только на чтение (`file:<путь>?mode=ro`), ничего не пишется;
- текст записи — entry.content, темы — entrytheme → theme.name;
- дата — entry.created_at (в базе UTC без пояса), переведённая в местную дату.
  Смещение берётся из analyticssettings.tz_offset_minutes, без него 180 минут (Москва);
- темы из hiddentheme этого пользователя убираются из записей, а значит из topics и link,
  как продукт убирает их из графа, дашборда и итогов. Сама запись остаётся;
- themealias при чтении ничего не сводит: в продукте это ключевые слова автотеггера
  (tagger.py), которые при сохранении записи дописывают связь с темой в entrytheme.
  Синонимы уже сведены в entrytheme, и продукт считает темы по нему как есть.
  Поэтому и здесь счёт идёт по entrytheme без дополнительного слияния.
"""
import io
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone
from urllib.parse import quote


DEFAULT_TZ_OFFSET_MINUTES = 180  # Москва, как в analyticssettings продукта


class NotConfigured(Exception):
    pass


def _load():
    path = os.environ.get('LU_DIARY_FILE')
    if path:
        if not os.path.exists(path):
            raise NotConfigured('файл дневника не найден: %s' % path)
        with io.open(path, encoding='utf-8') as f:
            return json.loads(f.read())
    db = os.environ.get('LU_DIARY_DB')
    if db:
        return _load_db(db, os.environ.get('LU_DIARY_USER'))
    if os.environ.get('LU_DIARY_URL'):
        raise NotConfigured('чтение по сети ещё не реализовано, задайте LU_DIARY_FILE или LU_DIARY_DB')
    raise NotConfigured(
        'дневник не подключен: не задана ни одна из LU_DIARY_FILE, LU_DIARY_DB, LU_DIARY_URL. '
        'Это настройка окружения, а не отсутствие записей.')


def _connect_ro(path):
    if not os.path.exists(path):
        raise NotConfigured('база дневника не найдена: %s' % path)
    uri = 'file:%s?mode=ro' % quote(os.path.abspath(path))
    try:
        con = sqlite3.connect(uri, uri=True)
        con.execute('SELECT 1 FROM entry LIMIT 1')
    except sqlite3.Error as e:
        raise NotConfigured('база дневника не читается (%s): %s' % (e, path))
    return con


def _tz_offset(con):
    try:
        row = con.execute('SELECT tz_offset_minutes FROM analyticssettings ORDER BY id LIMIT 1').fetchone()
    except sqlite3.Error:
        row = None
    return row[0] if row and row[0] is not None else DEFAULT_TZ_OFFSET_MINUTES


def _local_date(created_at, tz):
    """created_at в базе — UTC-строка вида 2026-06-03 21:15:00.123456, иногда с поясом."""
    dt = datetime.fromisoformat(str(created_at).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date().isoformat()


def _load_db(path, user):
    if not user:
        raise NotConfigured(
            'задана LU_DIARY_DB, но не задан LU_DIARY_USER: id пользователя из таблицы user, '
            'чьи записи читать. Это настройка окружения, а не отсутствие записей.')
    try:
        user_id = int(user)
    except ValueError:
        raise NotConfigured('LU_DIARY_USER должен быть числом (id из таблицы user), сейчас: %r' % user)

    con = _connect_ro(path)
    try:
        if con.execute('SELECT 1 FROM user WHERE id = ?', (user_id,)).fetchone() is None:
            raise NotConfigured('в базе дневника нет пользователя с id %d (LU_DIARY_USER)' % user_id)
        tz = timezone(timedelta(minutes=_tz_offset(con)))
        hidden = {r[0] for r in con.execute(
            'SELECT theme_id FROM hiddentheme WHERE user_id = ?', (user_id,))}
        themes = {}
        for entry_id, theme_id, name in con.execute(
                'SELECT et.entry_id, et.theme_id, t.name FROM entrytheme et '
                'JOIN theme t ON t.id = et.theme_id '
                'JOIN entry e ON e.id = et.entry_id '
                'WHERE e.user_id = ? ORDER BY t.name', (user_id,)):
            if theme_id not in hidden:
                themes.setdefault(entry_id, []).append(name)
        rows = []
        for entry_id, content, created_at in con.execute(
                'SELECT id, content, created_at FROM entry WHERE user_id = ? '
                'ORDER BY created_at, id', (user_id,)):
            rows.append({'date': _local_date(created_at, tz),
                         'text': content,
                         'topics': themes.get(entry_id, [])})
        return rows
    except sqlite3.Error as e:
        raise NotConfigured('ошибка чтения базы дневника: %s' % e)
    finally:
        con.close()


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
    # как в продукте: по убыванию числа, при равенстве по имени
    return sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))


def link(topic_a, topic_b):
    """Сколько раз две темы встречались в одной записи. Возвращает число и список дат."""
    dates = [r['date'] for r in entries(None, None, None, limit=10 ** 6)
             if topic_a in r.get('topics', []) and topic_b in r.get('topics', [])]
    return len(dates), dates
