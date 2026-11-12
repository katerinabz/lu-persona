#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Мост с Телеграмом: у каждого человека свой дневник и свой Лу.

    LU_TG_TOKEN=... python3 tools/tg_bridge.py

Работает рядом с диспетчером (`tools/hub.py`). Мост принимает сообщения, ведёт дневники и
доставляет ответы. Решает, что сказать, только Лу: мост кладёт вопрос в рабочее место человека
(`agent/users/<id>-<режим>/inbox.jsonl`), диспетчер запускает там сессию, ответ появляется в
исходящих того же места.

Режимы человека (хранятся в data/lu.db, `tools/tg_store.py`):

    ''      только пришёл: приветствие с выбором
    own     свой дневник. Каждое сообщение — запись, вопрос Лу — кнопкой «Спросить Лу»
    demo    дневник-пример. Каждое сообщение — вопрос Лу о нём

Переключение в любой момент: /mine и /example или кнопки внизу чата.

Интерфейс на виджетах Телеграма (Bot API 10.4):

    welcome   приветствие: выбор режима, сводка своего дневника или задание на примере
    answer    под ответом Лу: записи, на которые он опирается, текст записи по нажатию
    diary     дневник календарём, фильтр по теме, частые пары тем (/diary)
    feedback  два вопроса после третьего ответа Лу

Если виджет не отправился, то же содержание уходит обычным текстом.

Лимиты на ключе автора: LU_TG_DAILY_LIMIT вопросов Лу на человека в сутки (10) и
LU_TG_GLOBAL_LIMIT на всех (150). Записи в дневник без лимита: они не тратят модель.
Человек со своим ключом (/key) лимитов не имеет, его сессии идут через его модель.

Журналы:

    logs/tg_users.jsonl     первое появление человека: id, время, метка из ссылки ?start=<метка>
    logs/tg_feedback.jsonl  ответы на вопросы отзыва
    logs/tg_errors.jsonl    ошибки Bot API

