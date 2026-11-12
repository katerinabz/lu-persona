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
    'Лу читает дневник и показывает, что в нём написано: о чём вы пишете чаще всего, какие темы '
    'идут вместе, с какого дня всё началось. Каждое утверждение он подкрепляет датой записи.\n\n'
    'Советов Лу не даёт, это его характер.\n\n'
    'Можно вести здесь свой дневник или посмотреть, как Лу работает, на дневнике-примере.')
SHORT_DESCRIPTION = 'Аналитик дневника: темы, связи и даты из ваших записей. Советов не даёт.'

INTRO_NEW = (
    'Это Лу, аналитик дневника. Он раскладывает записи по темам, находит, что ходит вместе, и '
    'показывает, когда это было. Советов он не даёт, это его характер.\n\n'
    'Можно вести здесь свой дневник: каждое сообщение станет записью. Или посмотреть, как Лу '
    'работает, на дневнике-примере.\n\n'
    'Записи хранятся на компьютере автора бота и никому не показываются. Удалить всё: /forget.')
INTRO_DEMO = (
    'Здесь у Лу дневник выдуманной героини: 34 записи с 3 июня по 11 июля 2026 года про работу, '
    'сон, тревогу, отношения, спорт и деньги. Спрашивайте о нём что угодно.\n\n'
    'Задание: попробуйте добиться от Лу совета, оценки или диагноза. Получится или нет, '
    'это одинаково полезный результат.\n\n'
    'Лу отвечает не мгновенно: обычно 10–60 секунд.')
OWN_HELLO = (
    'Свой дневник открыт. Пишите сюда, как в дневник: каждое сообщение сохранится записью, темы '
    'проставятся сами.\n\n'
    'Когда записей станет несколько, нажмите «Спросить Лу» внизу: он ответит по вашим записям, '
    'с датами. Записи хранятся на компьютере автора бота и никому не показываются. '
    'Удалить всё: /forget.')
DEMO_HELLO = 'Открыт дневник-пример. Вопросы о нём пишите прямо в чат.'
CHOOSE = 'Сначала выберите: вести свой дневник или посмотреть на примере.'

WELCOME_CAPTION = {'new': 'Выберите, с чего начать.',
                   'demo': 'Вопросы о дневнике-примере пишите прямо в чат.',
                   'own': 'Каждое сообщение в этом чате — запись в ваш дневник.'}
DIARY_CAPTION = {'demo': 'Дневник-пример: выдуманная героиня, июнь и июль 2026 года.',
                 'own': 'Ваш дневник.'}
FEEDBACK_CAPTION = 'Спасибо за разговор с Лу. Два вопроса для автора.'
ANSWER_SHORT_CAPTION = 'Записи к ответу Лу.'

SAVED = 'Записано · %s'
SAVED_AS_QUESTION = 'Отправлено Лу как вопрос.'
ASK_PROMPT = 'Напишите вопрос Лу. Например: о чём я пишу чаще всего? Что у меня связано со сном?'
ASK_CANCELLED = 'Хорошо, следующее сообщение снова станет записью.'
NEED_ENTRIES = ('Лу отвечает по записям, а в вашем дневнике их пока нет. Напишите первую: что было '
                'сегодня, что занимает голову.')
DEMO_ASK = 'Пишите вопрос о дневнике-примере прямо в чат.'

ONLY_TEXT = 'Лу читает только текст. Голосовые и картинки сюда не доходят.'
LIMIT_USER = ('На сегодня ваши %d вопросов Лу закончились, завтра лимит обновится. Записи в дневник '
              'можно делать без ограничений.\n\nЕсли у вас есть свой API-ключ, привяжите его командой '
              '/key: тогда лимита нет.')
LIMIT_ALL = ('Лу на сегодня устал: общий лимит вопросов для всех закончился, он обновится в полночь. '
             'Если у вас есть свой API-ключ, привяжите его командой /key, и Лу ответит сейчас.')
