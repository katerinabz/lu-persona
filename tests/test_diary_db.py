#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение дневника из базы продукта (LU_DIARY_DB).

Временная SQLite с той же схемой, что у lu_web/backend/lu.db, и выдуманными записями.
Запуск: python3 -m unittest discover tests
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'agent', 'scripts'))
import _diary  # noqa: E402


SCHEMA = """
CREATE TABLE user (
    id INTEGER NOT NULL, email VARCHAR NOT NULL, password_hash VARCHAR NOT NULL,
    name VARCHAR NOT NULL, phone VARCHAR NOT NULL, privacy_accepted_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL, PRIMARY KEY (id));
CREATE TABLE entry (
    id INTEGER NOT NULL, user_id INTEGER NOT NULL, content VARCHAR NOT NULL, summary VARCHAR,
    created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, photo_path TEXT,
    mood_emoji TEXT, source TEXT, input_method TEXT, content_html TEXT,
    PRIMARY KEY (id), FOREIGN KEY(user_id) REFERENCES user (id));
CREATE TABLE theme (id INTEGER NOT NULL, name VARCHAR NOT NULL, PRIMARY KEY (id));
CREATE TABLE entrytheme (
    entry_id INTEGER NOT NULL, theme_id INTEGER NOT NULL, PRIMARY KEY (entry_id, theme_id));
CREATE TABLE hiddentheme (
    id INTEGER NOT NULL, user_id INTEGER NOT NULL, theme_id INTEGER NOT NULL,
    created_at DATETIME NOT NULL, PRIMARY KEY (id));
CREATE TABLE themealias (
    id INTEGER NOT NULL, user_id INTEGER NOT NULL, theme_id INTEGER NOT NULL,
    keyword VARCHAR NOT NULL, created_at DATETIME NOT NULL, PRIMARY KEY (id));
"""

TS = '2026-01-01 00:00:00.000000'
THEMES = {1: 'работа', 2: 'сон', 3: 'семья', 4: 'тревога', 5: 'спорт'}
# id, user_id, content, created_at (UTC, как пишет продукт), темы
ENTRIES = [
    (1, 1, 'Дедлайн, спал пять часов.', '2026-06-01 08:00:00.000000', [1, 2]),
    (2, 1, 'Ужин у родителей.', '2026-06-02 18:30:00.000000', [3]),
    # 22:30 UTC — уже 3 июня по Москве
    (3, 1, 'Ночью думала о проекте, не спалось.', '2026-06-02 22:30:00.000000', [1, 2, 4]),
    (4, 1, 'Пробежка утром.', '2026-06-05 05:00:00.000000', [5]),
    (5, 1, 'Совещание, потом тревожно.', '2026-06-07 10:00:00.000000', [1, 4]),
    # чужая запись не должна попасть
    (6, 2, 'Запись другого человека про работу.', '2026-06-03 10:00:00.000000', [1]),
]


class DiaryDbTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.db = os.path.join(self.dir, 'дневник тест.db')
        con = sqlite3.connect(self.db)
        con.executescript(SCHEMA)
        for uid in (1, 2):
            con.execute('INSERT INTO user VALUES (?,?,?,?,?,?,?)',
                        (uid, 'u%d@example.test' % uid, 'x', 'Тест', '0', TS, TS))
        con.executemany('INSERT INTO theme VALUES (?,?)', THEMES.items())
        for eid, uid, text, ts, ths in ENTRIES:
            con.execute('INSERT INTO entry (id, user_id, content, created_at, updated_at) '
                        'VALUES (?,?,?,?,?)', (eid, uid, text, ts, ts))
            con.executemany('INSERT INTO entrytheme VALUES (?,?)', [(eid, t) for t in ths])
        # пользователь 1 скрыл «тревогу», пользователь 2 скрыл «сон»
        con.execute('INSERT INTO hiddentheme VALUES (1, 1, 4, ?)', (TS,))
        con.execute('INSERT INTO hiddentheme VALUES (2, 2, 2, ?)', (TS,))
        # алиас «недосып» → «сон»: в продукте он работает только в автотеггере
        con.execute("INSERT INTO themealias VALUES (1, 1, 2, 'недосып', ?)", (TS,))
        con.commit()
        con.close()

        self._env = {k: os.environ.get(k) for k in
                     ('LU_DIARY_FILE', 'LU_DIARY_DB', 'LU_DIARY_USER', 'LU_DIARY_URL')}
        for k in self._env:
            os.environ.pop(k, None)
        os.environ['LU_DIARY_DB'] = self.db
        os.environ['LU_DIARY_USER'] = '1'

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.dir)

    def test_record_shape_and_local_date(self):
        rows = _diary.entries()
        self.assertEqual(len(rows), 5)
        self.assertEqual(set(rows[0]), {'date', 'text', 'topics'})
        self.assertEqual([r['date'] for r in rows],
                         ['2026-06-01', '2026-06-02', '2026-06-03', '2026-06-05', '2026-06-07'])
        self.assertEqual(rows[2]['text'], 'Ночью думала о проекте, не спалось.')

    def test_only_own_entries(self):
        texts = [r['text'] for r in _diary.entries(limit=100)]
        self.assertNotIn('Запись другого человека про работу.', texts)

    def test_filter_by_topic(self):
        rows = _diary.entries(topic='работа')
        self.assertEqual([r['date'] for r in rows], ['2026-06-01', '2026-06-03', '2026-06-07'])

    def test_filter_by_dates(self):
        rows = _diary.entries(since='2026-06-03', until='2026-06-05')
        self.assertEqual([r['date'] for r in rows], ['2026-06-03', '2026-06-05'])
        self.assertEqual(_diary.topics(since='2026-06-03', until='2026-06-05'),
                         [('работа', 1), ('сон', 1), ('спорт', 1)])

    def test_limit(self):
        self.assertEqual(len(_diary.entries(limit=2)), 2)

    def test_hidden_theme_excluded(self):
        names = dict(_diary.topics())
        self.assertNotIn('тревога', names)
        self.assertEqual(_diary.entries(topic='тревога'), [])
        self.assertEqual(_diary.link('работа', 'тревога'), (0, []))
        # запись со скрытой темой остаётся, без самой темы
        rows = _diary.entries(since='2026-06-07')
        self.assertEqual(rows[0]['topics'], ['работа'])

    def test_hidden_is_per_user(self):
        os.environ['LU_DIARY_USER'] = '2'
        self.assertEqual(_diary.topics(), [('работа', 1)])

    def test_topics_counts_like_product(self):
        # Счёт по entrytheme без скрытых, как list_themes в routers/themes.py
        self.assertEqual(_diary.topics(), [('работа', 3), ('сон', 2), ('семья', 1), ('спорт', 1)])

    def test_alias_not_counted_as_separate_theme(self):
        # themealias сводит синоним при тегировании; при чтении «недосып» темой не становится
        names = dict(_diary.topics())
        self.assertNotIn('недосып', names)
        self.assertEqual(names['сон'], 2)

    def test_link(self):
        self.assertEqual(_diary.link('работа', 'сон'), (2, ['2026-06-01', '2026-06-03']))
        self.assertEqual(_diary.link('семья', 'спорт'), (0, []))

    def test_no_user_is_not_configured(self):
        os.environ.pop('LU_DIARY_USER')
        with self.assertRaises(_diary.NotConfigured) as cm:
            _diary.entries()
        self.assertIn('LU_DIARY_USER', str(cm.exception))

    def test_bad_user(self):
        os.environ['LU_DIARY_USER'] = 'катя'
        self.assertRaises(_diary.NotConfigured, _diary.entries)
        os.environ['LU_DIARY_USER'] = '99'
        self.assertRaises(_diary.NotConfigured, _diary.entries)

    def test_missing_db(self):
        os.environ['LU_DIARY_DB'] = os.path.join(self.dir, 'нет.db')
        self.assertRaises(_diary.NotConfigured, _diary.entries)

    def test_read_only(self):
        con = _diary._connect_ro(self.db)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                con.execute("INSERT INTO theme VALUES (99, 'x')")
        finally:
            con.close()

    def test_tz_from_settings(self):
        con = sqlite3.connect(self.db)
        con.execute('CREATE TABLE analyticssettings (id INTEGER PRIMARY KEY, tz_offset_minutes INTEGER)')
        con.execute('INSERT INTO analyticssettings VALUES (1, 0)')
        con.commit()
        con.close()
        rows = _diary.entries(since='2026-06-02', until='2026-06-02')
        self.assertEqual(len(rows), 2)  # при UTC обе записи 2 июня


if __name__ == '__main__':
    unittest.main()
