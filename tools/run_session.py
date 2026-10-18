#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Раннер: одна сессия персонажа от начала до сна.

    LU_DIARY_FILE=demo/дневник.json python3 tools/run_session.py lu
    python3 tools/run_session.py lu --from-operator "перестань представляться"

Берёт промпт у compose_session.py, отдаёт его модели, выполняет инструменты, которые модель
вызвала, и возвращает ей результат. Так по кругу, пока модель не перестанет вызывать инструменты
или пока не кончится бюджет.

Бюджет здесь не украшение. Персонаж просыпается сам и может проснуться в пустую комнату, поэтому
у сессии есть потолок по шагам и по токенам, и упёршись в него она заканчивается сном, а не
обрывом.

Ключи берутся из окружения:

    LU_API_KEY      ключ
    LU_API_BASE     адрес, совместимый с OpenAI (по умолчанию NeuralDeep)
    LU_MODEL        модель
"""
import argparse
import io
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import compose_session  # noqa: E402

BASE = os.environ.get('LU_API_BASE', 'https://api.neuraldeep.tech/v1')
MODEL = os.environ.get('LU_MODEL', 'qwen3.6-35b-a3b-noreason')
MAX_STEPS = int(os.environ.get('LU_MAX_STEPS', '14'))
MAX_TOKENS = int(os.environ.get('LU_MAX_TOKENS', '12000'))

TOOLS = [
    ('entries', 'Записи дневника. Можно задать тему и период.',
     {'topic': 'string', 'since': 'string', 'until': 'string'}),
    ('topics', 'Сколько записей по каждой теме за период.',
     {'since': 'string', 'until': 'string'}),
    ('link', 'Сколько раз две темы встречались в одной записи.',
     {'a': 'string', 'b': 'string'}),
    ('say', 'Сказать человеку. Это и есть твой ответ ему.',
     {'to': 'string', 'text': 'string'}),
    ('remember', 'Дописать датированную строку в заметку о человеке, теме или периоде.',
     {'entity': 'string', 'text': 'string'}),
    ('read_note', 'Прочитать свою заметку целиком.', {'name': 'string'}),
    ('write_state', 'Перезаписать memory/state.md целиком. Сжимай, а не дописывай.',
     {'text': 'string'}),
    ('sleep', 'Назначить себе следующее пробуждение, в минутах.', {'minutes': 'string'}),
    ('ticket', 'Сообщить оператору, что инструмента не хватает или он сломан.', {'text': 'string'}),
]


def schema():
    out = []
    for name, desc, props in TOOLS:
        out.append({'type': 'function', 'function': {
            'name': name, 'description': desc,
            'parameters': {'type': 'object',
                           'properties': {k: {'type': v} for k, v in props.items()}}}})
    return out


def sh(args, env):
    p = subprocess.run(args, cwd=ROOT, env=env, capture_output=True, text=True)
    out = (p.stdout or '').strip()
    err = (p.stderr or '').strip()
    if p.returncode != 0:
        return (err or out or 'скрипт вернул код %d' % p.returncode)
    return out or 'готово'


def last_sender(home):
    """Кто написал последним. Нужно, чтобы ответ ушёл человеку, а не самому персонажу."""
    p = os.path.join(home, 'inbox.jsonl')
    who = None
    if os.path.exists(p):
        for line in io.open(p, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                who = json.loads(line).get('from') or who
            except ValueError:
                pass
    return who


def run_tool(name, a, who, env):
    home = os.path.join(ROOT, 'agent', who)
    s = os.path.join(ROOT, 'agent', 'scripts')

    if name == 'entries':
        cmd = [os.path.join(s, 'entries.sh')]
        for k in ('topic', 'since', 'until'):
            if a.get(k):
                cmd += ['--' + k, str(a[k])]
        return sh(cmd, env)
    if name == 'topics':
        cmd = [os.path.join(s, 'topics.sh')]
        for k in ('since', 'until'):
            if a.get(k):
                cmd += ['--' + k, str(a[k])]
        return sh(cmd, env)
    if name == 'link':
        return sh([os.path.join(s, 'link.sh'), str(a.get('a', '')), str(a.get('b', ''))], env)
    if name == 'say':
        # Кому говорить, решает не модель: она регулярно путает себя с собеседником и пишет
        # ответ на своё же имя. Адресат берётся из последней входящей строки.
        to = str(a.get('to') or '').strip()
        if not to or to.lower() in (who.lower(), 'лу', 'lu'):
            to = last_sender(home) or 'человек'
        return sh([os.path.join(s, 'say.sh'), to, str(a.get('text', ''))], env)
    if name == 'remember':
        return sh([os.path.join(s, 'remember.sh'), str(a.get('entity', '')), str(a.get('text', ''))], env)
    if name == 'sleep':
        return sh([os.path.join(s, 'sleep.sh'), str(a.get('minutes', '60'))], env)
    if name == 'ticket':
        return sh([os.path.join(s, 'ticket.sh'), str(a.get('text', ''))], env)
    if name == 'read_note':
        p = os.path.join(home, 'memory', 'entities', '%s.md' % a.get('name', ''))
        if not os.path.exists(p):
            return 'заметки нет, её ещё не заводили'
        return io.open(p, encoding='utf-8').read()
    if name == 'write_state':
        p = os.path.join(home, 'memory', 'state.md')
        io.open(p, 'w', encoding='utf-8', newline='\n').write(str(a.get('text', '')).strip() + '\n')
        return 'состояние переписано, %d знаков' % len(str(a.get('text', '')))
    return 'нет такого инструмента'


def call(messages, key):
    body = {'model': MODEL, 'messages': messages, 'tools': schema(), 'max_tokens': 1600}
    r = urllib.request.Request(BASE + '/chat/completions', data=json.dumps(body).encode(),
                               headers={'Authorization': 'Bearer ' + key,
                                        'Content-Type': 'application/json'})
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('name')
    p.add_argument('--from-operator', dest='from_operator')
    p.add_argument('--quiet', action='store_true')
    a = p.parse_args()

    key = os.environ.get('LU_API_KEY') or os.environ.get('NEURAL_DEEP_API_KEY')
    if not key:
        print('нет ключа: задайте LU_API_KEY', file=sys.stderr)
        return 2

    prompt = compose_session.compose(a.name, a.from_operator)
    if prompt is None:
        return 1

    env = dict(os.environ, AGENT_NAME=a.name)
    messages = [{'role': 'system', 'content': prompt},
                {'role': 'user', 'content': 'Начинай сессию.'}]
    spent = 0

    for step in range(1, MAX_STEPS + 1):
        try:
            d = call(messages, key)
        except urllib.error.HTTPError as e:
            print('модель ответила ошибкой %s: %s' % (e.code, e.read().decode()[:300]), file=sys.stderr)
            return 3
        spent += d.get('usage', {}).get('completion_tokens', 0)
        m = d['choices'][0]['message']
        calls = m.get('tool_calls') or []
        messages.append({'role': 'assistant', 'content': m.get('content') or '',
                         'tool_calls': calls})

        if not calls:
            if not a.quiet and (m.get('content') or '').strip():
                print('[%d] Лу говорит в пустоту: %s' % (step, m['content'].strip()[:400]))
            break

        for c in calls:
            fn = c['function']['name']
            try:
                args = json.loads(c['function']['arguments'] or '{}')
            except ValueError:
                args = {}
            result = run_tool(fn, args, a.name, env)
            if not a.quiet:
                short = json.dumps(args, ensure_ascii=False)
                print('[%d] %s %s' % (step, fn, short[:160]))
                print('    → %s' % result.replace('\n', '\n      ')[:400])
            messages.append({'role': 'tool', 'tool_call_id': c['id'], 'content': result[:4000]})

        if spent > MAX_TOKENS:
            messages.append({'role': 'user', 'content':
                             'Бюджет сессии кончился. Перепиши состояние и назначь пробуждение.'})

    if not a.quiet:
        print('\nшагов: %d, токенов потрачено: %d' % (step, spent))
    return 0


if __name__ == '__main__':
    sys.exit(main())
