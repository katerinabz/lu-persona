#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Диспетчер сессий Лу для людей из Телеграма: у каждого своя память и свой дневник.

    LU_API_KEY=... python3 tools/hub.py

Работает рядом с мостом (`tools/tg_bridge.py`). Мост кладёт сообщение человека в его рабочее
место, диспетчер замечает новое и запускает одну сессию Лу именно там.

Рабочее место — `agent/users/<id в Телеграме>-<режим>/`, устроено как `agent/lu/`: свои GOALS,
состояние, заметки, входящие и исходящие. Характер и инструкция общие, их файлы копируются из
`agent/lu/`. Режимов два, и память у них раздельная, чтобы числа из дневника-примера не
попали в разговор о своём дневнике:

    demo   дневник-пример, LU_DIARY_FILE=demo/дневник.json
    own    свой дневник человека, LU_DIARY_DB=data/lu.db и LU_DIARY_USER=<id>

Сессия идёт отдельным процессом `run_session.py`: так одновременно отвечают до трёх людей и
одна упавшая сессия не роняет остальные. Лу здесь сам не просыпается: сессия начинается только
с сообщения человека, иначе десяток дневников тратил бы бюджет впустую.

Бюджет. Сессии на ключе автора считаются в data/hub.json, потолок LU_DAILY_TOKENS в сутки на
всех. Кончился — человеку уходит сообщение с предложением привязать свой ключ (/key). Сессии
на своём ключе человека в общий потолок не входят.

