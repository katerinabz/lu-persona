#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Цело ли рабочее место персонажа.

    python3 tools/check.py

Четыре вопроса, все про факты. Здесь ничего не судит о том, хорош ли персонаж: это чтение,
а чтение работа человека.

    есть ли у персонажа файлы, которых ждёт цикл
    ведёт ли каждая [[ссылка]] на существующую заметку
    не вырос ли файл состояния настолько, что становится проблемой контекста
    разбирается ли каждая строка входящих и исходящих как JSON

Про третий стоит сказать отдельно. Правильного размера у файла памяти нет, поэтому здесь его и
не выдумывают: предупреждение на 16 КБ, примерно там состояние начинает стоить дороже, чем оно
того стоит, на каждой сессии. Печатается число, а не приговор. Решение сжимать остаётся за тем,
кто это прочитает.
"""
import glob
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, 'agent')

EXPECTED = ('PERSONA.md', 'SESSION_PROMPT.md', 'GOALS.md', 'memory/README.md', 'memory/state.md')
STATE_WARN_BYTES = 16 * 1024
LINK = re.compile(r'\[\[([^\]]+)\]\]')


def characters():
    """Папка под agent/, в которой лежит PERSONA.md, это персонаж. Ничего не зашито."""
    out = []
    for d in sorted(glob.glob(os.path.join(AGENTS, '*'))):
        if os.path.isdir(d) and os.path.exists(os.path.join(d, 'PERSONA.md')):
            out.append(os.path.basename(d))
    return out


def main():
    who = characters()
    if not who:
        print('НЕ УСТАНОВЛЕНО: ни в одной папке под %s нет PERSONA.md' % AGENTS)
        return 2

    print('ПЕРСОНАЖИ: %s' % ', '.join(who))
    bad, notes = [], []

    for name in who:
        home = os.path.join(AGENTS, name)

        for rel in EXPECTED:
            if not os.path.exists(os.path.join(home, *rel.split('/'))):
                bad.append('у %s нет %s' % (name, rel))

        ents = {os.path.basename(p)[:-3]
                for p in glob.glob(os.path.join(home, 'memory', 'entities', '*.md'))}
        for p in glob.glob(os.path.join(home, 'memory', '**', '*.md'), recursive=True):
            src = os.path.relpath(p, ROOT).replace(os.sep, '/')
            try:
                body = io.open(p, encoding='utf-8').read()
            except OSError:
                continue
            for target in set(LINK.findall(body)):
                if target == name or target in ents:
                    continue
                bad.append('%s ссылается на [[%s]], а такой заметки нет' % (src, target))

        sp = os.path.join(home, 'memory', 'state.md')
        if os.path.exists(sp):
            n = os.path.getsize(sp)
            if n > STATE_WARN_BYTES:
                notes.append('state.md у %s весит %.1f КБ. Ничего не сломано: это размер, '
                             'начиная с которого сжатие окупается на каждой сессии.'
                             % (name, n / 1024.0))

        for box in ('inbox.jsonl', 'outbox.jsonl'):
            p = os.path.join(home, box)
            if not os.path.exists(p):
                continue
            for i, line in enumerate(io.open(p, encoding='utf-8'), 1):
                if not line.strip():
                    continue
                try:
                    json.loads(line)
                except ValueError as e:
                    bad.append('%s/%s строка %d не разбирается как JSON: %s' % (name, box, i, e))

    for n in notes:
        print('   заметка: %s' % n)
    if bad:
        print()
        for b in bad:
            print('  %s' % b)
        print()
        print('проблем: %d' % len(bad))
        return 1
    print('Рабочее место цело: файлы на месте, ссылки ведут куда надо, строки разбираются.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
