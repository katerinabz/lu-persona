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
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import compose_session  # noqa: E402

BASE = os.environ.get('LU_API_BASE', 'https://api.neuraldeep.tech/v1')
MODEL = os.environ.get('LU_MODEL', 'qwen3.6-35b-a3b-noreason')
MAX_STEPS = int(os.environ.get('LU_MAX_STEPS', '14'))
MAX_TOKENS = int(os.environ.get('LU_MAX_TOKENS', '12000'))
MAX_NUDGES = 2

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


def note_name(raw):
    """Имя заметки без пути и расширения.

    Модель часто передаёт `memory/entities/человек.md` вместо `человек`, и тогда заметка
    заводилась во вложенной папке рядом с настоящей. Каталоги и `.md` отрезаются здесь,
    заодно это не даёт выйти из папки памяти через `..`.
    """
    n = os.path.basename(str(raw or '').strip().replace('\\', '/'))
    if n.endswith('.md'):
        n = n[:-3]
    return n.strip('. ') or 'без имени'


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
        # ответ на своё же имя. Адресат берётся из самого раннего неотвеченного сообщения:
        # когда пишут несколько человек из Телеграма, «последний написавший» уже не тот.
        to = str(a.get('to') or '').strip()
        if not to or to.lower() in (who.lower(), 'лу', 'lu'):
            pending = [m.get('from') for m in compose_session.unanswered(home) if m.get('from')]
            to = (pending[0] if pending else last_sender(home)) or 'человек'
        text = str(a.get('text', ''))
        # Правило «без советов» держит среда, а не только промпт. Сообщение с нарушением не
        # уходит, модель получает причину и пишет заново сама. Счётчик отказов подряд живёт
        # на функции: процесс это одна сессия, отдельное состояние ради одного числа не нужно.
        if s not in sys.path:
            sys.path.insert(0, s)
        import _guard
        found = _guard.check(text, getattr(run_tool, 'support', None))
        if not found:
            run_tool.refusals = 0
            return sh([os.path.join(s, 'say.sh'), to, text], env)
        run_tool.refusals = getattr(run_tool, 'refusals', 0) + 1
        v = found[0]
        if run_tool.refusals < 3:
            return ('не отправлено (отказ %d подряд): найдено «%s», правило %d из PERSONA.md, '
                    'переформулируй. Все нарушения: %s'
                    % (run_tool.refusals, v['fragment'], v['rule'], _guard.describe(found)))
        # Третий отказ подряд: человеку уходит нейтральная строка из данных, без слов модели.
        import _diary
        try:
            rows = _diary.entries(limit=10 ** 6)
        except _diary.NotConfigured:
            rows = []
        names = sorted({t for r in rows for t in r.get('topics', [])})
        topic = next((t for t in names if t in text.lower()), None)
        if topic:
            rows = [r for r in rows if topic in r.get('topics', [])]
        if rows:
            neutral = '%s у вас %d записей, последняя от %s.' % (
                'По теме «%s»' % topic if topic else 'В дневнике', len(rows), rows[-1]['date'])
        else:
            neutral = 'В дневнике пока нет записей.'
        sh([os.path.join(s, 'ticket.sh'),
            'say: три отказа подряд, отправлена нейтральная строка. Последнее нарушение: %s'
            % _guard.describe(found)], env)
        run_tool.refusals = 0
        sent = sh([os.path.join(s, 'say.sh'), to, neutral], env)
        return ('не отправлено: третий отказ подряд (%s). Вместо ответа человеку ушла нейтральная '
                'строка «%s», оператору заведён тикет. %s' % (_guard.describe(found), neutral, sent))
    if name == 'remember':
        # Память попадает в промпт следующих сессий, а значит в опору для say: выдуманное число,
        # записанное в заметку, потом прошло бы проверку как подтверждённое. 12.11 Лу записал
        # «записей 50» при 34 записях в дневнике.
        support = getattr(run_tool, 'support', None)
        if support is not None:
            if s not in sys.path:
                sys.path.insert(0, s)
            import _guard
            bad = [v for v in _guard.check(str(a.get('text', '')), support) if v['kind'] == 'число без опоры']
            if bad:
                return ('не записано: в заметке число, которого нет в данных (%s). Возьми число из ответа '
                        'инструмента и запиши заново.' % _guard.describe(bad))
        return sh([os.path.join(s, 'remember.sh'), note_name(a.get('entity')), str(a.get('text', ''))], env)
    if name == 'sleep':
        return sh([os.path.join(s, 'sleep.sh'), str(a.get('minutes', '60'))], env)
    if name == 'ticket':
        return sh([os.path.join(s, 'ticket.sh'), str(a.get('text', ''))], env)
    if name == 'read_note':
        p = os.path.join(home, 'memory', 'entities', '%s.md' % note_name(a.get('name')))
        if not os.path.exists(p):
            return 'заметки нет, её ещё не заводили'
        return io.open(p, encoding='utf-8').read()
    if name == 'write_state':
        p = os.path.join(home, 'memory', 'state.md')
        io.open(p, 'w', encoding='utf-8', newline='\n').write(str(a.get('text', '')).strip() + '\n')
        return 'состояние переписано, %d знаков' % len(str(a.get('text', '')))
    return 'нет такого инструмента'


