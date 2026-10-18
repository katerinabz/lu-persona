#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Собрать промпт одной сессии персонажа.

    python3 tools/compose_session.py lu
    python3 tools/compose_session.py lu --from-operator "перестань представляться, это уже сделано"

Читает файлы персонажа и то, что пришло с прошлого раза, и печатает один промпт в stdout.
Печать в stdout сделана нарочно: это работает с любым раннером, читается человеком и
сравнивается между двумя запусками, чтобы увидеть, что именно изменилось.

Этот файл ничего не решает. В нём нет ни одного правила про конкретного персонажа и ни одного
особого случая, и так должно остаться. Первый же `if name == "lu"` превращает всё обратно в
промпт с лишними шагами.

Порядок важен. То, что пришло один раз, идёт в самом верху, потому что сессия, прочитавшая
только первый экран, всё равно это увидит. Дальше кто ты, что ты делаешь, постоянная
инструкция, правила.
"""
import argparse
import datetime
import io
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AGENTS = os.path.join(ROOT, 'agent')


def read(*parts):
    try:
        return io.open(os.path.join(*parts), encoding='utf-8').read().strip()
    except OSError:
        return ''


def unanswered(home):
    """Строки входящих, после которых нет ответа этому человеку.

    Нарочно простое правило, и простое в сторону большей работы: если непонятно, строка
    показывается. Пропущенное сообщение это проигнорированный человек, а лишнее это персонаж,
    сказавший одно и то же дважды, что неловко, но поправимо.
    """
    inbox, outbox = [], []
    for name, into in (('inbox.jsonl', inbox), ('outbox.jsonl', outbox)):
        p = os.path.join(home, name)
        if not os.path.exists(p):
            continue
        for line in io.open(p, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                into.append(json.loads(line))
            except ValueError:
                into.append({'_broken': line[:120]})
    answered_after = {}
    for o in outbox:
        if o.get('to') and o.get('at'):
            answered_after[o['to']] = max(answered_after.get(o['to'], ''), o['at'])
    out = []
    for m in inbox:
        if m.get('_broken'):
            out.append(m)
            continue
        last = answered_after.get(m.get('from'), '')
        if not last or (m.get('at') or '') > last:
            out.append(m)
    return out


def compose(name, from_operator=None):
    home = os.path.join(AGENTS, name)
    if not os.path.isdir(home):
        print('нет такого персонажа: %s' % name, file=sys.stderr)
        return None

    L = []
    add = L.append

    if from_operator:
        add('=== НОВОЕ ОТ ОПЕРАТОРА ===')
        add('')
        add(from_operator)
        add('')
        add('Это доставляется ровно один раз и больше не придёт. До всего остального запиши это')
        add('в GOALS.md пунктом 0 со статусом OPEN и держи открытым, пока дело действительно не')
        add('закончено, сколько бы сессий ни ушло.')
        add('')
        add('=' * 30)
        add('')

    now = datetime.datetime.now()
    add('СЕЙЧАС: %s (%s)' % (now.strftime('%Y-%m-%d %H:%M'), now.strftime('%A')))
    add('')

    pending = unanswered(home)
    if pending:
        add('БЕЗ ОТВЕТА (%d). По каждому сначала прочитай заметку о человеке, чтобы продолжать'
            % len(pending))
        add('разговор, а не начинать его заново.')
        add('')
        for m in pending:
            if m.get('_broken'):
                add('- строка входящих не разобралась: %s' % m['_broken'])
            else:
                add('- %s в %s: %s' % (m.get('from'), m.get('at'), m.get('text')))
        add('')

    add('--- КТО ТЫ ---')
    add('')
    add(read(home, 'PERSONA.md') or '(нет PERSONA.md)')
    add('')

    goals = read(home, 'GOALS.md')
    if goals:
        add('--- ЧТО ТЫ ДЕЛАЕШЬ ---')
        add('')
        add(goals)
        add('')

    state = read(home, 'memory', 'state.md')
    if state:
        add('--- ГДЕ ТЫ ОСТАНОВИЛСЯ ---')
        add('')
        add(state)
        add('')

    add('--- ИНСТРУКЦИЯ СЕССИИ ---')
    add('')
    add(read(home, 'SESSION_PROMPT.md') or '(нет SESSION_PROMPT.md)')
    add('')

    add('--- КАК ЗДЕСЬ УСТРОЕНО ---')
    add('')
    add(read(AGENTS, 'AGENTS.md'))
    add('')
    add(read(AGENTS, 'shared', 'rules.md'))

    return '\n'.join(L)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('name')
    p.add_argument('--from-operator', dest='from_operator')
    a = p.parse_args()
    out = compose(a.name, a.from_operator)
    if out is None:
        return 1
    print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
