#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Хранилище людей из Телеграма: свои дневники, режим, свой ключ модели.

    data/lu.db   SQLite, в git не попадает

Таблицы дневника повторяют схему продукта (user, entry, theme, entrytheme, hiddentheme,
analyticssettings), поэтому Лу читает чужой дневник тем же адаптером `_diary.py`, что и
дневник продукта: LU_DIARY_DB=data/lu.db, LU_DIARY_USER=<id>. Своих правил чтения здесь нет.

Таблица tg связывает человека из Телеграма с id дневника и хранит:

    mode    'demo' — разговор о дневнике-примере, 'own' — свой дневник, '' — ещё не выбрал
    key     свой ключ модели, если человек его привязал (base и model рядом)

Темы новой записи проставляет словарь ключевых слов, без модели: запись бесплатна и мгновенна.
Те же шесть тем, что в дневнике-примере, чтобы виджеты и ответы Лу выглядели одинаково.
"""
import datetime
import os
import re
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.environ.get('LU_TG_DB', os.path.join(ROOT, 'data', 'lu.db'))
sys.path.insert(0, os.path.join(ROOT, 'agent', 'scripts'))

TZ_OFFSET_MINUTES = 180  # Москва; _diary.py переводит UTC из базы в местную дату по этому сдвигу

SCHEMA = """
CREATE TABLE IF NOT EXISTS user (id INTEGER PRIMARY KEY, created_at DATETIME NOT NULL);
CREATE TABLE IF NOT EXISTS entry (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, content VARCHAR NOT NULL,
    created_at DATETIME NOT NULL);