Сообщения диспетчера человеку пишутся в исходящие рабочего места с kind=system: мост
доставляет их обычным текстом, а Лу в следующей сессии видит, что сообщение не осталось
без ответа.
"""
import datetime
import io
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import compose_session  # noqa: E402
import tg_store  # noqa: E402

AGENTS = os.path.join(ROOT, 'agent')
USERS = os.path.join(AGENTS, 'users')
TEMPLATE = os.path.join(ROOT, 'agent', 'template')
LU = os.path.join(ROOT, 'agent', 'lu')
DEMO = os.path.join(ROOT, 'demo', 'дневник.json')
STATE = os.path.join(ROOT, 'data', 'hub.json')
LOGS = os.path.join(ROOT, 'logs')

DAILY_TOKENS = int(os.environ.get('LU_DAILY_TOKENS', '150000'))
WORKERS = int(os.environ.get('LU_HUB_WORKERS', '3'))
RETRIES = 2
POLL = 1.5

LIMIT_TOKENS = ('Лу на сегодня устал: общий лимит запросов к модели закончился, он обновится в '
                'полночь. Если у вас есть свой API-ключ, привяжите его командой /key, и Лу ответит сейчас.')
MODEL_DOWN = 'Лу сейчас не может ответить: модель недоступна. Сообщение сохранено, попробуйте написать позже.'
KEY_FAILED = ('Ваш ключ не сработал, модель ответила ошибкой. Проверьте ключ или отвяжите его '
              'командой /key_off, тогда Лу ответит на ключе автора.')
NO_ANSWER = 'Лу не сформулировал ответ. Попробуйте спросить иначе.'


def now():
    return datetime.datetime.now().replace(microsecond=0)


# --- рабочие места ---------------------------------------------------------------------

def name_of(tg_id, mode):
    return 'users/%s-%s' % (tg_id, mode)


def home_of(name):
    return os.path.join(AGENTS, name)


def ensure_workspace(tg_id, mode):
    """Завести рабочее место по шаблону, если его нет. Возвращает имя для run_session."""
    name = name_of(tg_id, mode)
    home = home_of(name)
    if os.path.isdir(home):
        return name
    tmp = home + '.tmp'
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(TEMPLATE, tmp)
    for rel in ('PERSONA.md', 'SESSION_PROMPT.md', os.path.join('memory', 'README.md')):
        shutil.copy(os.path.join(LU, rel), os.path.join(tmp, rel))
    os.makedirs(os.path.join(tmp, 'state'), exist_ok=True)
    for f in ('inbox.jsonl', 'outbox.jsonl'):
        io.open(os.path.join(tmp, f), 'w').close()
    os.replace(tmp, home)
    return name


def parse(name):
    m = re.match(r'users/(-?\d+)-(demo|own)$', name)
    return (m.group(1), m.group(2)) if m else (None, None)


def system_message(name, tg_id, text):
    line = {'to': 'tg:%s' % tg_id, 'kind': 'system', 'at': now().isoformat(timespec='seconds'), 'text': text}
    with io.open(os.path.join(home_of(name), 'outbox.jsonl'), 'a', encoding='utf-8', newline='\n') as f:
        f.write(json.dumps(line, ensure_ascii=False) + '\n')


def size(path):
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# --- диспетчер -------------------------------------------------------------------------

class Hub(object):
    def __init__(self, store=None, runner=None):
        self.store = store or tg_store.Store()
        self.runner = runner or self.run_session
        self.lock = threading.Lock()
        self.running = set()
        self.s = self.load()

    def load(self):
        try:
            with io.open(STATE, encoding='utf-8') as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        s.setdefault('errors', {})
        s.setdefault('budget', {'date': '', 'spent': 0})
        return s

    def save(self):
        with self.lock:
            os.makedirs(os.path.dirname(STATE), exist_ok=True)
            tmp = STATE + '.tmp'
            with io.open(tmp, 'w', encoding='utf-8') as f:
                f.write(json.dumps(self.s, ensure_ascii=False, indent=1))
            os.replace(tmp, STATE)

    def budget(self):
        b = self.s['budget']
        today = now().date().isoformat()
        if b.get('date') != today:
            b.update(date=today, spent=0)
        return b

    def log(self, event, **kw):
        os.makedirs(LOGS, exist_ok=True)
        row = dict(at=now().isoformat(timespec='seconds'), event=event, **kw)
        with self.lock:
            with io.open(os.path.join(LOGS, 'hub.jsonl'), 'a', encoding='utf-8') as f:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')

    def seen(self, name):
        """Размер входящих, уже отданных Лу. Лежит в самом рабочем месте: удалили место — забыли и это."""
        try:
            with io.open(os.path.join(home_of(name), 'state', 'hub_seen.txt'), encoding='utf-8') as f:
                return int(f.read().strip() or 0)
        except (OSError, ValueError):
            return 0

    def set_seen(self, name, n):
        d = os.path.join(home_of(name), 'state')
        if os.path.isdir(d):
            with io.open(os.path.join(d, 'hub_seen.txt'), 'w', encoding='utf-8') as f:
                f.write('%d\n' % n)

    def pending(self):
        """Рабочие места, где входящие выросли с прошлой сессии."""
        if not os.path.isdir(USERS):
            return []
        out = []
        for d in sorted(os.listdir(USERS)):
            name = 'users/' + d
            if parse(name)[0] is None or name in self.running:
                continue
            n = size(os.path.join(home_of(name), 'inbox.jsonl'))
            if n > self.seen(name):
                out.append(name)
        return out

    def env(self, tg_id, mode, person):
        env = {k: v for k, v in os.environ.items() if not k.startswith('LU_DIARY_')}
        if mode == 'demo':
            env['LU_DIARY_FILE'] = DEMO
        else:
            env['LU_DIARY_DB'] = self.store.path
            env['LU_DIARY_USER'] = str(person['user_id'])
        if person.get('key'):
            env['LU_API_KEY'] = person['key']
            env['LU_API_BASE'] = person['base']
            env['LU_MODEL'] = person['model']
            env.pop('NEURAL_DEEP_API_KEY', None)
        return env

    def run_session(self, name, env):
        """Одна сессия отдельным процессом. Возвращает (код, токенов)."""
        day = now().strftime('%Y-%m-%d')
        log_dir = os.path.join(LOGS, 'sessions', day)
        os.makedirs(log_dir, exist_ok=True)
        p = subprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'run_session.py'), name],
                           cwd=ROOT, env=env, capture_output=True, text=True, timeout=900)
        out = (p.stdout or '') + ('\n' + p.stderr if p.stderr else '')
        with io.open(os.path.join(log_dir, name.replace('/', '_') + '.log'), 'a', encoding='utf-8') as f:
            f.write('=== %s код %d\n%s\n' % (now().isoformat(), p.returncode, out))
        m = re.search(r'токенов потрачено: (\d+)', out)
        return p.returncode, int(m.group(1)) if m else 0

    def handle(self, name):
        tg_id, mode = parse(name)
        home = home_of(name)
        inbox_size = size(os.path.join(home, 'inbox.jsonl'))
        person = self.store.person(tg_id)
        own_key = bool(person.get('key'))
        try:
            if not own_key and self.budget()['spent'] >= DAILY_TOKENS:
                self.log('budget_exceeded', who=name)
                system_message(name, tg_id, LIMIT_TOKENS)
                self.set_seen(name, inbox_size)
                return
            self.log('wake', who=name, own_key=own_key)
            code, spent = self.runner(name, self.env(tg_id, mode, person))
            if not own_key:
                self.budget()['spent'] += spent
            self.log('session_end', who=name, code=code, tokens=spent)

            if code == 4:      # сессия в этом месте уже идёт: подхватится следующим проходом
                return
            if code == 3:
                errs = self.s['errors'].get(name, 0) + 1
                self.s['errors'][name] = errs
                if own_key:
                    system_message(name, tg_id, KEY_FAILED)
                elif errs <= RETRIES:
                    time.sleep(20)
                    return     # входящие не отмечены, повтор на следующем проходе
                else:
                    system_message(name, tg_id, MODEL_DOWN)
            else:
                self.s['errors'].pop(name, None)
                if code == 0 and compose_session.unanswered(home):
                    system_message(name, tg_id, NO_ANSWER)
            self.set_seen(name, inbox_size)
        finally:
            with self.lock:
                self.running.discard(name)
            self.save()

    def run(self):
        print('диспетчер запущен: %s, до %d сессий сразу' % (os.path.relpath(USERS, ROOT), WORKERS), flush=True)
        self.log('hub_start', workers=WORKERS, daily_tokens=DAILY_TOKENS)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            while True:
                for name in self.pending():
                    with self.lock:
                        if len(self.running) >= WORKERS:
                            break
                        self.running.add(name)
                    pool.submit(self._safe, name)
                time.sleep(POLL)

    def _safe(self, name):
        try:
            self.handle(name)
        except Exception as e:  # одна сессия не роняет диспетчер
            self.log('hub_error', who=name, error=repr(e)[:300])
            with self.lock:
                self.running.discard(name)


def main():
    if not (os.environ.get('LU_API_KEY') or os.environ.get('NEURAL_DEEP_API_KEY')):
        print('нет ключа: задайте LU_API_KEY', file=sys.stderr)
        return 2
    os.chdir(ROOT)
    try:
        Hub().run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
