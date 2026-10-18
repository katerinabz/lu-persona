#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор аргументов для трёх читающих скриптов. Печатает простой результат."""
import argparse
import sys

import _diary


def main():
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument('what', choices=('entries', 'topics', 'link'))
    p.add_argument('rest', nargs='*')
    p.add_argument('--topic')
    p.add_argument('--since')
    p.add_argument('--until')
    p.add_argument('--limit', type=int, default=50)
    a = p.parse_args()

    try:
        if a.what == 'entries':
            rows = _diary.entries(a.topic, a.since, a.until, a.limit)
            for r in rows:
                print('%s | %s | %s' % (r['date'], ','.join(r.get('topics', [])), r['text']))
            if not rows:
                print('записей по этому запросу нет')
        elif a.what == 'topics':
            for name, n in _diary.topics(a.since, a.until):
                print('%s %d' % (name, n))
        else:
            if len(a.rest) < 2:
                print('нужны две темы: link.sh <тема> <тема>', file=sys.stderr)
                return 2
            n, dates = _diary.link(a.rest[0], a.rest[1])
            print('%d совместных упоминаний: %s' % (n, ', '.join(dates)))
    except _diary.NotConfigured as e:
        print('ИНСТРУМЕНТ НЕ РАБОТАЕТ: %s' % e, file=sys.stderr)
        return 3
    return 0


if __name__ == '__main__':
    sys.path.insert(0, __file__.rsplit('/', 1)[0])
    sys.exit(main())
