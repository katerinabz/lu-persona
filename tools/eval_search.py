#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Замер поиска по дневнику: какие записи находит каждый способ.

    python3 tools/eval_search.py                 # все способы и модели, отчёт в tests/
    python3 tools/eval_search.py --models frida  # одна модель эмбеддингов

Вопросы из tests/search_queries.json заданы к дневнику-примеру (demo/дневник.json) и нарочно
сформулированы другими словами, чем записи: «когда я плохо спала» при записях «легла в три»
и «проснулась разбитая». Для каждого вопроса заранее указаны номера подходящих записей.

Метрики:
    recall@5  какая доля подходящих записей попала в первые 5 (из скольких могло попасть)
    MRR       1 / место первой подходящей записи, среднее по вопросам
"""
import argparse
import io
import json
import os
import sys
import time
from datetime import date

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'agent', 'scripts'))
import _search  # noqa: E402

EMBED_MODELS = ['frida', 'giga-embeddings', 'jina-embeddings-v4', 'bge-m3', 'qwen3-embedding-8b', 'e5-large']
RERANKERS = ['bge-reranker', 'qwen3-reranker-8b']
K = 5


def metrics(order, gold):
    top = order[:K]
    hit = len([i for i in top if i in gold])
    recall = hit / min(K, len(gold))
    first = next((p for p, i in enumerate(order) if i in gold), None)
    return recall, (1.0 / (first + 1)) if first is not None else 0.0


def run(name, fn, queries):
    rs, ms, t0 = [], [], time.time()
    for q in queries:
        r, m = metrics(fn(q['q']), set(q['gold']))
        rs.append(r)
        ms.append(m)
    return {'name': name, 'recall': sum(rs) / len(rs), 'mrr': sum(ms) / len(ms),
            'sec': time.time() - t0, 'per': rs}


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--models', nargs='*', default=EMBED_MODELS)
    p.add_argument('--rerankers', nargs='*', default=RERANKERS)
    a = p.parse_args()

    diary = json.load(io.open(os.path.join(ROOT, 'demo', 'дневник.json'), encoding='utf-8'))
    queries = json.load(io.open(os.path.join(ROOT, 'tests', 'search_queries.json'), encoding='utf-8'))
    docs = [_search.doc_text(r) for r in diary]
    words = lambda q: _search.ranking(_search.bm25_scores(q, docs))  # noqa: E731

    res = [run('слова (BM25)', words, queries)]
    print('%-40s recall@5 %.2f  MRR %.2f' % (res[0]['name'], res[0]['recall'], res[0]['mrr']))
    for m in a.models:
        try:
            r = run('смысл: %s' % m,
                    lambda q, m=m: _search.ranking([s + 1 for s in _search.vector_scores(q, docs, m)]), queries)
        except _search.SearchError as e:
            print('%s пропущена: %s' % (m, e))
            continue
        res.append(r)
        print('%-40s recall@5 %.2f  MRR %.2f' % (r['name'], r['recall'], r['mrr']))

    vec = [r for r in res[1:]]
    if not vec:
        return 1
    best = max(vec, key=lambda r: (r['recall'], r['mrr']))
    bm = best['name'].split(': ', 1)[1]

    def fused_w(w):
        return lambda q: _search.rrf(words(q), _search.ranking([s + 1 for s in _search.vector_scores(q, docs, bm)]),
                                     weights=[w, 1.0])
    for w in (1.0, _search.WORDS_WEIGHT):
        r = run('слова ×%.1f + смысл (%s)' % (w, bm), fused_w(w), queries)
        res.append(r)
        print('%-40s recall@5 %.2f  MRR %.2f' % (r['name'], r['recall'], r['mrr']))
    fused = fused_w(_search.WORDS_WEIGHT)
    for rr in a.rerankers:
        def full(q, rr=rr):
            f = fused(q)
            return _search.rerank(q, docs, f[:_search.CANDIDATES], rr) + f[_search.CANDIDATES:]
        try:
            r = run('слова ×%.1f + смысл + %s' % (_search.WORDS_WEIGHT, rr), full, queries)
        except _search.SearchError as e:
            print('%s пропущен: %s' % (rr, e))
            continue
        res.append(r)
        print('%-40s recall@5 %.2f  MRR %.2f' % (r['name'], r['recall'], r['mrr']))

    out = os.path.join(ROOT, 'tests', 'search-report-%s.md' % date.today().isoformat())
    with io.open(out, 'w', encoding='utf-8') as f:
        f.write('# Замер поиска, %s\n\n' % date.today().isoformat())
        f.write('Дневник-пример: %d записей. Вопросов: %d (tests/search_queries.json).\n\n' % (len(diary), len(queries)))
        f.write('| Способ | recall@5 | MRR | секунд |\n|---|---|---|---|\n')
        for r in res:
            f.write('| %s | %.2f | %.2f | %.0f |\n' % (r['name'], r['recall'], r['mrr'], r['sec']))
        f.write('\n## По вопросам, recall@5\n\n| Вопрос | %s |\n|---|%s\n'
                % (' | '.join(r['name'] for r in res), '---|' * len(res)))
        for i, q in enumerate(queries):
            f.write('| %s | %s |\n' % (q['q'], ' | '.join('%.1f' % r['per'][i] for r in res)))
    print('отчёт:', os.path.relpath(out, ROOT))
    return 0


if __name__ == '__main__':
    sys.exit(main())
