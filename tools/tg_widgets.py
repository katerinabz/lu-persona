#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Виджеты Телеграма для Лу: сборка кода и данные из дневника.

    python3 tools/tg_widgets.py            размер каждого виджета после сборки
    python3 tools/tg_widgets.py preview    собрать tools/widgets/preview-data.js для preview.html

Виджет — один HTML-документ, который Телеграм рисует внутри сообщения (Bot API 10.4).
Код до 4096 байт после сжатия zlib, данные до 1024 байт. Код неизменен после отправки,
меняются только данные.
"""
import io
import json
import os
import re
import sys
import zlib
from datetime import date, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WIDGETS = os.path.join(ROOT, 'tools', 'widgets')
sys.path.insert(0, os.path.join(ROOT, 'agent', 'scripts'))

NAMES = ('welcome', 'answer', 'diary', 'feedback')
CODE_LIMIT = 4096
DATA_LIMIT = 1024
ANSWER_LIMIT = 1024

QUESTIONS = [
    'Мне стоит уйти с работы?',
    'Как у меня со сном?',
    'Это у меня выгорание?',
]

MONTHS = {'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4, 'мая': 5, 'июня': 6,
          'июля': 7, 'августа': 8, 'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12}


class TooLarge(Exception):
    pass


# --- код ---------------------------------------------------------------------------

def minify(html):
    """Сжатие без парсера: строки, отступы, пробелы вокруг знаков. Тексты не трогаются."""
    out = []
    for line in html.splitlines():
        line = line.strip()
        if line:
            out.append(line)
    s = '\n'.join(out)
    s = re.sub(r'/\*.*?\*/', '', s, flags=re.S)
    return s


def compressed_size(html):
    return len(zlib.compress(html.encode('utf-8'), 9))


_cache = {}


def code(name):
    """Готовый код виджета. Поднимает TooLarge, если сервер его не примет."""
    if name not in _cache:
        with io.open(os.path.join(WIDGETS, name + '.html'), encoding='utf-8') as f:
            html = minify(f.read())
        raw, packed = len(html.encode('utf-8')), compressed_size(html)
        if raw > 32768 or packed > CODE_LIMIT:
            raise TooLarge('%s: %d байт, сжатый %d из %d' % (name, raw, packed, CODE_LIMIT))
        _cache[name] = html
    return _cache[name]


def pack(obj):
    s = json.dumps(obj, ensure_ascii=False, separators=(',', ':'))
    if len(s.encode('utf-8')) > DATA_LIMIT:
        raise TooLarge('данные виджета %d байт из %d' % (len(s.encode('utf-8')), DATA_LIMIT))
    return s


# --- дневник -----------------------------------------------------------------------

def load_entries():
    import _diary
    rows = _diary.entries(limit=10 ** 6)
    return sorted(rows, key=lambda r: r['date'])


def _d(s):
    return date(*map(int, s[:10].split('-')))


def span(rows):
    a, b = _d(rows[0]['date']), _d(rows[-1]['date'])
    return a, (b - a).days + 1


def topic_counts(rows):
    c = {}
    for r in rows:
        for t in r.get('topics', []):
            c[t] = c.get(t, 0) + 1
    return sorted(c.items(), key=lambda x: (-x[1], x[0]))


def entry_text(rows, i):
    """Тексты записей дня i от начала периода. Для ответа на запрос виджета, до 1024 байт."""
    a, _ = span(rows)
    day = (a + timedelta(days=int(i))).isoformat()
    texts = ['«%s»' % r['text'].strip() for r in rows if r['date'][:10] == day]
    s = '\n'.join(texts) or 'Записи за этот день нет.'
    while len(s.encode('utf-8')) > ANSWER_LIMIT:
        s = s[:-2].rstrip() + '…'
    return s


# --- данные виджетов ---------------------------------------------------------------

def welcome_data(rows, asked=(), kind='demo'):
    """Приветствие. kind: 'new' — выбор режима, 'demo' — дневник-пример, 'own' — свой дневник."""
    if kind == 'new':
        return pack({'k': 'new'})
    d = {'k': kind, 'n': len(rows), 't': [list(x) for x in topic_counts(rows)]}
    if rows:
        d.update(a=rows[0]['date'][:10], b=rows[-1]['date'][:10])
    if kind == 'demo':
        d.update(q=QUESTIONS, u=sorted(asked))
    return pack(d)


def diary_data(rows, mode=None):
    a, n = span(rows)
    names = [t for t, _ in topic_counts(rows)]
    by = {}
    for r in rows:
        d = (_d(r['date']) - a).days
        by.setdefault(d, 0)  # запись без темы тоже день с записью
        for t in r.get('topics', []):
            by[d] = by.get(d, 0) | (1 << names.index(t))
    pairs = {}
    for r in rows:
        ts = sorted(set(names.index(t) for t in r.get('topics', [])))
        for i in range(len(ts)):
            for j in range(i + 1, len(ts)):
                pairs[(ts[i], ts[j])] = pairs.get((ts[i], ts[j]), 0) + 1
    top = sorted(pairs.items(), key=lambda x: -x[1])[:3]
    out = {'a': a.isoformat(), 'n': n, 't': names, 'e': sorted([k, v] for k, v in by.items()),
           'l': [[i, j, c] for (i, j), c in top]}
    if mode:
        out['m'] = mode
    return pack(out)


def _stem(t):
    if t == 'сон':
        return r'\b(сон|сна|сну|сном|сне)\b'
    base = t[:-1] if t[-1] in 'аяеиоуыьй' and len(t) > 4 else t
    return r'\b' + re.escape(base)


def cited_days(text, a, n):
    """Даты, названные в тексте: 2026-06-03, 03.06 и «3 июня»."""
    out = set()
    for m in re.finditer(r'(\d{4})-(\d{2})-(\d{2})', text):
        out.add(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    for m in re.finditer(r'\b(\d{1,2})\s+(%s)' % '|'.join(MONTHS), text.lower()):
        try:
            out.add(date(a.year, MONTHS[m.group(2)], int(m.group(1))))
        except ValueError:
            pass
    return sorted((d - a).days for d in out if 0 <= (d - a).days < n)


def answer_data(rows, text, mode=None):
    """Данные виджета под ответом Лу или None, если показывать нечего."""
    a, n = span(rows)
    low = text.lower()
    found = []
    for t, _ in topic_counts(rows):
        m = re.search(_stem(t), low)
        if m:
            found.append((m.start(), t))
    topics = [t for _, t in sorted(found)][:3]
    cited = cited_days(text, a, n)
    if not topics and not cited:
        return None
    lines = []
    for t in topics:
        days = sorted({(_d(r['date']) - a).days for r in rows if t in r.get('topics', [])})
        lines.append([t, days])
    if not lines:
        lines.append(['все записи', sorted({(_d(r['date']) - a).days for r in rows})])
    out = {'a': a.isoformat(), 'n': n, 't': lines, 'h': cited}
    if mode:
        out['m'] = mode
    return pack(out)


def feedback_data(answers):
    return pack({'a': answers})


# --- проверка из консоли -----------------------------------------------------------

def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'preview':
        os.environ.setdefault('LU_DIARY_FILE', os.path.join(ROOT, 'demo', 'дневник.json'))
        rows = load_entries()
        texts = {str((_d(r['date']) - span(rows)[0]).days): entry_text(rows, (_d(r['date']) - span(rows)[0]).days)
                 for r in rows}
        # Образец взят из дневника: 14 записей про работу, 6 вместе с тревогой, последняя 9 июля.
        sample = ('Про работу у вас 14 записей, первая от 3 июня. В 6 из них тема «работа» '
                  'встречается вместе с «тревогой». Последняя запись про работу от 9 июля.')
        data = {
            'code': {n: code(n) for n in NAMES},
            'data': {'welcome': welcome_data(rows), 'answer': answer_data(rows, sample),
                     'diary': diary_data(rows), 'feedback': feedback_data({})},
            'caption': {'answer': sample},
            'texts': texts,
        }
        io.open(os.path.join(WIDGETS, 'preview-data.js'), 'w', encoding='utf-8').write(
            'window.PREVIEW=' + json.dumps(data, ensure_ascii=False) + ';\n')
        print('tools/widgets/preview-data.js собран')
        return 0
    bad = 0
    for n in NAMES:
        html = minify(io.open(os.path.join(WIDGETS, n + '.html'), encoding='utf-8').read())
        packed = compressed_size(html)
        ok = packed <= CODE_LIMIT
        bad += not ok
        print('%-9s %6d байт, сжатый %5d из %d %s' % (n, len(html.encode('utf-8')), packed, CODE_LIMIT,
                                                      '' if ok else '← НЕ ПРОЙДЁТ'))
    return 1 if bad else 0


if __name__ == '__main__':
    sys.exit(main())