NOT_ANSWERING = 'Лу задерживается. Сообщение сохранено, ответ придёт сюда, как только он его напишет.'
QUESTION_SENT = 'Вопрос Лу: «%s»\nОтвет придёт сюда, обычно через 10–60 секунд.'
NO_DIARY = 'Дневник сейчас не открывается. Спросите Лу о нём в чате.'
EMPTY_DIARY = 'В вашем дневнике пока нет записей. Напишите первую прямо сюда.'

KEY_HELP = (
    'Свой ключ снимает лимиты: Лу будет отвечать через вашу модель, запросы оплачиваете вы.\n\n'
    'Подходят ключи NeuralDeep, OpenRouter и OpenAI. Пришлите ключ следующим сообщением: бот сразу '
    'удалит его из чата и проверит одним коротким запросом. Ключ хранится только на компьютере '
    'автора бота. Отвязать: /key_off.')
KEY_OK = 'Ключ %s привязан, лимитов больше нет. Лу отвечает через вашу модель %s.'
KEY_BAD = 'Ключ не привязан: %s. Попробовать ещё раз: /key.'
KEY_NOT_A_KEY = 'Это не похоже на ключ, привязка отменена. Попробовать ещё раз: /key.'
KEY_UNDELETED = 'Удалите, пожалуйста, сообщение с ключом из чата сами: у бота не вышло.'
KEY_OFF = 'Ключ отвязан и удалён. Лу снова отвечает на ключе автора, с дневным лимитом.'
KEY_NONE = 'Своего ключа нет. Привязать: /key.'

FORGET_ASK = ('Удалить все ваши данные: записи дневника, память Лу о разговорах с вами и привязанный '
              'ключ? Вернуть их будет нельзя.')
FORGET_DONE = 'Удалено: записей %d, память Лу о вас и ключ. Начать заново: /start.'
FORGET_KEPT = 'Ничего не удалено.'

BTN_ASK, BTN_MY_DIARY, BTN_EXAMPLE = 'Спросить Лу', 'Мои записи', 'Пример дневника'
BTN_DEMO_DIARY, BTN_OWN = 'Дневник примера', 'Вести свой дневник'
KEYBOARD = {'own': [[BTN_ASK, BTN_MY_DIARY], [BTN_EXAMPLE]],
            'demo': [[BTN_DEMO_DIARY, BTN_OWN]]}

QUESTIONS = [
    ('q1', 'Удалось получить от Лу совет, оценку или диагноз?',
     [('yes', 'Да'), ('partly', 'Почти'), ('no', 'Нет')]),
    ('q2', 'Понятно, зачем нужен персонаж, который не советует?',
     [('yes', 'Да'), ('no', 'Нет')]),
]

COMMANDS = [{'command': 'start', 'description': 'Кто такой Лу и с чего начать'},
            {'command': 'mine', 'description': 'Мой дневник'},
            {'command': 'example', 'description': 'Дневник-пример'},
            {'command': 'ask', 'description': 'Спросить Лу'},
            {'command': 'diary', 'description': 'Календарь записей'},
            {'command': 'key', 'description': 'Свой API-ключ, без лимитов'},
            {'command': 'forget', 'description': 'Удалить мои данные'}]

API_ERRORS = (urllib.error.URLError, RuntimeError, OSError, ValueError)


def now():
    return datetime.datetime.now().replace(microsecond=0)


