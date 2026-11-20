# -*- coding: utf-8 -*-
"""Опора проверки чисел: слова человека, дата и память, без времени сессии и текста правил."""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import compose_session  # noqa: E402


def test_support_has_messages_and_memory_not_rules(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_session, 'AGENTS', str(tmp_path))
    home = tmp_path / 'users' / '1-demo'
    (home / 'memory').mkdir(parents=True)
    (home / 'PERSONA.md').write_text('Правило 50: пример [N].', encoding='utf-8')
    (home / 'memory' / 'state.md').write_text('про сон 9 записей', encoding='utf-8')
    (home / 'inbox.jsonl').write_text(json.dumps({'from': 'tg:1', 'at': '2026-11-18T23:50:00',
                                                  'text': 'почему 3 раза плохо спала?'}, ensure_ascii=False) + '\n',
                                      encoding='utf-8')
    s = compose_session.support('users/1-demo')
    assert '9 записей' in s and '3 раза' in s
    assert '50' not in s
