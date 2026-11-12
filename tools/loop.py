#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Цикл автономии: персонаж просыпается сам и засыпает сам.

    LU_API_KEY=... LU_DIARY_FILE=demo/дневник.json python3 tools/loop.py lu
    python3 tools/loop.py lu --from-operator "назначь себе сон на 5 минут"

Запускается один раз и живёт, пока его не остановят (Ctrl+C, SIGTERM или файл
agent/<имя>/state/STOP). Между сессиями ждёт ближайшего из трёх событий:

    таймер            наступило время из state/next_wake.txt
    новое сообщение   в inbox.jsonl выросли байты, проверка раз в 5 секунд
    новые записи      в дневнике появились записи, проверка раз в 5 минут

После сессии читает state/sleep.txt (минуты, их пишет инструмент sleep), ограничивает
5–1440 минутами и записывает абсолютное время в state/next_wake.txt. Если персонаж не
назначил сон, ставится 60 минут.

Защита: блокировка state/session.lock (её берёт сама сессия), дневной бюджет
LU_DAILY_TOKENS в state/budget.json, файл STOP, повтор через 10 минут при ошибке модели
и тикет оператору после трёх ошибок подряд.

Журнал: logs/<дата>.jsonl, по строке на событие.
"""
import argparse
import datetime
import io
import json
import os
import signal
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
sys.path.insert(0, os.path.join(ROOT, 'agent', 'scripts'))
import run_session  # noqa: E402
import _diary  # noqa: E402

INBOX_POLL = float(os.environ.get('LU_INBOX_POLL_SEC', '5'))
DIARY_POLL = float(os.environ.get('LU_DIARY_POLL_SEC', '300'))
DAILY_TOKENS = int(os.environ.get('LU_DAILY_TOKENS', '150000'))
SLEEP_MIN, SLEEP_MAX, SLEEP_DEFAULT = 5, 1440, 60
RETRY_MIN = 10
ERRORS_BEFORE_TICKET = 3


def now():
    return datetime.datetime.now().replace(microsecond=0)


def iso(t):
    return t.isoformat(timespec='seconds')


class Loop(object):
    def __init__(self, name, quiet=False):
        self.name = name
        self.quiet = quiet
        self.home = os.path.join(ROOT, 'agent', name)
        self.state = os.path.join(self.home, 'state')
        self.inbox = os.path.join(self.home, 'inbox.jsonl')
        self.outbox = os.path.join(self.home, 'outbox.jsonl')
        self.logs = os.path.join(ROOT, 'logs')
        os.makedirs(self.state, exist_ok=True)
        os.makedirs(self.logs, exist_ok=True)

    def p(self, *parts):
        return os.path.join(self.state, *parts)

    # --- журнал и тикеты ---

    def log(self, event, **kw):
        t = now()
        row = dict(at=iso(t), event=event, **kw)
        line = json.dumps(row, ensure_ascii=False)
        path = os.path.join(self.logs, '%s.jsonl' % t.strftime('%Y-%m-%d'))
        with io.open(path, 'a', encoding='utf-8', newline='\n') as f:
            f.write(line + '\n')
        if not self.quiet:
            print(line, flush=True)

    def ticket(self, text):
        line = json.dumps({'to': 'operator', 'kind': 'ticket', 'from': 'loop',
                           'at': iso(now()), 'text': text}, ensure_ascii=False)
        with io.open(self.outbox, 'a', encoding='utf-8', newline='\n') as f:
            f.write(line + '\n')
        self.log('ticket', text=text)

    # --- состояние ---

    def stopped(self):
        return os.path.exists(self.p('STOP'))

    def inbox_size(self):
        try:
            return os.path.getsize(self.inbox)
        except OSError:
            return 0

    def read_next_wake(self):
        try:
            s = io.open(self.p('next_wake.txt'), encoding='utf-8').read().strip()
            return datetime.datetime.fromisoformat(s)
        except (OSError, ValueError):
            return None

    def write_next_wake(self, t):
        io.open(self.p('next_wake.txt'), 'w', encoding='utf-8', newline='\n').write(iso(t) + '\n')

    def sleep_mtime(self):
        try:
            return os.path.getmtime(self.p('sleep.txt'))
        except OSError:
            return None

    def read_sleep_minutes(self):
        try:
            raw = io.open(self.p('sleep.txt'), encoding='utf-8').read().strip()
            return float(raw.replace(',', '.'))
        except (OSError, ValueError):
            return None

    # --- бюджет ---

    def budget(self):
        today = now().strftime('%Y-%m-%d')
        try:
            b = json.loads(io.open(self.p('budget.json'), encoding='utf-8').read())
        except (OSError, ValueError):
            b = {}
        if b.get('date') != today:
            b = {'date': today, 'spent': 0, 'ticket': False}
        return b

    def save_budget(self, b):
        io.open(self.p('budget.json'), 'w', encoding='utf-8', newline='\n').write(
            json.dumps(b, ensure_ascii=False) + '\n')

    # --- дневник ---

    def diary_seen(self):
        try:
            return json.loads(io.open(self.p('diary_seen.json'), encoding='utf-8').read())
        except (OSError, ValueError):
            return None

    def diary_snapshot(self):
        """Последняя дата в дневнике и сколько записей с этой даты. None, если дневник не подключен."""
        try:
            rows = _diary.entries(None, None, None, 10 ** 6)
        except _diary.NotConfigured:
            return None
        if not rows:
            return {'max_date': '', 'count': 0}
        top = max(r['date'] for r in rows)
        return {'max_date': top, 'count': sum(1 for r in rows if r['date'] >= top)}

    def remember_diary(self):
        snap = self.diary_snapshot()
        if snap is not None:
            io.open(self.p('diary_seen.json'), 'w', encoding='utf-8', newline='\n').write(
                json.dumps(snap, ensure_ascii=False) + '\n')

    def new_diary_since(self):
        """Дата, с которой появились новые записи, или None."""
        seen = self.diary_seen()
        if seen is None:
            self.remember_diary()
            return None
        since = seen.get('max_date') or None
        try:
            rows = _diary.entries(None, since, None, 10 ** 6)
        except _diary.NotConfigured:
            return None
        if len(rows) > seen.get('count', 0):
            return since or min(r['date'] for r in rows)
        return None

    # --- главный цикл ---

    def run(self, from_operator=None):
        if self.stopped():
            print('есть файл %s, цикл не стартует. Удалите его и запустите снова.' % self.p('STOP'),
                  file=sys.stderr)
            return 0

        next_wake = self.read_next_wake()
        timer_reason = 'таймер'
        if next_wake is None:
            next_wake = now()
            timer_reason = 'запуск цикла, времени пробуждения ещё не было'
        self.log('loop_start', next_wake=iso(next_wake), pid=os.getpid())

        inbox_seen = self.inbox_size()
        if self.diary_seen() is None:
            self.remember_diary()
        last_diary_check = time.time()
        errors = 0
        blocked_until = None

        while True:
            if self.stopped():
                self.log('loop_stop', why='файл STOP')
                return 0

            t = now()
            if blocked_until and t < blocked_until:
                time.sleep(INBOX_POLL)
                continue
            if blocked_until:
                blocked_until = None
                self.log('budget_reset')

            reason = None
            size = self.inbox_size()
            if size > inbox_seen:
                reason = 'новое сообщение'
            elif t >= next_wake:
                reason = timer_reason
            elif time.time() - last_diary_check >= DIARY_POLL:
                last_diary_check = time.time()
                since = self.new_diary_since()
                if since:
                    reason = 'новые записи с %s' % since
            elif size < inbox_seen:
                inbox_seen = size  # файл почистили руками

            if reason is None:
                left = (next_wake - t).total_seconds()
                time.sleep(max(0.5, min(INBOX_POLL, left)))
                continue

            b = self.budget()
            if b['spent'] >= DAILY_TOKENS:
                midnight = datetime.datetime.combine(t.date() + datetime.timedelta(days=1),
                                                     datetime.time())
                self.log('budget_exceeded', spent=b['spent'], limit=DAILY_TOKENS,
                         until=iso(midnight), skipped_reason=reason)
                if not b.get('ticket'):
                    self.ticket('Дневной бюджет %d токенов исчерпан (%d). Сессии остановлены до '
                                'полуночи.' % (DAILY_TOKENS, b['spent']))
                    b['ticket'] = True
                    self.save_budget(b)
                blocked_until = midnight
                if next_wake < midnight:
                    next_wake = midnight
                    timer_reason = 'таймер, после паузы по бюджету'
                    self.write_next_wake(next_wake)
                continue

            # --- сессия ---
            inbox_before = self.inbox_size()
            sleep_before = self.sleep_mtime()
            self.log('wake', reason=reason)
            spent, code = run_session.run(
                self.name, from_operator, wake_reason=reason, quiet=self.quiet,
                on_event=lambda ev: self.log(ev.pop('event'), **ev))
            from_operator = None
            b = self.budget()
            b['spent'] += spent
            self.save_budget(b)
            self.log('session_end', code=code, tokens=spent, tokens_today=b['spent'])

            if code == 5:
                self.log('loop_stop', why='файл STOP во время сессии')
                return 0
            if code in (1, 2):
                self.log('loop_stop', why='сессия не может стартовать, код %d' % code)
                return code
            if code == 4:
                # Другая сессия держит замок. Ждём её и пробуем снова, причина та же.
                time.sleep(30)
                continue

            inbox_seen = inbox_before
            last_diary_check = time.time()
            self.remember_diary()

            if code == 3:
                errors += 1
                next_wake = now() + datetime.timedelta(minutes=RETRY_MIN)
                timer_reason = 'повтор после ошибки модели'
                inbox_seen = self.inbox_size()  # сообщение подхватится повтором
                self.write_next_wake(next_wake)
                self.log('model_error_retry', errors_in_row=errors, next_wake=iso(next_wake))
                if errors == ERRORS_BEFORE_TICKET:
                    self.ticket('Модель не отвечает %d раз подряд. Цикл продолжает повторять раз в '
                                '%d минут, подробности в журнале logs/.' % (errors, RETRY_MIN))
                continue
            errors = 0

            minutes = None
            if self.sleep_mtime() != sleep_before:
                minutes = self.read_sleep_minutes()
            asked, note = minutes, None
            if minutes is None:
                minutes = SLEEP_DEFAULT
                note = 'уснул без назначения'
            clamped = max(SLEEP_MIN, min(SLEEP_MAX, minutes))
            if clamped == int(clamped):
                clamped = int(clamped)
            next_wake = now() + datetime.timedelta(minutes=clamped)
            timer_reason = 'таймер'
            self.write_next_wake(next_wake)
            ev = dict(asked=asked, minutes=clamped, next_wake=iso(next_wake))
            if note:
                ev['note'] = note
            self.log('sleep', **ev)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('name')
    p.add_argument('--from-operator', dest='from_operator',
                   help='сообщение оператора, уходит только в первую сессию')
    p.add_argument('--quiet', action='store_true')
    a = p.parse_args()

    # Скрипты и LU_DIARY_FILE считают пути от корня репозитория.
    os.chdir(ROOT)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    loop = Loop(a.name, a.quiet)
    try:
        return loop.run(a.from_operator)
    except KeyboardInterrupt:
        loop.log('loop_stop', why='сигнал')
        return 0


if __name__ == '__main__':
    sys.exit(main())