CREATE INDEX IF NOT EXISTS entry_user ON entry(user_id, created_at);
CREATE TABLE IF NOT EXISTS theme (id INTEGER PRIMARY KEY, name VARCHAR NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS entrytheme (
    entry_id INTEGER NOT NULL, theme_id INTEGER NOT NULL, PRIMARY KEY (entry_id, theme_id));
CREATE TABLE IF NOT EXISTS hiddentheme (
    id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, theme_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL);
CREATE TABLE IF NOT EXISTS analyticssettings (id INTEGER PRIMARY KEY, tz_offset_minutes INTEGER);
CREATE TABLE IF NOT EXISTS tg (
    tg_id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, mode TEXT NOT NULL DEFAULT '',
    created_at DATETIME NOT NULL, key TEXT, base TEXT, model TEXT);
"""

# Ключевые слова тем. Начало слова, без морфологии: «работ» ловит работу, работать, работой.
TOPICS = {
    'работа': r'работ|начальн|коллег|проект|офис|дедлайн|совещан|созвон|клиент|отч[её]т|увол|задач[аиу]\b|'
              r'шеф|босс|собеседован',
    'тревога': r'тревог|тревож|беспоко|страш|страх|паник|нервн|нервнича|волнова|волну[юе]|пережива|стресс|'
               r'не по себе',
    'сон': r'сон\b|сна\b|сну\b|сном\b|сне\b|спал|сплю|спать|спала|усну|засну|засып|проснул|бессонн|недосып|'
           r'выспал|кошмар',
    'отношения': r'отношен|парн[яюеи]|парень|девушк|муж[аеу]?\b|жен[аеыу]\b|мам[аеуы]?\b|пап[аеуы]?\b|'
                 r'родител|друз|подруг|друг[ау]?\b|ссор|поссор|свидан|любим|расстал|семь[яиюе]',
    'спорт': r'спорт|трениров|бега|бегал|бегу\b|пробеж|зал[еау]?\b|йог|плаван|бассейн|велосипед|футбол|'
             r'фитнес|растяжк|качал',
    'деньги': r'деньг|денег|зарплат|долг[аиу]?\b|долгов|задолж|кредит|ипотек|трат|потрат|купил|покупк|бюджет|накоплен|сбережен|'
              r'\d+\s*(руб|тыс|к\b)|аренд|счет[аы]? за|оплат',
}
_TOPIC_RE = {t: re.compile(r'(?<![а-яё])(?:%s)' % p) for t, p in TOPICS.items()}


def tag(text):
    """Темы записи по ключевым словам, в порядке TOPICS."""
    low = (text or '').lower()
    return [t for t, rx in _TOPIC_RE.items() if rx.search(low)]


def _now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


class Store(object):
    def __init__(self, path=None):
        self.path = path or DB
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        self.con.row_factory = sqlite3.Row
        self.con.executescript(SCHEMA)
        if not self.con.execute('SELECT 1 FROM analyticssettings').fetchone():
            self.con.execute('INSERT INTO analyticssettings (id, tz_offset_minutes) VALUES (1, ?)',
                             (TZ_OFFSET_MINUTES,))
        for t in TOPICS:
            self.con.execute('INSERT OR IGNORE INTO theme (name) VALUES (?)', (t,))
        self.con.commit()

    def close(self):
        self.con.close()

    # --- человек -----------------------------------------------------------------------

    def person(self, tg_id):
        """Строка tg для человека, заводится при первом обращении."""
        row = self.con.execute('SELECT * FROM tg WHERE tg_id = ?', (int(tg_id),)).fetchone()
        if row:
            return dict(row)
        cur = self.con.execute('INSERT INTO user (created_at) VALUES (?)', (_now(),))
        self.con.execute('INSERT INTO tg (tg_id, user_id, created_at) VALUES (?, ?, ?)',
                         (int(tg_id), cur.lastrowid, _now()))
        self.con.commit()
        return self.person(tg_id)

    def set_mode(self, tg_id, mode):
        assert mode in ('demo', 'own')
        self.person(tg_id)
        self.con.execute('UPDATE tg SET mode = ? WHERE tg_id = ?', (mode, int(tg_id)))
        self.con.commit()

    def set_key(self, tg_id, key, base, model):
        self.person(tg_id)
        self.con.execute('UPDATE tg SET key = ?, base = ?, model = ? WHERE tg_id = ?',
                         (key, base, model, int(tg_id)))
        self.con.commit()

    def clear_key(self, tg_id):
        self.con.execute('UPDATE tg SET key = NULL, base = NULL, model = NULL WHERE tg_id = ?',
                         (int(tg_id),))
        self.con.commit()

    def forget(self, tg_id):
        """Удалить человека целиком: записи, темы записей, ключ, строку tg."""
        p = self.con.execute('SELECT user_id FROM tg WHERE tg_id = ?', (int(tg_id),)).fetchone()
        if not p:
            return 0
        uid = p['user_id']
        n = self.con.execute('SELECT COUNT(*) FROM entry WHERE user_id = ?', (uid,)).fetchone()[0]
        self.con.execute('DELETE FROM entrytheme WHERE entry_id IN (SELECT id FROM entry WHERE user_id = ?)',
                         (uid,))
        self.con.execute('DELETE FROM entry WHERE user_id = ?', (uid,))
        self.con.execute('DELETE FROM hiddentheme WHERE user_id = ?', (uid,))
        self.con.execute('DELETE FROM user WHERE id = ?', (uid,))
        self.con.execute('DELETE FROM tg WHERE tg_id = ?', (int(tg_id),))
        self.con.commit()
        return n

    # --- записи ------------------------------------------------------------------------

    def add_entry(self, tg_id, text):
        """Сохранить запись. Возвращает (id записи, темы)."""
        uid = self.person(tg_id)['user_id']
        cur = self.con.execute('INSERT INTO entry (user_id, content, created_at) VALUES (?, ?, ?)',
                               (uid, text, _now()))
        topics = tag(text)
        for t in topics:
            self.con.execute('INSERT INTO entrytheme (entry_id, theme_id) '
                             'SELECT ?, id FROM theme WHERE name = ?', (cur.lastrowid, t))
        self.con.commit()
        return cur.lastrowid, topics

    def take_back(self, tg_id, entry_id):
        """Удалить свою запись и вернуть её текст: запись оказалась вопросом к Лу."""
        uid = self.person(tg_id)['user_id']
        row = self.con.execute('SELECT content FROM entry WHERE id = ? AND user_id = ?',
                               (int(entry_id), uid)).fetchone()
        if not row:
            return None
        self.con.execute('DELETE FROM entrytheme WHERE entry_id = ?', (int(entry_id),))
        self.con.execute('DELETE FROM entry WHERE id = ?', (int(entry_id),))
        self.con.commit()
        return row['content']

    def entries(self, tg_id):
        """Записи человека в формате дневника: тот же адаптер, которым их читает Лу."""
        import _diary
        uid = self.person(tg_id)['user_id']
        try:
            rows = _diary._load_db(self.path, str(uid))
        except _diary.NotConfigured:
            return []
        return sorted(rows, key=lambda r: r['date'])