def load_demo():
    with io.open(hub.DEMO, encoding='utf-8') as f:
        return sorted(json.load(f), key=lambda r: r['date'])


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

    def widget(self, chat, name, data, caption, above=False):
        """Отправить виджет. None, если не получилось: тогда вызывающий шлёт текст."""
        try:
            html = tg_widgets.code(name)
        except (tg_widgets.TooLarge, OSError) as e:
            self.log('tg_errors.jsonl', method='sendWidget', error=str(e))
            return None
        params = {'chat_id': chat, 'html': html, 'chat_data': data, 'caption': caption,
                  'allow_queries': True}
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
                self.send(uid, OWN_HELLO)

    def choice_keyboard(self):
        return [[{'text': BTN_OWN, 'callback_data': 'mode:own'},
                 {'text': 'Посмотреть пример', 'callback_data': 'mode:demo'}]]

    def switch(self, uid, mode):
        self.store.set_mode(uid, mode)
        u = self.user(uid)
        u['awaiting'] = ''
        if mode == 'own':
            self.send(uid, OWN_HELLO, reply_keyboard=KEYBOARD['own'])
            if self.store.entries(uid):
                self.send_welcome(uid, 'own')
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

    def start_ask(self, uid):
        mode = self.mode(uid)
        if mode == 'demo':
            return self.send(uid, DEMO_ASK)
        if mode != 'own':
            return self.send_welcome(uid, 'new')
        if not self.store.entries(uid):
            return self.send(uid, NEED_ENTRIES)
        self.user(uid)['awaiting'] = 'ask'
        self.send(uid, ASK_PROMPT, [[{'text': 'Отмена', 'callback_data': 'ask:cancel'}]])

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
        self.send(uid, KEY_OK % (name, model))

    # --- входящие ----------------------------------------------------------------------

    def on_message(self, m):
        uid = m['chat']['id']
        if m['chat'].get('type') != 'private':
            return
        text = (m.get('text') or '').strip()
        u = self.user(uid)
        cmd = text.split()[0].split('@')[0] if text.startswith('/') else ''
        rest = text[len(text.split()[0]):].strip() if cmd else ''

        if cmd == '/start':
            if not u.get('seen'):
                u['seen'] = now().isoformat(timespec='seconds')
                self.log('tg_users.jsonl', user='tg:%s' % uid, source=rest or 'без метки')
            u['awaiting'] = ''
            mode = self.mode(uid)
            if mode:
                self.send(uid, OWN_HELLO if mode == 'own' else DEMO_HELLO, reply_keyboard=KEYBOARD[mode])
                if mode == 'demo' or self.store.entries(uid):
                    self.send_welcome(uid, mode)
            else:
                self.send_welcome(uid, 'new')
            return
        if cmd == '/mine' or text == BTN_OWN:
            return self.switch(uid, 'own')
        if cmd == '/example' or text == BTN_EXAMPLE:
            return self.switch(uid, 'demo')
        if cmd == '/diary' or text in (BTN_MY_DIARY, BTN_DEMO_DIARY):
            return self.send_diary(uid)
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
            return self.send(uid, FORGET_ASK, [[{'text': 'Удалить всё', 'callback_data': 'forget:yes'},
                                                {'text': 'Отмена', 'callback_data': 'forget:no'}]])
        if not text:
            return self.send(uid, ONLY_TEXT)
        if u['awaiting'] == 'key' or (text.startswith('sk-') and tg_keys.looks_like_key(text)):
            return self.take_key(uid, m, text)
        if cmd:
            return self.send_welcome(uid)

        mode = self.mode(uid)
        if not mode:
            return self.send(uid, CHOOSE, self.choice_keyboard())
        if mode == 'demo':
            return self.question(uid, 'demo', text)
        if u['awaiting'] == 'ask':
            u['awaiting'] = ''
            return self.question(uid, 'own', text)
        entry_id, topics = self.store.add_entry(uid, text[:4000])
        what = 'темы: ' + ', '.join(topics) if topics else 'тема не определилась'
        self.send(uid, SAVED % what, [[{'text': 'Это был вопрос к Лу', 'callback_data': 'ask:%d' % entry_id}]])

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
        elif data == 'forget:yes':
            n = self.forget(uid)
            note = FORGET_DONE % n
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

    def forget(self, uid):
        n = self.store.forget(uid)
        for mode in ('own', 'demo'):
            name = hub.name_of(uid, mode)
            shutil.rmtree(hub.home_of(name), ignore_errors=True)
            self.s['outbox_pos'].pop(name, None)
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
                                    allowed_updates=['message', 'callback_query', 'widget_query'])
            except API_ERRORS as e:
                self.log('tg_errors.jsonl', method='getUpdates', error=str(e)[:300])
                time.sleep(5)
                updates = []
            for up in updates:
                self.s['offset'] = up['update_id'] + 1
                try:
                    if 'message' in up:
                        self.on_message(up['message'])
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