Без сторонних библиотек: длинный опрос Bot API через urllib.
"""
import argparse
import datetime
import io
import json
import os
import re
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import hub  # noqa: E402
import tg_keys  # noqa: E402
import tg_store  # noqa: E402
import tg_widgets  # noqa: E402

DAILY_LIMIT = int(os.environ.get('LU_TG_DAILY_LIMIT', '10'))
GLOBAL_LIMIT = int(os.environ.get('LU_TG_GLOBAL_LIMIT', '150'))
FEEDBACK_AFTER = 3
TYPING_EVERY = 4        # секунд: «печатает…» в Телеграме гаснет через 5
WAIT_NOTICE = 120       # секунд без ответа Лу до сообщения, что он задерживается
WAIT_GIVE_UP = 600      # после этого «печатает…» больше не показывается
STATE = os.path.join(ROOT, 'data', 'tg.json')

# --- тексты ------------------------------------------------------------------------------

DESCRIPTION = (
    'Лу — умный дневник.\n\n'
    'Пишите сюда, как прошёл день и что беспокоит, как в заметки. Лу читает записи и замечает то, '
    'что трудно увидеть самому: о чём вы пишете чаще всего, что повторяется вместе, когда что началось.\n\n'
    'В каждом ответе Лу показывает даты записей. Советов и диагнозов не даёт.')
SHORT_DESCRIPTION = 'Умный дневник: вы пишете, Лу замечает, что повторяется. С датами, без советов.'

INTRO_NEW = (
    'Лу — умный дневник.\n\n'
    '1. Пишете сюда, как прошёл день. Каждое сообщение сохраняется записью.\n'
    '2. Спрашиваете Лу, например: «Почему я плохо сплю?»\n'
    '3. Лу отвечает по вашим записям и показывает даты. Советов не даёт.\n\n'
    'Записи хранятся у создателя бота и никому не показываются. Удалить их можно в настройках.')
INTRO_DEMO = (
    'Это пример: 34 записи выдуманной героини за июнь и июль 2026 года.\n\n'
    'Спросите Лу о ней: напишите вопрос в чат.')
OWN_HELLO = ('Дневник начат. Напишите первую запись: как прошёл день, что порадовало, что беспокоит. '
             'Просто сообщением сюда.')
OWN_BACK = 'Новая запись — просто сообщение сюда.'
DEMO_HELLO = 'Это пример: дневник выдуманной героини. Спросите Лу о ней: нажмите вопрос в карточке или напишите свой.'
FIRST_ENTRY = ('Это первая запись. Пишите так каждый день или когда хочется. '
               'Через несколько записей спросите Лу, что повторяется: кнопка «Спросить Лу» внизу.')
THIRD_ENTRY = 'Записей уже три. Можно спросить Лу, например: «О чём я пишу чаще всего?» Кнопка «Спросить Лу» внизу.'
QUESTION_OR_ENTRY = 'Это вопрос к Лу или запись в дневник?'
CHOOSE = 'Сначала выберите: свой дневник или пример.'

WELCOME_CAPTION = {'new': '', 'demo': '', 'own': ''}
DIARY_CAPTION = {'demo': 'Пример, июнь и июль 2026 года.', 'own': ''}
FEEDBACK_CAPTION = 'Ответьте, пожалуйста, на два вопроса.'
ANSWER_SHORT_CAPTION = 'Записи из ответа.'

SAVED = 'Записано · %s'
SAVED_AS_QUESTION = 'Отправлено Лу как вопрос.'
ASK_PROMPT = 'Напишите вопрос. Например: о чём я пишу чаще всего?'
ASK_PROMPT_SHORT = 'Напишите вопрос.'
ASK_CANCELLED = 'Отменено.'
NEED_ENTRIES = 'Сначала напишите хотя бы одну запись: Лу отвечает только по ним.'
DEMO_ASK = 'Пишите вопрос прямо в чат.'

ONLY_TEXT = 'Лу понимает только текст.'
LIMIT_USER = ('Вопросы на сегодня закончились, завтра будет ещё %d. Записи можно делать без ограничений.\n\n'
              'Свой API-ключ снимает лимит: /key.')
LIMIT_ALL = 'Общий лимит вопросов на сегодня закончился, он обновится в полночь. Свой API-ключ снимает лимит: /key.'
NOT_ANSWERING = 'Лу отвечает дольше обычного. Ответ придёт сюда.'
QUESTION_SENT = 'Вопрос отправлен: «%s». Ответ придёт через 10–60 секунд.'
NO_DIARY = 'Дневник сейчас не открывается.'
EMPTY_DIARY = 'Записей пока нет. Напишите первую прямо в чат.'

KEY_HELP = ('Со своим ключом лимитов нет: Лу отвечает через вашу модель, запросы оплачиваете вы. '
            'Подходят ключи NeuralDeep, OpenRouter и OpenAI.\n\n'
            'Пришлите ключ одним сообщением, бот сразу удалит его из чата.')
KEY_OK = 'Ключ %s привязан, лимитов больше нет.'
KEY_BAD = 'Ключ не подошёл: %s. Попробовать снова: /key.'
KEY_NOT_A_KEY = 'Это не похоже на ключ. Попробовать снова: /key.'
KEY_UNDELETED = 'Удалите, пожалуйста, сообщение с ключом сами.'
KEY_OFF = 'Ключ отвязан и удалён.'
KEY_NONE = 'Ключ не привязан. Привязать: /key.'

FORGET_ASK = 'Удалить все ваши записи, память Лу о вас и ключ? Восстановить их будет нельзя.'
FORGET_DONE = 'Всё удалено. Начать заново: /start.'
FORGET_KEPT = 'Ничего не удалено.'

SETTINGS = 'Настройки'
SETTINGS_OWN = 'Свой дневник · %d %s'
SETTINGS_DEMO = 'Пример дневника'
SETTINGS_KEY = 'Свой ключ: вопросы без лимита'
SETTINGS_LIMIT = 'Осталось вопросов на сегодня: %d из %d. Записи без ограничений.'
CLEAR_ASK = 'Удалить все записи (%d)? Лу забудет и разговоры о них. Восстановить будет нельзя.'
CLEARED = 'Записи удалены: %d.'
MEM_ASK = 'Лу забудет все разговоры с вами. Записи останутся.'
MEM_CLEARED = 'Лу забыл разговоры. Записи на месте.'
NO_ENTRIES = 'Записей пока нет. Напишите первую прямо в чат.'
LIST_HEAD = 'Записи %d–%d из %d. Нажмите номер, чтобы открыть запись.'
ENTRY_GONE = 'Этой записи уже нет.'
ENTRY_DELETED = 'Запись удалена.'
DELETE_ASK = 'Удалить запись от %s?\n\n%s'
TOPICS_HEAD = 'Темы записи от %s. Нажмите, чтобы добавить или убрать.'
EDIT_PROMPT = ('Пришлите новый текст записи от %s. Сейчас там:\n\n%s\n\n'
               'Поправить пару слов быстрее в самом сообщении: долгое нажатие на него → «Изменить».')
EDIT_DONE = 'Текст изменён.'
EDIT_CANCELLED = 'Отменено.'
SAVED_EDITED = 'Изменено · %s'
EXPORT_CAPTION = 'Все ваши записи.'

BTN_ASK, BTN_MY_DIARY, BTN_EXAMPLE = 'Спросить Лу', 'Мои записи', 'Пример дневника'
BTN_CALENDAR = 'Календарь'
BTN_DEMO_DIARY, BTN_OWN, BTN_SETTINGS = 'Дневник героини', 'Начать свой дневник', 'Настройки'
KEYBOARD = {'own': [[BTN_ASK, BTN_MY_DIARY], [BTN_CALENDAR, BTN_SETTINGS]],
            'demo': [[BTN_DEMO_DIARY, BTN_OWN], [BTN_SETTINGS]]}
BUTTONS = {BTN_ASK, BTN_MY_DIARY, BTN_EXAMPLE, BTN_CALENDAR, BTN_DEMO_DIARY, BTN_OWN, BTN_SETTINGS}
EMOJI = {'работа': '💼', 'тревога': '🌀', 'сон': '🌙', 'отношения': '💞', 'спорт': '🏃', 'деньги': '💰'}
MONTHS = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']
PAGE = 5

QUESTIONS = [
    ('q1', 'Лу дал вам совет или диагноз?',
     [('yes', 'Да'), ('partly', 'Почти'), ('no', 'Нет')]),
    ('q2', 'Понятно, зачем нужен Лу?',
     [('yes', 'Да'), ('no', 'Нет')]),
]

# В меню только частое. /mine, /example, /key, /key_off, /forget работают, но живут в настройках и подсказках.
COMMANDS = [{'command': 'start', 'description': 'Начало'},
            {'command': 'ask', 'description': 'Спросить Лу'},
            {'command': 'entries', 'description': 'Мои записи'},
            {'command': 'diary', 'description': 'Календарь'},
            {'command': 'settings', 'description': 'Настройки'}]

API_ERRORS = (urllib.error.URLError, RuntimeError, OSError, ValueError)


def now():
    return datetime.datetime.now().replace(microsecond=0)


def load_demo():
    with io.open(hub.DEMO, encoding='utf-8') as f:
        return sorted(json.load(f), key=lambda r: r['date'])


def day_label(r):
    y, m, d = r['date'].split('-')
    return '%d %s%s' % (int(d), MONTHS[int(m) - 1], ', ' + r['time'] if r.get('time') else '')


def short(text, n=140):
    text = ' '.join(text.split())
    return text if len(text) <= n else text[:n - 1].rstrip() + '…'


def topics_label(topics):
    return ', '.join(topics) if topics else 'без темы'


MONTHS_FULL = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября',
               'октября', 'ноября', 'декабря']


def human_dates(text):
    """2026-11-10 в ответе Лу превращается в «10 ноября»: в остальном интерфейсе даты так и пишутся."""
    return re.sub(r'\b(20\d\d)-(\d\d)-(\d\d)\b',
                  lambda m: '%d %s' % (int(m.group(3)), MONTHS_FULL[int(m.group(2)) - 1])
                  if 1 <= int(m.group(2)) <= 12 else m.group(0), text)


def plural(n, a, b, c):
    m, h = n % 10, n % 100
    return a if m == 1 and h != 11 else b if 2 <= m <= 4 and not 10 <= h < 20 else c


class Bridge(object):
    def __init__(self, token, store=None):
        self.api = 'https://api.telegram.org/bot%s/' % token
        self.store = store or tg_store.Store()
        self.logs = os.path.join(ROOT, 'logs')
        os.makedirs(self.logs, exist_ok=True)
        self.state = STATE
        self.s = self.load_state()
        self._demo = None

    # --- состояние моста ---------------------------------------------------------------

    def load_state(self):
        try:
            with io.open(self.state, encoding='utf-8') as f:
                s = json.load(f)
        except (OSError, ValueError):
            s = {}
        s.setdefault('offset', 0)
        s.setdefault('users', {})
        if not isinstance(s.get('outbox_pos'), dict):
            s['outbox_pos'] = {}
        s.setdefault('global', {'day': '', 'count': 0})
        return s

    def save_state(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        tmp = self.state + '.tmp'
        with io.open(tmp, 'w', encoding='utf-8') as f:
            f.write(json.dumps(self.s, ensure_ascii=False, indent=1))
        os.replace(tmp, self.state)

    def user(self, uid):
        u = self.s['users'].setdefault(str(uid), {'day': '', 'count': 0, 'replies': 0, 'asked': ''})
        u.setdefault('questions', [])     # номера примеров вопросов, нажатых в приветствии
        u.setdefault('answers', {})       # ответы на вопросы отзыва
        u.setdefault('waiting', 0)        # время вопроса, на который Лу ещё не ответил; 0 — ждать нечего
        u.setdefault('awaiting', '')      # 'ask' — следующее сообщение вопрос, 'key' — ключ
        today = now().date().isoformat()
        if u['day'] != today:
            u['day'], u['count'] = today, 0
        return u

    def mode(self, uid):
        return self.store.person(uid)['mode']

    def log(self, fname, **kw):
        kw['at'] = now().isoformat(timespec='seconds')
        with io.open(os.path.join(self.logs, fname), 'a', encoding='utf-8') as f:
            f.write(json.dumps(kw, ensure_ascii=False) + '\n')

    def rows(self, uid, mode):
        """Записи для виджетов: дневник-пример или свой дневник человека."""
        if mode == 'own':
            return self.store.entries(uid)
        if self._demo is None:
            try:
                self._demo = load_demo()
            except (OSError, ValueError) as e:
                self.log('tg_errors.jsonl', error='дневник-пример: %r' % e)
                self._demo = []
        return self._demo

    # --- Bot API -----------------------------------------------------------------------

    def call(self, method, http_timeout=40, **params):
        data = urllib.parse.urlencode(
            {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list, bool)) else v)
             for k, v in params.items()}).encode()
        try:
            with urllib.request.urlopen(self.api + method, data=data, timeout=http_timeout) as r:
                d = json.loads(r.read())
        except urllib.error.HTTPError as e:
            # Телеграм отвечает 400 с описанием ошибки в теле: оно нужнее, чем код.
            d = json.loads(e.read() or b'{}')
        if not d.get('ok'):
            raise RuntimeError('%s: %s' % (method, d.get('description')))
        return d['result']

    def safe(self, method, **params):
        try:
            return self.call(method, http_timeout=20, **params)
        except API_ERRORS as e:
            self.log('tg_errors.jsonl', method=method, error=str(e)[:300])
            return None

    def send(self, chat, text, keyboard=None, reply_keyboard=None):
        params = {'chat_id': chat, 'text': text}
        if keyboard:
            params['reply_markup'] = {'inline_keyboard': keyboard}
        elif reply_keyboard:
            params['reply_markup'] = {'keyboard': reply_keyboard, 'resize_keyboard': True,
                                      'is_persistent': True}
        return self.safe('sendMessage', **params)

    def upload(self, method, file_field, filename, content, **params):
        """Отправить файл (multipart). None при ошибке."""
        boundary = 'lu%d' % int(time.time() * 1000)
        body = b''
        for k, v in params.items():
            body += ('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n%s\r\n' % (boundary, k, v)).encode()
        body += ('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\n'
                 'Content-Type: text/plain; charset=utf-8\r\n\r\n' % (boundary, file_field, filename)).encode()
        body += content + ('\r\n--%s--\r\n' % boundary).encode()
        r = urllib.request.Request(self.api + method, data=body,
                                   headers={'Content-Type': 'multipart/form-data; boundary=' + boundary})
        try:
            with urllib.request.urlopen(r, timeout=30) as resp:
                return json.loads(resp.read()).get('result')
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.log('tg_errors.jsonl', method=method, error=str(e)[:300])
            return None

    def show(self, uid, screen, msg_id=None):
        """Экран из текста и кнопок: правкой сообщения на месте, если оно есть, иначе новым."""
        text, keyboard = screen
        if msg_id:
            params = {'chat_id': uid, 'message_id': msg_id, 'text': text}
            params['reply_markup'] = {'inline_keyboard': keyboard or []}
            if self.safe('editMessageText', **params):
                return
        self.send(uid, text, keyboard)

    def widget(self, chat, name, data, caption, above=False):
        """Отправить виджет. None, если не получилось: тогда вызывающий шлёт текст."""
        try:
            html = tg_widgets.code(name)
        except (tg_widgets.TooLarge, OSError) as e:
            self.log('tg_errors.jsonl', method='sendWidget', error=str(e))
            return None
        params = {'chat_id': chat, 'html': html, 'chat_data': data, 'allow_queries': True}
        if caption:
            params['caption'] = caption
        if above:
            params['show_caption_above_media'] = True
        return self.safe('sendWidget', **params)

    def setup_profile(self):
        """Команды и описание бота. Повторный вызов ничего не ломает."""
        self.safe('setMyCommands', commands=COMMANDS)
        self.safe('setMyDescription', description=DESCRIPTION)
        self.safe('setMyShortDescription', short_description=SHORT_DESCRIPTION)

    # --- экраны ------------------------------------------------------------------------

    def welcome_data(self, uid, kind):
        rows = [] if kind == 'new' else self.rows(uid, kind)
        return tg_widgets.welcome_data(rows, self.user(uid)['questions'], kind)

    def send_welcome(self, uid, kind=None):
        kind = kind or self.mode(uid) or 'new'
        msg = None
        try:
            data = self.welcome_data(uid, kind)
        except tg_widgets.TooLarge as e:
            self.log('tg_errors.jsonl', method='welcome_data', error=str(e))
        else:
            msg = self.widget(uid, 'welcome', data, WELCOME_CAPTION[kind])
        if not msg:
            if kind == 'new':
                self.send(uid, INTRO_NEW, self.choice_keyboard())
            elif kind == 'demo':
                self.send(uid, INTRO_DEMO)
            else:
                self.send(uid, OWN_BACK)

    def choice_keyboard(self):
        return [[{'text': BTN_OWN, 'callback_data': 'mode:own'},
                 {'text': 'Сначала посмотреть пример', 'callback_data': 'mode:demo'}]]

    def switch(self, uid, mode):
        self.store.set_mode(uid, mode)
        u = self.user(uid)
        u['awaiting'] = ''
        if mode == 'own':
            if self.store.entries(uid):
                self.send(uid, OWN_BACK, reply_keyboard=KEYBOARD['own'])
                self.send_welcome(uid, 'own')
            else:
                self.send(uid, OWN_HELLO, reply_keyboard=KEYBOARD['own'])
        else:
            self.send(uid, DEMO_HELLO, reply_keyboard=KEYBOARD['demo'])
            self.send_welcome(uid, 'demo')

    def send_diary(self, uid):
        mode = self.mode(uid)
        if not mode:
            return self.send_welcome(uid, 'new')
        rows = self.rows(uid, mode)
        if not rows:
            return self.send(uid, EMPTY_DIARY if mode == 'own' else NO_DIARY)
        try:
            data = tg_widgets.diary_data(rows, mode)
        except tg_widgets.TooLarge as e:
            self.log('tg_errors.jsonl', method='diary_data', error=str(e))
            data = None
        if not data or not self.widget(uid, 'diary', data, DIARY_CAPTION[mode]):
            self.send(uid, NO_DIARY)

    # --- настройки и записи ------------------------------------------------------------

    def settings_screen(self, uid):
        p = self.store.person(uid)
        lines = [SETTINGS, '']
        if p['mode'] == 'own':
            n = self.store.count(uid)
            lines.append(SETTINGS_OWN % (n, plural(n, 'запись', 'записи', 'записей')))
        elif p['mode'] == 'demo':
            lines.append(SETTINGS_DEMO)
        u = self.user(uid)
        lines.append(SETTINGS_KEY if p.get('key') else SETTINGS_LIMIT % (max(0, DAILY_LIMIT - u['count']), DAILY_LIMIT))
        kb = []
        if p['mode'] == 'own':
            kb.append([{'text': 'Скачать записи', 'callback_data': 'set:export'}])
            kb.append([{'text': 'Пример дневника', 'callback_data': 'mode:demo'}])
        else:
            kb.append([{'text': 'Вести свой дневник', 'callback_data': 'mode:own'}])
        kb.append([{'text': 'Отвязать ключ', 'callback_data': 'set:keyoff'} if p.get('key')
                   else {'text': 'Свой API-ключ: без лимита вопросов', 'callback_data': 'set:key'}])
        if self.store.count(uid):
            kb.append([{'text': 'Удалить все записи', 'callback_data': 'set:clear'}])
        kb.append([{'text': 'Удалить все мои данные из бота', 'callback_data': 'set:all'}])
        return '\n'.join(lines), kb

    def confirm(self, text, yes, yes_data, no_data='set'):
        return text, [[{'text': yes, 'callback_data': yes_data}, {'text': 'Отмена', 'callback_data': no_data}]]

    def done(self, text, back='set', label='← Настройки'):
        return text, [[{'text': label, 'callback_data': back}]]

    def list_screen(self, uid, offset=0, note=None):
        total = self.store.count(uid)
        if not total:
            return (note + '\n\n' if note else '') + NO_ENTRIES, []
        offset = max(0, min(int(offset), (total - 1) // PAGE * PAGE))
        rows = self.store.listing(uid, offset, PAGE)
        parts = [LIST_HEAD % (offset + 1, offset + len(rows), total)]
        if note:
            parts.insert(0, note)
        for i, r in enumerate(rows, 1):
            parts.append('%d. %s · %s\n%s' % (i, day_label(r), topics_label(r['topics']), short(r['text'])))
        kb = [[{'text': str(i), 'callback_data': 'en:%d' % r['id']} for i, r in enumerate(rows, 1)]]
        nav = []
        if offset > 0:
            nav.append({'text': '← Новее', 'callback_data': 'ls:%d' % (offset - PAGE)})
        if offset + PAGE < total:
            nav.append({'text': 'Старее →', 'callback_data': 'ls:%d' % (offset + PAGE)})
        if nav:
            kb.append(nav)
        kb.append([{'text': 'Календарь', 'callback_data': 'cal'}])
        return '\n\n'.join(parts), kb

    def entry_keyboard(self, eid):
        return [[{'text': 'Изменить текст', 'callback_data': 'ed:%d' % eid},
                 {'text': 'Темы', 'callback_data': 'tp:%d' % eid}],
                [{'text': 'Удалить', 'callback_data': 'dl:%d' % eid},
                 {'text': 'Это вопрос к Лу', 'callback_data': 'ask:%d' % eid}],
                [{'text': '← К списку', 'callback_data': 'ls:0'}]]

    def entry_screen(self, uid, eid, note=None):
        e = self.store.entry(uid, eid)
        if e is None:
            return self.done(ENTRY_GONE, 'ls:0', '← К списку')
        text = '%s · %s\n\n%s' % (day_label(e), topics_label(e['topics']), e['text'][:3500])
        return (note + '\n\n' if note else '') + text, self.entry_keyboard(e['id'])

    def topics_screen(self, uid, eid):
        e = self.store.entry(uid, eid)
        if e is None:
            return self.done(ENTRY_GONE, 'ls:0', '← К списку')
        names = list(tg_store.TOPICS)
        buttons = [{'text': ('✓ ' if t in e['topics'] else '') + EMOJI.get(t, '') + ' ' + t,
                    'callback_data': 'tt:%d:%d' % (e['id'], i)} for i, t in enumerate(names)]
        kb = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        kb.append([{'text': 'Готово', 'callback_data': 'en:%d' % e['id']}])
        return TOPICS_HEAD % day_label(e) + '\n\n' + short(e['text'], 300), kb

    def reset_memory(self, uid, modes=('own', 'demo')):
        """Стереть рабочие места Лу у человека: заметки, состояние, переписку."""
        for mode in modes:
            name = hub.name_of(uid, mode)
            shutil.rmtree(hub.home_of(name), ignore_errors=True)
            self.s['outbox_pos'].pop(name, None)
        self.user(uid)['waiting'] = 0

    def export(self, uid):
        if not self.store.count(uid):
            return self.send(uid, NO_ENTRIES)
        if not self.upload('sendDocument', 'document', 'lu-diary.txt',
                           self.store.export_text(uid).encode('utf-8'), chat_id=uid, caption=EXPORT_CAPTION):
            self.send(uid, 'Не получилось отправить файл. Попробуйте позже.')

    def start_ask(self, uid):
        mode = self.mode(uid)
        if mode == 'demo':
            return self.send(uid, DEMO_ASK)
        if mode != 'own':
            return self.send_welcome(uid, 'new')
        if not self.store.entries(uid):
            return self.send(uid, NEED_ENTRIES)
        self.user(uid)['awaiting'] = 'ask'
        self.send(uid, ASK_PROMPT_SHORT if self.user(uid)['replies'] else ASK_PROMPT, [[{'text': 'Отмена', 'callback_data': 'ask:cancel'}]])

    # --- вопросы Лу --------------------------------------------------------------------

    def take(self, uid):
        """Учесть вопрос в лимитах. Возвращает текст отказа или None."""
        u = self.user(uid)
        if self.store.person(uid).get('key'):
            u['count'] += 1
            return None
        g = self.s['global']
        today = now().date().isoformat()
        if g['day'] != today:
            g['day'], g['count'] = today, 0
        if u['count'] >= DAILY_LIMIT:
            return LIMIT_USER % DAILY_LIMIT
        if g['count'] >= GLOBAL_LIMIT:
            return LIMIT_ALL
        u['count'] += 1
        g['count'] += 1
        return None

    def to_inbox(self, uid, mode, text):
        name = hub.ensure_workspace(uid, mode)
        line = {'from': 'tg:%s' % uid, 'at': now().isoformat(timespec='seconds'), 'text': text[:2000]}
        with io.open(os.path.join(hub.home_of(name), 'inbox.jsonl'), 'a', encoding='utf-8', newline='\n') as f:
            f.write(json.dumps(line, ensure_ascii=False) + '\n')
        u = self.user(uid)
        u['waiting'] = u['waiting'] or time.time()
        u['notified'] = False
        u['typing'] = 0

    def question(self, uid, mode, text, echo=False):
        """Отправить вопрос Лу. False — упёрлись в лимит, человеку уже сказано."""
        refusal = self.take(uid)
        if refusal:
            self.send(uid, refusal)
            return False
        self.to_inbox(uid, mode, text)
        if echo:
            # Нажатие в виджете не оставляет сообщения в чате: без этой строки ответ Лу
            # приходит ни на что (юзабилити-тест 12.11, две персоны из трёх).
            self.send(uid, QUESTION_SENT % text)
        self.typing(uid, force=True)
        return True

    # --- ключ --------------------------------------------------------------------------

    def take_key(self, uid, m, text):
        self.user(uid)['awaiting'] = ''
        if not self.safe('deleteMessage', chat_id=uid, message_id=m.get('message_id')):
            self.send(uid, KEY_UNDELETED)
        if not tg_keys.looks_like_key(text):
            return self.send(uid, KEY_NOT_A_KEY)
        name, base, model, why = tg_keys.check(text.strip())
        if why:
            self.log('tg_errors.jsonl', method='key_check', user='tg:%s' % uid, error=why)
            return self.send(uid, KEY_BAD % why)
        self.store.set_key(uid, text.strip(), base, model)
        self.log('tg_users.jsonl', user='tg:%s' % uid, key=name)
        self.send(uid, KEY_OK % name)

    # --- входящие ----------------------------------------------------------------------

    def on_message(self, m):
        uid = m['chat']['id']
        if m['chat'].get('type') != 'private':
            return
        text = (m.get('text') or '').strip()
        u = self.user(uid)
        cmd = text.split()[0].split('@')[0] if text.startswith('/') else ''
        rest = text[len(text.split()[0]):].strip() if cmd else ''
        if cmd or text in BUTTONS:
            u['awaiting'] = ''  # команда или кнопка отменяет начатое: вопрос, ключ, правку

        if cmd == '/start':
            if not u.get('seen'):
                u['seen'] = now().isoformat(timespec='seconds')
                self.log('tg_users.jsonl', user='tg:%s' % uid, source=rest or 'без метки')
            u['awaiting'] = ''
            mode = self.mode(uid)
            if mode == 'own' and self.store.entries(uid):
                self.send(uid, OWN_BACK, reply_keyboard=KEYBOARD['own'])
                self.send_welcome(uid, 'own')
            elif mode:
                self.send(uid, OWN_HELLO if mode == 'own' else DEMO_HELLO, reply_keyboard=KEYBOARD[mode])
                if mode == 'demo':
                    self.send_welcome(uid, mode)
            else:
                self.send_welcome(uid, 'new')
            return
        if cmd == '/mine' or text == BTN_OWN:
            return self.switch(uid, 'own')
        if cmd == '/example' or text == BTN_EXAMPLE:
            return self.switch(uid, 'demo')
        if cmd == '/diary' or text in (BTN_DEMO_DIARY, BTN_CALENDAR):
            return self.send_diary(uid)
        if cmd == '/entries' or text == BTN_MY_DIARY:
            return self.show(uid, self.list_screen(uid))
        if cmd == '/settings' or text == BTN_SETTINGS:
            return self.show(uid, self.settings_screen(uid))
        if cmd == '/ask' or text == BTN_ASK:
            if rest and self.mode(uid) in ('own', 'demo'):
                u['awaiting'] = ''
                return self.question(uid, self.mode(uid), rest)
            return self.start_ask(uid)
        if cmd == '/key':
            u['awaiting'] = 'key'
            return self.send(uid, KEY_HELP)
        if cmd == '/key_off':
            if self.store.person(uid).get('key'):
                self.store.clear_key(uid)
                return self.send(uid, KEY_OFF)
            return self.send(uid, KEY_NONE)
        if cmd == '/forget':
            return self.show(uid, self.confirm(FORGET_ASK, 'Удалить всё', 'forget:yes', 'forget:no'))
        if not text:
            return self.send(uid, ONLY_TEXT)
        if u['awaiting'] == 'key' or (text.startswith('sk-') and tg_keys.looks_like_key(text)):
            return self.take_key(uid, m, text)
        if cmd:
            return self.send_welcome(uid)
        if u['awaiting'].startswith('edit:'):
            eid = int(u['awaiting'][5:])
            u['awaiting'] = ''
            if self.store.update_entry(uid, eid, text[:4000]) is None:
                return self.send(uid, ENTRY_GONE)
            return self.show(uid, self.entry_screen(uid, eid, EDIT_DONE))

        mode = self.mode(uid)
        if not mode:
            return self.send(uid, CHOOSE, self.choice_keyboard())
        if mode == 'demo':
            return self.question(uid, 'demo', text)
        if u['awaiting'] == 'ask' or re.match(r'лу\s*[,:!]', text, re.I):
            u['awaiting'] = ''
            return self.question(uid, 'own', text)
        entry_id, topics = self.store.add_entry(uid, text[:4000])
        if text.rstrip().endswith('?'):
            r = self.send(uid, QUESTION_OR_ENTRY, self.question_keyboard(entry_id))
        else:
            r = self.send(uid, self.saved_text(uid, topics), self.saved_keyboard(entry_id))
        self.store.link_message(uid, entry_id, m.get('message_id'), (r or {}).get('message_id'))

    def saved_text(self, uid, topics):
        text = SAVED % self.what(topics)
        n = self.store.count(uid)
        if n == 1:
            text += '\n\n' + FIRST_ENTRY
        elif n == 3:
            text += '\n\n' + THIRD_ENTRY
        return text

    def question_keyboard(self, eid):
        return [[{'text': 'Спросить Лу', 'callback_data': 'ask:%d' % eid},
                 {'text': 'Это запись', 'callback_data': 'keep:%d' % eid}]]

    def what(self, topics):
        return 'темы: ' + ', '.join(topics) if topics else 'без темы'

    def saved_keyboard(self, eid):
        return [[{'text': 'Изменить', 'callback_data': 'ed:%d' % eid},
                 {'text': 'Удалить', 'callback_data': 'dl:%d' % eid}]]

    def on_edited_message(self, m):
        """Человек поправил сообщение с записью прямо в чате: запись правится вместе с ним."""
        uid = m['chat']['id']
        text = (m.get('text') or '').strip()
        link = self.store.by_message(uid, m.get('message_id'))
        if m['chat'].get('type') != 'private' or not text or not link:
            return
        eid, reply_id = link
        topics = self.store.update_entry(uid, eid, text[:4000])
        if topics is not None and reply_id:
            self.safe('editMessageText', chat_id=uid, message_id=reply_id, text=SAVED_EDITED % self.what(topics),
                      reply_markup={'inline_keyboard': self.saved_keyboard(eid)})

    def on_widget_query(self, q):
        """Нажатие внутри виджета. Ответ нужен за 15 секунд, поэтому медленное идёт после ответа."""
        uid = q['from']['id']
        data = q.get('data') or ''
        u = self.user(uid)
        msg_id = q.get('message_id')
        answer, after = '{"ok":1}', None

        if data in ('mode:own', 'mode:demo'):
            after = lambda: self.switch(uid, data[5:])  # noqa: E731
        elif data == 'ask':
            after = lambda: self.start_ask(uid)  # noqa: E731
        elif data == 'diary':
            after = lambda: self.send_diary(uid)  # noqa: E731
        elif data == 'settings':
            after = lambda: self.show(uid, self.settings_screen(uid))  # noqa: E731
        elif data.startswith('q:') and data[2:].isdigit() and int(data[2:]) < len(tg_widgets.QUESTIONS):
            i = int(data[2:])
            # Примеры вопросов есть только в приветствии дневника-примера: вопрос туда же.
            refusal = self.take(uid)
            if refusal:
                answer = '{"ok":0,"why":"limit"}'
                after = lambda: self.send(uid, refusal)  # noqa: E731
            else:
                if i not in u['questions']:
                    u['questions'].append(i)
                self.to_inbox(uid, 'demo', tg_widgets.QUESTIONS[i])

                def after():
                    self.send(uid, QUESTION_SENT % tg_widgets.QUESTIONS[i])
                    self.typing(uid, force=True)
                    if msg_id:
                        self.safe('editMessageWidgetData', chat_id=uid, message_id=msg_id,
                                  chat_data=self.welcome_data(uid, 'demo'))
        elif data.startswith('e:'):
            parts = data.split(':')
            mode, i = (parts[1], parts[2]) if len(parts) == 3 else ('demo', parts[-1])
            rows = self.rows(uid, mode) if mode in ('demo', 'own') and i.isdigit() else []
            answer = tg_widgets.entry_text(rows, int(i)) if rows else 'Запись недоступна.'
        elif data.startswith('f:') and data.count(':') == 2:
            _, qid, value = data.split(':')
            valid = {x[0]: [v for v, _ in x[2]] for x in QUESTIONS}
            if value in valid.get(qid, []):
                u['answers'][qid] = value
                self.log('tg_feedback.jsonl', user='tg:%s' % uid, question=qid, answer=value)
                if msg_id:
                    after = lambda: self.safe(  # noqa: E731
                        'editMessageWidgetData', chat_id=uid, message_id=msg_id,
                        chat_data=tg_widgets.feedback_data(u['answers']))
            else:
                answer = '{"ok":0}'
        else:
            answer = '{"ok":0}'

        self.safe('answerWidgetQuery', widget_query_id=q['id'], data=answer)
        if after:
            after()

    def on_callback(self, c):
        """Кнопки под сообщениями: выбор режима, запись-вопрос, удаление, отзыв без виджета."""
        uid = c['from']['id']
        data = c.get('data') or ''
        msg = c.get('message') or {}
        u = self.user(uid)
        note = None

        if data in ('mode:own', 'mode:demo'):
            self.safe('answerCallbackQuery', callback_query_id=c['id'])
            return self.switch(uid, data[5:])
        mid = msg.get('message_id')
        screen = self.screen_for(uid, data, mid)
        if screen is not None:
            self.safe('answerCallbackQuery', callback_query_id=c['id'])
            if screen:
                self.show(uid, screen, mid)
            return
        if data == 'ask:cancel':
            u['awaiting'] = ''
            note = ASK_CANCELLED
        elif data.startswith('ask:') and data[4:].isdigit():
            text = self.store.take_back(uid, int(data[4:]))
            self.safe('answerCallbackQuery', callback_query_id=c['id'])
            if text is None:
                return
            if self.question(uid, 'own', text) and msg.get('message_id'):
                self.safe('editMessageText', chat_id=uid, message_id=msg['message_id'], text=SAVED_AS_QUESTION)
            else:
                self.store.add_entry(uid, text)  # лимит: запись возвращается на место
            return
        elif data.startswith('keep:') and data[5:].isdigit():
            e = self.store.entry(uid, int(data[5:]))
            self.safe('answerCallbackQuery', callback_query_id=c['id'])
            if e and msg.get('message_id'):
                self.safe('editMessageText', chat_id=uid, message_id=msg['message_id'],
                          text=self.saved_text(uid, e['topics']),
                          reply_markup={'inline_keyboard': self.saved_keyboard(e['id'])})
            return
        elif data == 'forget:yes':
            n = self.forget(uid)
            note = FORGET_DONE
        elif data == 'forget:no':
            note = FORGET_KEPT
        else:
            qid, _, answer = data.partition(':')
            self.log('tg_feedback.jsonl', user='tg:%s' % uid, question=qid, answer=answer)
            self.safe('answerCallbackQuery', callback_query_id=c['id'], text='Спасибо')
            ids = [q[0] for q in QUESTIONS]
            if qid in ids and ids.index(qid) + 1 < len(ids):
                self.ask(uid, ids.index(qid) + 1)
            return
        self.safe('answerCallbackQuery', callback_query_id=c['id'])
        if msg.get('message_id'):
            self.safe('editMessageText', chat_id=uid, message_id=msg['message_id'], text=note)
        else:
            self.send(uid, note)

    def screen_for(self, uid, data, mid):
        """Кнопки настроек и записей. Экран для показа на месте, False — уже сделано, None — не наше."""
        head, _, arg = data.partition(':')
        num = arg.rstrip('!').split(':')[0]
        if num and not num.isdigit() and arg != 'cancel' and head in ('ls', 'en', 'ed', 'tp', 'tt', 'dl', 'dl!'):
            return False
        if data == 'set':
            return self.settings_screen(uid)
        if data == 'set:key':
            self.user(uid)['awaiting'] = 'key'
            self.send(uid, KEY_HELP)
            return False
        if data == 'set:keyoff':
            self.store.clear_key(uid)
            return self.done(KEY_OFF)
        if data == 'set:export':
            self.export(uid)
            return False
        if data == 'set:mem':
            return self.confirm(MEM_ASK, 'Да, забыть', 'set:mem!')
        if data == 'set:mem!':
            self.reset_memory(uid)
            return self.done(MEM_CLEARED)
        if data == 'set:clear':
            return self.confirm(CLEAR_ASK % self.store.count(uid), 'Да, удалить записи', 'set:clear!')
        if data == 'set:clear!':
            n = self.store.clear_entries(uid)
            self.reset_memory(uid, ('own',))
            return self.done(CLEARED % n)
        if data == 'set:all':
            return self.confirm(FORGET_ASK, 'Удалить всё', 'forget:yes')
        if data == 'cal':
            self.send_diary(uid)
            return False
        if head == 'ls':
            return self.list_screen(uid, int(arg or 0))
        if head == 'en':
            return self.entry_screen(uid, int(arg))
        if head == 'ed' and arg == 'cancel':
            self.user(uid)['awaiting'] = ''
            return self.done(EDIT_CANCELLED, 'ls:0', '← К списку')
        if head == 'ed':
            e = self.store.entry(uid, int(arg))
            if e is None:
                return self.done(ENTRY_GONE, 'ls:0', '← К списку')
            self.user(uid)['awaiting'] = 'edit:%d' % e['id']
            self.send(uid, EDIT_PROMPT % (day_label(e), e['text']), [[{'text': 'Отмена', 'callback_data': 'ed:cancel'}]])
            return False
        if head == 'tp':
            return self.topics_screen(uid, int(arg))
        if head == 'tt':
            eid, _, i = arg.partition(':')
            names = list(tg_store.TOPICS)
            if i.isdigit() and int(i) < len(names):
                self.store.toggle_topic(uid, int(eid), names[int(i)])
            return self.topics_screen(uid, int(eid))
        if head == 'dl':
            e = self.store.entry(uid, int(arg))
            if e is None:
                return self.done(ENTRY_GONE, 'ls:0', '← К списку')
            return self.confirm(DELETE_ASK % (day_label(e), short(e['text'], 300)), 'Да, удалить',
                                'dl!:%d' % e['id'], 'en:%d' % e['id'])
        if head == 'dl!':
            self.store.delete_entry(uid, int(arg))
            return self.list_screen(uid, 0, ENTRY_DELETED)
        return None

    def forget(self, uid):
        n = self.store.forget(uid)
        self.reset_memory(uid)
        self.s['users'].pop(str(uid), None)
        self.log('tg_users.jsonl', user='tg:%s' % uid, forgot=n)
        return n

    def ask(self, uid, i):
        qid, text, options = QUESTIONS[i]
        self.send(uid, text, [[{'text': t, 'callback_data': '%s:%s' % (qid, v)} for v, t in options]])

    # --- ожидание ответа ---------------------------------------------------------------

    def typing(self, uid, force=False):
        u = self.user(uid)
        if not u['waiting']:
            return
        waited = time.time() - u['waiting']
        if waited > WAIT_NOTICE and not u.get('notified'):
            u['notified'] = True
            self.send(uid, NOT_ANSWERING)
        if waited > WAIT_GIVE_UP:
            return
        if force or time.time() - u.get('typing', 0) >= TYPING_EVERY:
            u['typing'] = time.time()
            self.safe('sendChatAction', chat_id=uid, action='typing')

    def waiting_users(self):
        return [uid for uid, u in self.s['users'].items()
                if u.get('waiting') and time.time() - u['waiting'] <= WAIT_GIVE_UP + 5]

    # --- исходящие ---------------------------------------------------------------------

    def reply(self, uid, mode, text):
        """Ответ Лу: текст в подписи над виджетом с записями, на которые он опирается."""
        rows = self.rows(uid, mode)
        try:
            data = tg_widgets.answer_data(rows, text, mode) if rows else None
            text = human_dates(text)
        except tg_widgets.TooLarge as e:
            self.log('tg_errors.jsonl', method='answer_data', error=str(e))
            data = None
        if data and len(text) <= 1024 and self.widget(uid, 'answer', data, text, above=True):
            return
        self.send(uid, text)
        if data and len(text) > 1024:
            self.widget(uid, 'answer', data, ANSWER_SHORT_CAPTION)

    def workspaces(self):
        if not os.path.isdir(hub.USERS):
            return []
        return ['users/' + d for d in sorted(os.listdir(hub.USERS)) if hub.parse('users/' + d)[0]]

    def deliver(self):
        for name in self.workspaces():
            self.deliver_from(name)

    def deliver_from(self, name):
        _, mode = hub.parse(name)
        path = os.path.join(hub.home_of(name), 'outbox.jsonl')
        size = hub.size(path)
        pos = self.s['outbox_pos'].get(name, 0)
        if size < pos:
            pos = 0  # файл почистили руками
        if size == pos:
            return
        with io.open(path, 'rb') as f:
            f.seek(pos)
            chunk = f.read()
        # Недописанную последнюю строку оставляем до следующего раза.
        end = chunk.rfind(b'\n') + 1
        self.s['outbox_pos'][name] = pos + end
        for raw in chunk[:end].decode('utf-8', 'replace').splitlines():
            try:
                o = json.loads(raw)
            except ValueError:
                continue
            to = str(o.get('to') or '')
            if o.get('kind') == 'ticket' or not to.startswith('tg:'):
                continue
            uid = to[3:]
            u = self.user(uid)
            u['waiting'] = 0
            if o.get('kind') == 'system':
                self.send(uid, o.get('text', ''))
                continue
            self.reply(uid, mode, o.get('text', ''))
            u['replies'] += 1
            if u['replies'] == FEEDBACK_AFTER and not u['asked']:
                u['asked'] = now().isoformat(timespec='seconds')
                if not self.widget(uid, 'feedback', tg_widgets.feedback_data(u['answers']), FEEDBACK_CAPTION):
                    self.ask(uid, 0)

    # --- цикл --------------------------------------------------------------------------

    def run(self):
        me = self.call('getMe', http_timeout=20)
        self.setup_profile()
        # Ответы, написанные, пока мост не работал, доставляются; старые до первого запуска — нет.
        for name in self.workspaces():
            self.s['outbox_pos'].setdefault(name, 0)
        print('мост запущен: @%s → agent/users' % me['username'], flush=True)
        while True:
            # Пока кто-то ждёт Лу, опрос короткий: «печатает…» обновляется каждые 4 секунды.
            poll = 3 if self.waiting_users() else 25
            try:
                updates = self.call('getUpdates', http_timeout=poll + 10, offset=self.s['offset'], timeout=poll,
                                    allowed_updates=['message', 'edited_message', 'callback_query', 'widget_query'])
            except API_ERRORS as e:
                self.log('tg_errors.jsonl', method='getUpdates', error=str(e)[:300])
                time.sleep(5)
                updates = []
            for up in updates:
                self.s['offset'] = up['update_id'] + 1
                try:
                    if 'message' in up:
                        self.on_message(up['message'])
                    elif 'edited_message' in up:
                        self.on_edited_message(up['edited_message'])
                    elif 'widget_query' in up:
                        self.on_widget_query(up['widget_query'])
                    elif 'callback_query' in up:
                        self.on_callback(up['callback_query'])
                except Exception as e:  # одно сломанное обновление не роняет мост
                    self.log('tg_errors.jsonl', update=up.get('update_id'), error=repr(e)[:300])
            self.deliver()
            for uid in self.waiting_users():
                self.typing(uid)
            self.save_state()
            if not updates and not self.waiting_users():
                time.sleep(1)


def main():
    p = argparse.ArgumentParser()
    p.add_argument('name', nargs='?', help='не используется, оставлен для старых команд запуска')
    p.parse_args()
    token = os.environ.get('LU_TG_TOKEN')
    if not token:
        print('нет токена: задайте LU_TG_TOKEN', file=sys.stderr)
        return 2
    try:
        Bridge(token).run()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    sys.exit(main())