def call(messages, key, only=None):
    tools = [t for t in schema() if not only or t['function']['name'] in only]
    body = {'model': MODEL, 'messages': messages, 'tools': tools, 'max_tokens': 1600}
    if only and len(only) == 1:
        body['tool_choice'] = {'type': 'function', 'function': {'name': only[0]}}
    r = urllib.request.Request(BASE + '/chat/completions', data=json.dumps(body).encode(),
                               headers={'Authorization': 'Bearer ' + key,
                                        'Content-Type': 'application/json'})
    # Тариф NeuralDeep ограничен 20 запросами в минуту. Ответ 429 означает «подожди», а не сбой,
    # поэтому сессия ждёт столько, сколько просит сервис, и повторяет, а не обрывается.
    for attempt in range(4):
        try:
            with urllib.request.urlopen(r, timeout=300) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            m = re.search(r'через (\d+)', e.read().decode('utf-8', 'replace'))
            time.sleep(int(m.group(1)) + 2 if m else 30)
        except (urllib.error.URLError, ConnectionError) as e:
            # Обрыв соединения у сервиса (Errno 54) разовый: в прогоне 10.11 он уронил
            # две сессии подряд. Одна пауза и повтор дешевле потерянной сессии.
            if attempt == 3:
                raise
            time.sleep(10)


def stop_file(name):
    return os.path.join(ROOT, 'agent', name, 'state', 'STOP')


def lock_path(name):
    return os.path.join(ROOT, 'agent', name, 'state', 'session.lock')


def acquire_lock(name):
    """Взять блокировку сессии. Возвращает открытый файл или None, если сессия уже идёт.

    Блокировку держит ядро (flock), поэтому упавший процесс её не оставляет: файл
    остаётся, а замок с него снимается вместе со смертью процесса.
    """
    import fcntl
    p = lock_path(name)
    os.makedirs(os.path.dirname(p), exist_ok=True)
    f = io.open(p, 'a+', encoding='utf-8')
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write('%d\n' % os.getpid())
    f.flush()
    return f


def release_lock(f):
    import fcntl
    try:
        f.seek(0)
        f.truncate()
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()


def run(name, from_operator=None, wake_reason=None, quiet=False, on_event=None):
    """Одна сессия. Возвращает (токенов потрачено, код).

    Коды: 0 сессия прошла, 1 нет такого персонажа, 2 нет ключа, 3 ошибка модели,
    4 другая сессия уже идёт, 5 остановлена файлом STOP.
    on_event(dict) получает по событию на каждый вызов инструмента; так цикл ведёт журнал.
    """
    def emit(**ev):
        if on_event:
            on_event(ev)

    key = os.environ.get('LU_API_KEY') or os.environ.get('NEURAL_DEEP_API_KEY')
    if not key:
        print('нет ключа: задайте LU_API_KEY', file=sys.stderr)
        return 0, 2

    lock = acquire_lock(name)
    if lock is None:
        print('сессия уже идёт: %s занят' % lock_path(name), file=sys.stderr)
        return 0, 4
    try:
        return _run_locked(name, key, from_operator, wake_reason, quiet, emit)
    finally:
        release_lock(lock)


