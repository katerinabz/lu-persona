#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Гибридный поиск по дневнику: слова (BM25) + смысл (эмбеддинги) + реранкер.

Темы ставятся словарём, поэтому «сидела до ночи над отчётом» не находится по теме «работа»,
а вопрос «когда мне было одиноко?» не находится вообще, если слова «одиноко» в записях нет.
Поиск закрывает это в три шага:

1. BM25 ранжирует записи по словам запроса (с грубой русской основой слова);
2. эмбеддинги ранжируют те же записи по смыслу, косинусной близостью векторов;
3. два списка сливаются по взвешенному Reciprocal Rank Fusion; по желанию реранкер
   пересортировывает первые кандидаты, читая пары «вопрос — запись» целиком.

Источник записей тот же адаптер _diary.py. Векторы кешируются в SQLite по хешу текста и имени
модели, поэтому запись считается один раз. База дневника открывается только на чтение, кеш лежит
отдельно (LU_VEC_CACHE, по умолчанию data/vec_cache.db).

Сеть нужна только шагам 2 и 3. Если она недоступна, поиск честно работает одним BM25 и говорит
об этом в выводе: персонаж должен понимать, каким инструментом нашёл записи.

Окружение:

    LU_EMBED_MODEL   модель эмбеддингов (по умолчанию выбрана замером, см. tools/eval_search.py)
    LU_RERANK_MODEL  реранкер, «none» выключает шаг 3
    LU_EMBED_BASE    адрес API, совместимого с OpenAI (/embeddings и /rerank)
    LU_EMBED_KEY     ключ; без него берутся NEURALDEEP_API_KEY, NEURAL_DEEP_API_KEY, LU_API_KEY
    LU_VEC_CACHE     путь к кешу векторов
