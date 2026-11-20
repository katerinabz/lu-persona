# -*- coding: utf-8 -*-
"""Поиск по дневнику без сети: слова, слияние, честный откат на BM25."""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'agent', 'scripts'))
import _search  # noqa: E402


def test_stem_joins_word_forms():
    assert _search.stem('задержалась') == _search.stem('задержался')
    assert _search.stem('записи') == _search.stem('запись') or _search.stem('записи') == _search.stem('записей')


def test_bm25_finds_word_forms():
    docs = ['опять задержалась допоздна', 'зал, ноги', 'заплатила за курс']
    order = _search.ranking(_search.bm25_scores('задерживаюсь на работе допоздна', docs))
    assert order[0] == 0
    assert 1 not in order


def test_rrf_rewards_agreement():
    assert _search.rrf([0, 1, 2], [2, 0, 1])[0] == 0
    assert set(_search.rrf([0], [1])) == {0, 1}
    # малый вес у первого списка: побеждает порядок второго
    assert _search.rrf([0, 1], [1, 0], weights=[0.1, 1.0])[0] == 1


def test_rank_falls_back_to_words_without_network(monkeypatch):
    def boom(*a, **k):
        raise _search.SearchError('сети нет')
    monkeypatch.setattr(_search, 'vector_scores', boom)
    order, how = _search.rank('допоздна', ['опять задержалась допоздна', 'зал, ноги'])
    assert order == [0]
    assert how.startswith('только слова')


def test_rerank_keeps_unscored_candidates(monkeypatch):
    monkeypatch.setattr(_search, '_post', lambda path, body: {'results': [{'index': 1, 'relevance_score': 0.9}]})
    assert _search.rerank('q', ['a', 'b', 'c'], [0, 1, 2], 'x') == [1, 0, 2]


def test_search_script_prints_how(tmp_path):
    diary = tmp_path / 'd.json'
    diary.write_text(json.dumps([{'date': '2026-06-09', 'text': 'опять задержалась допоздна', 'topics': ['работа']},
                                 {'date': '2026-06-13', 'text': 'зал, ноги', 'topics': ['спорт']}],
                                ensure_ascii=False), encoding='utf-8')
    env = dict(os.environ, LU_DIARY_FILE=str(diary), LU_EMBED_BASE='http://127.0.0.1:9',
               LU_EMBED_KEY='x', LU_VEC_CACHE=str(tmp_path / 'c.db'), LU_SEARCH_ATTEMPTS='1')
    out = subprocess.run([os.path.join(ROOT, 'agent', 'scripts', 'search.sh'), 'допоздна'],
                         env=env, capture_output=True, text=True).stdout
    assert out.splitlines()[0].startswith('поиск: только слова')
    assert '2026-06-09' in out and '2026-06-13' not in out
