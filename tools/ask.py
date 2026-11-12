#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Написать Лу и дождаться ответа.

    python3 tools/ask.py "Лу, о чём я больше всего писал в июне?"
    python3 tools/ask.py --wait 300 "..."

Дописывает строку {"from": "человек", "at": ..., "text": ...} в inbox.jsonl и ждёт,
пока в outbox.jsonl появится ответ этому собеседнику (по умолчанию до 2 минут).
Сессию сам не запускает: сообщение подхватывает цикл tools/loop.py.
"""
import argparse
import datetime
import io
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('text')
    p.add_argument('--name', default='lu', help='персонаж (папка в agent/)')
    p.add_argument('--from', dest='who', default='человек', help='кто пишет')
    p.add_argument('--wait', type=float, default=120, help='сколько секунд ждать ответа')
    a = p.parse_args()

    home = os.path.join(ROOT, 'agent', a.name)
    if not os.path.isdir(home):
        print('нет такого персонажа: %s' % a.name, file=sys.stderr)
        return 1
    inbox = os.path.join(home, 'inbox.jsonl')
    outbox = os.path.join(home, 'outbox.jsonl')

    try:
        start = os.path.getsize(outbox)
    except OSError:
        start = 0

    line = json.dumps({'from': a.who,
                       'at': datetime.datetime.now().isoformat(timespec='seconds'),
                       'text': a.text}, ensure_ascii=False)
    with io.open(inbox, 'a', encoding='utf-8', newline='\n') as f:
        f.write(line + '\n')
    print('отправлено, жду ответа до %d с…' % a.wait, file=sys.stderr)

    deadline = time.time() + a.wait
    last_reply = None
    while True:
        try:
            with io.open(outbox, 'rb') as f:
                f.seek(start)
                chunk = f.read()
        except OSError:
            chunk = b''
        # Читаем только целые строки: ответ мог быть дописан наполовину.
        end = chunk.rfind(b'\n')
        if end >= 0:
            start += end + 1
            for raw in chunk[:end].decode('utf-8', errors='replace').splitlines():
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get('to') == a.who and m.get('kind') != 'ticket':
                    print('Лу: %s' % m.get('text', ''), flush=True)
                    last_reply = time.time()
        # Лу иногда отвечает несколькими сообщениями подряд: после ответа ждём ещё 3 секунды тишины.
        if last_reply is not None and time.time() - last_reply >= 3:
            return 0
        if last_reply is None and time.time() >= deadline:
            print('за %d с ответа нет. Цикл tools/loop.py запущен?' % a.wait, file=sys.stderr)
            return 3
        time.sleep(1)


if __name__ == '__main__':
    sys.exit(main())
