#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свой ключ модели у человека из Телеграма: узнать сервис по ключу и проверить его.

Лу говорит с моделью по протоколу, совместимому с OpenAI (run_session.call), поэтому подходит
любой такой сервис. Сервис узнаётся по началу ключа, модель берётся из окружения:

    sk-or-...   OpenRouter   LU_BYOK_OPENROUTER_MODEL
    sk-...      OpenAI       LU_BYOK_OPENAI_MODEL
    остальное   NeuralDeep   та же модель, что у Лу по умолчанию (LU_MODEL)

Проверка — один короткий запрос к модели. Ключ никуда не пишется и в журнал не попадает.
"""
import json
import os
import re
import urllib.error
import urllib.request

NEURALDEEP = ('NeuralDeep', os.environ.get('LU_API_BASE', 'https://api.neuraldeep.tech/v1'),
              os.environ.get('LU_MODEL', 'qwen3.6-35b-a3b-noreason'))
OPENROUTER = ('OpenRouter', 'https://openrouter.ai/api/v1',
              os.environ.get('LU_BYOK_OPENROUTER_MODEL', 'qwen/qwen3-235b-a22b-2507'))
OPENAI = ('OpenAI', 'https://api.openai.com/v1', os.environ.get('LU_BYOK_OPENAI_MODEL', 'gpt-4.1-mini'))


def looks_like_key(text):
    """Похоже ли сообщение на ключ: одно «слово» без пробелов, 20–300 знаков."""
    t = (text or '').strip()
    return bool(re.fullmatch(r'[A-Za-z0-9_\-\.:]{20,300}', t))


def provider(key):
    if key.startswith('sk-or-'):
        return OPENROUTER
    if key.startswith('sk-'):
        return OPENAI
    return NEURALDEEP


def check(key, timeout=20):
    """Проверить ключ. Возвращает (сервис, base, model, None) или (сервис, None, None, причина)."""
    name, base, model = provider(key)
    body = {'model': model, 'messages': [{'role': 'user', 'content': 'Ответь одним словом: да.'}],
            'max_tokens': 5}
    r = urllib.request.Request(base + '/chat/completions', data=json.dumps(body).encode(),
                               headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            json.loads(resp.read())
        return name, base, model, None
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return name, None, None, '%s не принял ключ' % name
        if e.code == 402:
            return name, None, None, 'на счёте %s нет денег' % name
        if e.code == 404:
            return name, None, None, 'у %s нет модели %s' % (name, model)
        if e.code == 429:
            return name, base, model, None  # ключ рабочий, просто лимит запросов в минуту
        return name, None, None, '%s ответил ошибкой %s' % (name, e.code)
    except (urllib.error.URLError, OSError, ValueError):
        return name, None, None, '%s не отвечает' % name
