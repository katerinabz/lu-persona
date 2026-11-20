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
        with io.open(os.path.join(*parts), encoding='utf-8') as f:
            return f.read().strip()
    except OSError:
        return ''


# Пометка перед сообщением в промпте, по режиму, который включил человек.
MARKS = {'luchik': '[режим Лучика] ', 'chat': '[разговор] '}


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
        with io.open(p, encoding='utf-8') as f:
            lines = f.read().splitlines()
        for line in lines:
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


def support(name):
    """Тексты, числа из которых Лу вправе называть без вызова инструмента."""
    home = os.path.join(AGENTS, name)
    parts = [datetime.date.today().isoformat()]
    parts += [str(m.get('text', '')) for m in unanswered(home) if not m.get('_broken')]
    parts.append(read(home, 'memory', 'state.md') or '')
    return '\n'.join(parts)


def compose(name, from_operator=None, wake_reason=None):
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

    if wake_reason:
        add('=== ПОЧЕМУ ТЫ ПРОСНУЛСЯ ===')
        add('')
        add(wake_reason)
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
                add('- %s%s в %s: %s' % (MARKS.get(m.get('persona'), ''),
                                         m.get('from'), m.get('at'), m.get('text')))
        add('')

    luchik = [m for m in pending if m.get('persona') == 'luchik']
    if luchik and len(luchik) == len(pending):
        # Все ждущие включили Лучика: личность Лу в промпт не идёт, иначе её «советов нет»
        # перевешивает и модель отвечает как Лу (прогон 18.11, два вопроса из двух).
        add('--- КТО ТЫ СЕЙЧАС: ЛУЧИК ---')
        add('')
        add('Человек собрал 10 солнышек и включил режим Лучика. В этой сессии ты Лучик, не Лу. '
            'Отвечаешь тепло, на «ты», с «!» и ☀, и можешь дать совет по записям. '
            'Проверка ответа требует хотя бы один «!» или ☀ и не пропустит фразу «совет я не даю».')
        add('')
        add(read(AGENTS, 'lu', 'LUCHIK.md') or '(нет LUCHIK.md)')
        add('')
        luchik = []
    else:
        add('--- КТО ТЫ ---')
        add('')
        add(read(home, 'PERSONA.md') or '(нет PERSONA.md)')
        add('')
    if luchik:
        add('--- РЕЖИМ ЛУЧИКА ---')
        add('')
        add('Сообщения с пометкой [режим Лучика] пришли от человека, который включил режим Лучика. На них отвечай')
        add('как Лучик по правилам ниже: они заменяют правила 2, 3 и 5 Лу только для этих сообщений.')
        add('Проверка ответа для них пропускает совет по записям, «!» и ☀. Остальные сообщения — обычный Лу.')
        add('')
        add(read(AGENTS, 'lu', 'LUCHIK.md') or '(нет LUCHIK.md)')
        add('')

    if any(m.get('persona') == 'chat' for m in pending):
        add('--- ГОЛОС В РАЗГОВОРЕ ---')
        add('')
        add('На сообщения с пометкой [разговор] отвечай голосом из этого файла. Правила Лу остаются,')
        add('меняется манера; проверка ответа пропускает одно эмодзи из 🫧 🌙 ☁️, «!» по-прежнему нельзя.')
        add('')
        add(read(AGENTS, 'lu', 'CHAT_STYLE.md') or '(нет CHAT_STYLE.md)')
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
    p.add_argument('--wake-reason', dest='wake_reason')
    a = p.parse_args()
    out = compose(a.name, a.from_operator, a.wake_reason)
    if out is None:
        return 1
    print(out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