"""
import hashlib
import json
import math
import os
import re
import sqlite3
import struct
import time
import urllib.error
import urllib.request
from collections import Counter

import _diary

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Выбраны замером 15.11.2026 (tools/eval_search.py):
# e5-large нашла 95 % нужных записей против 13 % у одних слов. Слова в слиянии весят 0,1:
# на вопросах другими словами они только мешают, но страхуют редкие имена и термины.
# Реранкер на дневнике-примере ухудшил результат (0,95 → 0,91), поэтому выключен и включается
# переменной LU_RERANK_MODEL.
DEFAULT_EMBED_MODEL = 'e5-large'
DEFAULT_RERANK_MODEL = 'none'
WORDS_WEIGHT = float(os.environ.get('LU_WORDS_WEIGHT', '0.1'))
DEFAULT_BASE = 'https://api.neuraldeep.tech/v1'

# Часть моделей обучена с метками «вопрос» и «документ»: без них качество заметно падает.
PREFIXES = {
    'frida': ('search_query: ', 'search_document: '),
    'e5-large': ('query: ', 'passage: '),
}

CANDIDATES = 20   # сколько кандидатов из слияния уходит реранкеру
RRF_K = 60        # стандартная константа Reciprocal Rank Fusion
TIMEOUT = 40
ATTEMPTS = int(os.environ.get('LU_SEARCH_ATTEMPTS', '4'))


class SearchError(Exception):
    pass


# --- слова: BM25 ------------------------------------------------------------------------------

_ENDINGS = sorted("""
ость ости остью ение ения ению ением ении ание ания анию анием ании
ешь ете ишь ите ает ают яет яют ует уют ала ало али ила ило или
ого его ому ему ыми ими ая яя ую юю ой ей ые ие ых их ым им ом ем ов ев ам ям ах ях
ть ла ло ли ал ил ыл ет ит ут ют ат ят ой ий ый ое ее а я о е и ы у ю ь
""".split(), key=len, reverse=True)


def stem(word):
    """Грубая основа: срезаются «-ся» и самое длинное подходящее окончание, основа не короче трёх букв."""
    w = word.lower().replace('ё', 'е')
    if len(w) > 5 and w[-2:] in ('ся', 'сь'):
        w = w[:-2]
    for e in _ENDINGS:
        if w.endswith(e) and len(w) - len(e) >= 3:
            return w[:-len(e)]
    return w


def tokens(text):
    return [stem(w) for w in re.findall(r'[0-9a-zа-яё]+', str(text).lower()) if len(w) > 1]


def bm25_scores(query, docs, k1=1.5, b=0.75):
    """Оценка BM25 для каждого документа. docs — список строк."""
    toks = [tokens(d) for d in docs]
    n = len(toks)
    if not n:
        return []
    avg = sum(len(t) for t in toks) / n or 1.0
    df = Counter()
    for t in toks:
        df.update(set(t))
    q = tokens(query)
    out = []
    for t in toks:
        tf = Counter(t)
        s = 0.0
        for w in q:
            if not tf[w]:
                continue
            idf = math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5))
            s += idf * tf[w] * (k1 + 1) / (tf[w] + k1 * (1 - b + b * len(t) / avg))
        out.append(s)
    return out


def ranking(scores):
    """Индексы по убыванию оценки; нулевые оценки в ранжирование не попадают."""
    return [i for i in sorted(range(len(scores)), key=lambda i: -scores[i]) if scores[i] > 0]


# --- смысл: эмбеддинги ------------------------------------------------------------------------

def _base():
    return os.environ.get('LU_EMBED_BASE', DEFAULT_BASE).rstrip('/')


def _key():
    for k in ('LU_EMBED_KEY', 'NEURALDEEP_API_KEY', 'NEURAL_DEEP_API_KEY', 'LU_API_KEY'):
        if os.environ.get(k):
            return os.environ[k]
    raise SearchError('нет ключа для эмбеддингов (LU_EMBED_KEY)')


def _post(path, body):
    req = urllib.request.Request(
        _base() + path, data=json.dumps(body).encode('utf-8'),
        headers={'Authorization': 'Bearer ' + _key(), 'Content-Type': 'application/json'})
    # Провайдер режет частые и одновременные запросы кодом 429 (потолок общий на аккаунт):
    # ждём, сколько он просит в Retry-After, и пробуем снова, а не роняем поиск.
    for attempt in range(ATTEMPTS):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 504) and attempt < ATTEMPTS - 1:
                try:
                    wait = float(e.headers.get('Retry-After') or 0)
                except ValueError:
                    wait = 0
                time.sleep(max(wait, min(2 ** attempt, 8)))
                continue
            raise SearchError('%s недоступен: %s' % (path, e))
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise SearchError('%s недоступен: %s' % (path, e))


def _cache():
    path = os.environ.get('LU_VEC_CACHE') or os.path.join(ROOT, 'data', 'vec_cache.db')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    con = sqlite3.connect(path)
    con.execute('CREATE TABLE IF NOT EXISTS vec (model TEXT, h TEXT, v BLOB, PRIMARY KEY (model, h))')
    return con


def _pack(v):
    return struct.pack('%df' % len(v), *v)


def _unpack(b):
    return list(struct.unpack('%df' % (len(b) // 4), b))


def _norm(v):
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def embed(texts, model=None, kind='doc'):
    """Нормированные векторы для списка текстов. Кешируются по хешу текста с меткой и модели."""
    model = model or os.environ.get('LU_EMBED_MODEL', DEFAULT_EMBED_MODEL)
    pre = PREFIXES.get(model, ('', ''))[0 if kind == 'query' else 1]
    texts = [pre + str(t) for t in texts]
    out = [None] * len(texts)
    con = _cache()
    try:
        todo = []
        for i, t in enumerate(texts):
            h = hashlib.sha1(t.encode('utf-8')).hexdigest()
            row = con.execute('SELECT v FROM vec WHERE model=? AND h=?', (model, h)).fetchone() if con else None
            if row:
                out[i] = _unpack(row[0])
            else:
                todo.append((i, h))
        for start in range(0, len(todo), 32):
            part = todo[start:start + 32]
            d = _post('/embeddings', {'model': model, 'input': [texts[i] for i, _ in part]})
            data = sorted(d.get('data') or [], key=lambda x: x.get('index', 0))
            if len(data) != len(part):
                raise SearchError('эмбеддинги вернули %d векторов вместо %d' % (len(data), len(part)))
            for (i, h), item in zip(part, data):
                v = _norm(item['embedding'])
                out[i] = v
                if con:
                    con.execute('INSERT OR REPLACE INTO vec VALUES (?, ?, ?)', (model, h, _pack(v)))
        if con:
            con.commit()
    finally:
        if con:
            con.close()
    return out


def vector_scores(query, docs, model=None):
    q = embed([query], model, 'query')[0]
    return [sum(a * b for a, b in zip(q, v)) for v in embed(docs, model, 'doc')]


# --- слияние и реранкер -----------------------------------------------------------------------

def rrf(*rankings, k=RRF_K, weights=None):
    """Reciprocal Rank Fusion: сумма вес/(k + место) по всем спискам."""
    weights = weights or [1.0] * len(rankings)
    s = Counter()
    for r, w in zip(rankings, weights):
        for place, i in enumerate(r):
            s[i] += w / (k + place + 1)
    return [i for i, _ in s.most_common()]


def rerank(query, docs, idx, model=None):
    """Пересортировка кандидатов idx реранкером. Возвращает индексы по убыванию."""
    model = model or os.environ.get('LU_RERANK_MODEL', DEFAULT_RERANK_MODEL)
    if not idx or model == 'none':
        return list(idx)
    d = _post('/rerank', {'model': model, 'query': query, 'documents': [docs[i] for i in idx]})
    res = sorted(d.get('results') or [], key=lambda r: -r.get('relevance_score', 0))
    order = [idx[r['index']] for r in res if 0 <= r.get('index', -1) < len(idx)]
    return order + [i for i in idx if i not in order]


def doc_text(r):
    """Что видят эмбеддинги и реранкер: текст записи вместе с её темами."""
    t = ', '.join(r.get('topics', []))
    return ('%s (темы: %s)' % (r['text'], t)) if t else r['text']


def rank(query, docs, mode='hybrid', embed_model=None, rerank_model=None):
    """Ранжирование строк docs. Возвращает (индексы, как искали)."""
    words = ranking(bm25_scores(query, docs))
    if mode == 'bm25':
        return words, 'слова'
    try:
        meaning = ranking([s + 1.0 for s in vector_scores(query, docs, embed_model)])
    except SearchError as e:
        return words, 'только слова: смысловой поиск недоступен (%s)' % e
    if mode == 'vector':
        return meaning, 'смысл'
    fused = rrf(words, meaning, weights=[WORDS_WEIGHT, 1.0])
    if mode == 'fusion' or (rerank_model or os.environ.get('LU_RERANK_MODEL', DEFAULT_RERANK_MODEL)) == 'none':
        return fused, 'слова + смысл'
    head = fused[:CANDIDATES]
    try:
        return rerank(query, docs, head, rerank_model) + fused[CANDIDATES:], 'слова + смысл + реранкер'
    except SearchError as e:
        return fused, 'слова + смысл, реранкер недоступен (%s)' % e


def search(query, k=5, topic=None, since=None, until=None, mode='hybrid'):
    """Записи дневника, ближе всего подходящие к вопросу. Возвращает (записи, как искали)."""
    rows = _diary.entries(topic, since, until, limit=10 ** 6)
    if not rows:
        return [], 'записей в этом диапазоне нет'
    order, how = rank(query, [doc_text(r) for r in rows], mode)
    return [rows[i] for i in order[:k]], how