def _run_locked(name, key, from_operator, wake_reason, quiet, emit):
    # Отказы проверки считаются в пределах одной сессии, а цикл гоняет много сессий в одном процессе.
    run_tool.refusals = 0
    prompt = compose_session.compose(name, from_operator, wake_reason)
    if prompt is None:
        return 0, 1
    # Промпт уже несёт память, текущее время и сообщения людей: числа оттуда не выдумка.
    run_tool.support = prompt

    env = dict(os.environ, AGENT_NAME=name)
    messages = [{'role': 'system', 'content': prompt},
                {'role': 'user', 'content': 'Начинай сессию.'}]
    spent = 0
    step = 0
    nudges = 0
    home = os.path.join(ROOT, 'agent', name)

    for step in range(1, MAX_STEPS + 1):
        if os.path.exists(stop_file(name)):
            emit(event='stop', step=step)
            if not quiet:
                print('[%d] найден STOP, сессия прервана' % step)
            return spent, 5
        try:
            # Последние два шага с неотвеченным сообщением: из инструментов остаётся только say.
            # Подсказки модель на этом месте пропускала (сценарий 10, 11.11).
            last = step >= MAX_STEPS - 1 and compose_session.unanswered(home)
            d = call(messages, key, only=('say',) if last else None)
        except urllib.error.HTTPError as e:
            msg = 'модель ответила ошибкой %s: %s' % (e.code, e.read().decode(errors='replace')[:300])
            print(msg, file=sys.stderr)
            emit(event='model_error', step=step, error=msg)
            return spent, 3
        except (urllib.error.URLError, OSError, ValueError) as e:
            msg = 'модель недоступна: %s' % e
            print(msg, file=sys.stderr)
            emit(event='model_error', step=step, error=msg)
            return spent, 3
        spent += d.get('usage', {}).get('completion_tokens', 0)
        m = d['choices'][0]['message']
        calls = m.get('tool_calls') or []
        messages.append({'role': 'assistant', 'content': m.get('content') or '',
                         'tool_calls': calls})

        if not calls:
            text = (m.get('content') or '').strip()
            if not quiet and text:
                print('[%d] Лу говорит в пустоту: %s' % (step, text[:400]))
            # Текст без инструмента до человека не доходит. Модель об этом не знает, пока среда не
            # скажет, поэтому ей возвращается факт, а решение остаётся за ней.
            if text and nudges < MAX_NUDGES and compose_session.unanswered(home):
                nudges += 1
                emit(event='nudge', step=step, why='text')
                messages.append({'role': 'user', 'content':
                                 'Этот текст никто не получил: человеку можно сказать только через '
                                 'инструмент say. Сообщение по-прежнему без ответа.'})
                continue
            break

        for c in calls:
            fn = c['function']['name']
            try:
                args = json.loads(c['function']['arguments'] or '{}')
            except ValueError:
                args = {}
            if last and fn != 'say':
                # Модель вызывает инструменты из системного промпта, даже когда их не предложили.
                result = 'не выполнено: шаги кончаются, а человек без ответа. Сейчас работает только say.'
            else:
                result = run_tool(fn, args, name, env)
            if fn not in ('remember', 'write_state', 'say'):
                # Опора для чисел: только то, что вернули данные, а не то, что модель записала сама.
                run_tool.support += '\n' + result
            emit(event='tool', step=step, tool=fn, args=args, result=result[:300])
            if not quiet:
                short = json.dumps(args, ensure_ascii=False)
                print('[%d] %s %s' % (step, fn, short[:160]))
                print('    → %s' % result.replace('\n', '\n      ')[:400])
            messages.append({'role': 'tool', 'tool_call_id': c['id'], 'content': result[:4000]})

        # Потолок шагов близко, а человек без ответа: в прогоне 10.11 Лу потратил последние
        # шаги на remember и не успел сказать. Среда называет факт, порядок выбирает модель.
        if step == MAX_STEPS - 3 and compose_session.unanswered(home):
            emit(event='nudge', step=step, why='steps')
            messages.append({'role': 'user', 'content':
                             'Осталось три шага, сообщение человека по-прежнему без ответа.'})

        # Сон по AGENTS.md последний шаг сессии. Без этой границы модель продолжала ходить
        # по кругу «переписать состояние, назначить сон» до потолка шагов.
        if any(c['function']['name'] == 'sleep' for c in calls):
            # Уйти спать с неотвеченным сообщением можно, но осознанно: среда называет факт.
            if nudges < MAX_NUDGES and compose_session.unanswered(home):
                nudges += 1
                emit(event='nudge', step=step, why='sleep')
                messages.append({'role': 'user', 'content':
                                 'Ты назначил сон, а сообщение человека осталось без ответа. Он не '
                                 'получит ничего, пока ты не скажешь через say.'})
                continue
            emit(event='asleep', step=step)
            break

        if spent > MAX_TOKENS:
            messages.append({'role': 'user', 'content':
                             'Бюджет сессии кончился. Перепиши состояние и назначь пробуждение.'})

    if not quiet:
        print('\nшагов: %d, токенов потрачено: %d' % (step, spent))
    return spent, 0


def main():
    p = argparse.ArgumentParser()
    p.add_argument('name')
    p.add_argument('--from-operator', dest='from_operator')
    p.add_argument('--wake-reason', dest='wake_reason')
    p.add_argument('--quiet', action='store_true')
    a = p.parse_args()
    _, code = run(a.name, a.from_operator, a.wake_reason, a.quiet)
    return code


if __name__ == '__main__':
    sys.exit(main())
