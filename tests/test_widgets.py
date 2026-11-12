# -*- coding: utf-8 -*-
"""Проверка виджетов Телеграма и моста без сети: размеры, данные, обработка нажатий.

    python3 -m unittest discover -s tests
"""
import json
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ['LU_DIARY_FILE'] = os.path.join(ROOT, 'demo', 'дневник.json')
sys.path.insert(0, os.path.join(ROOT, 'tools'))
import tg_widgets  # noqa: E402
import hub  # noqa: E402
import tg_bridge  # noqa: E402
import tg_store  # noqa: E402


class Sizes(unittest.TestCase):
    def test_code_fits_server_limits(self):
        for n in tg_widgets.NAMES:
            html = tg_widgets.code(n)
            self.assertLessEqual(tg_widgets.compressed_size(html), tg_widgets.CODE_LIMIT, n)
            self.assertLessEqual(len(html.encode('utf-8')), 32768, n)

    def test_no_forbidden_css(self):
        # Песочница Телеграма не знает vh, :has и color-mix: виджет развалится молча.
        for n in tg_widgets.NAMES:
            html = tg_widgets.code(n)
            for bad in ('vh', ':has(', 'color-mix(', '<input', '<textarea', 'fetch('):
                if bad == 'vh':
                    self.assertNotRegex(html, r'\d+vh\b', n)
                else:
                    self.assertNotIn(bad, html, n)


class Data(unittest.TestCase):
    rows = tg_widgets.load_entries()

    def test_demo_diary(self):
        a, n = tg_widgets.span(self.rows)
        self.assertEqual(a.isoformat(), '2026-06-03')
        self.assertEqual(n, 39)
        self.assertEqual(len(self.rows), 34)

    def test_welcome(self):
        d = json.loads(tg_widgets.welcome_data(self.rows, [1]))
        self.assertEqual(d['k'], 'demo')
        self.assertEqual(d['n'], 34)
        self.assertEqual(d['t'][0], ['работа', 14])
        self.assertEqual(d['u'], [1])
        self.assertEqual(len(d['q']), 3)

    def test_diary_masks_and_pairs(self):
        d = json.loads(tg_widgets.diary_data(self.rows))
        self.assertEqual(d['n'], 39)
        self.assertEqual(len(d['t']), 6)
        days = {k for k, _ in d['e']}
        self.assertIn(0, days)
        self.assertIn(38, days)
        self.assertTrue(all(m > 0 for _, m in d['e']))
        self.assertLessEqual(len(d['l']), 3)

    def test_all_data_under_limit(self):
        texts = ['Про работу 14 записей, про тревогу 13, про сон 9, про отношения 6, про спорт 5, про деньги 3.']
        for t in texts:
            self.assertLessEqual(len(tg_widgets.answer_data(self.rows, t).encode('utf-8')), 1024)
        for i in range(39):
            self.assertLessEqual(len(tg_widgets.entry_text(self.rows, i).encode('utf-8')), 1024)

    def test_answer_topics_in_order_of_mention(self):
        d = json.loads(tg_widgets.answer_data(self.rows, 'Сна касаются 9 записей, работы 14, тревоги 13, денег 3.'))
        self.assertEqual([t for t, _ in d['t']], ['сон', 'работа', 'тревога'])

    def test_answer_dates(self):
        d = json.loads(tg_widgets.answer_data(self.rows, 'Первая запись от 3 июня, последняя от 2026-07-11.'))
        self.assertEqual(d['h'], [0, 38])
        self.assertEqual(d['t'][0][0], 'все записи')

    def test_welcome_kinds(self):
        self.assertEqual(json.loads(tg_widgets.welcome_data([], kind='new')), {'k': 'new'})
        d = json.loads(tg_widgets.welcome_data([], kind='own'))
        self.assertEqual((d['k'], d['n']), ('own', 0))
        self.assertNotIn('q', d)

    def test_diary_keeps_untagged_days_and_mode(self):
        rows = [{'date': '2026-11-10', 'text': 'просто день', 'topics': []},
                {'date': '2026-11-12', 'text': 'работа', 'topics': ['работа']}]
        d = json.loads(tg_widgets.diary_data(rows, 'own'))
        self.assertEqual(d['e'], [[0, 0], [2, 1]])
        self.assertEqual(d['m'], 'own')

    def test_answer_none_without_topics_or_dates(self):
        self.assertIsNone(tg_widgets.answer_data(self.rows, 'Такого в дневнике нет.'))

    def test_date_outside_period_ignored(self):
        d = json.loads(tg_widgets.answer_data(self.rows, 'Про сон есть запись от 1 мая.'))
        self.assertEqual(d['h'], [])

    def test_entry_text(self):
        self.assertTrue(tg_widgets.entry_text(self.rows, 0).startswith('«'))
        self.assertEqual(tg_widgets.entry_text(self.rows, 1000), 'Записи за этот день нет.')


class FakeBridge(tg_bridge.Bridge):
    """Мост без сети: каждый вызов Bot API записывается, отдельные методы по желанию падают."""

    def __init__(self, home, fail=()):
        self.calls, self.fail = [], set(fail)
        self.api = ''
        self.store = tg_store.Store(os.path.join(home, 'lu.db'))
        self.logs = home
        self.state = os.path.join(home, 'tg.json')
        self.s = self.load_state()
        self._demo = None

    def call(self, method, http_timeout=40, **params):
        self.calls.append((method, params))
        if method in self.fail:
            raise RuntimeError('%s: Bad Request' % method)
        return {'message_id': len(self.calls)}

    def methods(self):
        return [m for m, _ in self.calls]

    def texts(self):
        return [p.get('text') for m, p in self.calls if m in ('sendMessage', 'editMessageText')]

    def upload(self, method, file_field, filename, content, **params):
        self.calls.append((method, dict(params, filename=filename, content=content.decode('utf-8'))))
        return {'message_id': len(self.calls)}

    def buttons(self):
        """Подписи и данные кнопок последнего сообщения или правки."""
        p = [p for m, p in self.calls if m in ('sendMessage', 'editMessageText')][-1]
        kb = (p.get('reply_markup') or {}).get('inline_keyboard') or []
        return {b['text']: b['callback_data'] for row in kb for b in row}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.old = hub.AGENTS, hub.USERS, hub.STATE
        hub.STATE = os.path.join(self.dir, 'hub.json')
        hub.AGENTS = os.path.join(self.dir, 'agent')
        hub.USERS = os.path.join(hub.AGENTS, 'users')
        self.stores = []

    def tearDown(self):
        for st in self.stores:
            st.close()
        hub.AGENTS, hub.USERS, hub.STATE = self.old
        shutil.rmtree(self.dir)

    def msg(self, text, uid=7, mid=100):
        return {'chat': {'id': uid, 'type': 'private'}, 'text': text, 'message_id': mid}

    def inbox(self, mode, uid=7):
        p = os.path.join(hub.home_of(hub.name_of(uid, mode)), 'inbox.jsonl')
        if not os.path.exists(p):
            return []
        with open(p, encoding='utf-8') as f:
            return [json.loads(x) for x in f]

    def outbox(self, mode, *texts, uid=7, kind=None):
        name = hub.ensure_workspace(uid, mode)
        with open(os.path.join(hub.home_of(name), 'outbox.jsonl'), 'a', encoding='utf-8') as f:
            for t in texts:
                o = {'to': 'tg:%d' % uid, 'text': t}
                if kind:
                    o['kind'] = kind
                f.write(json.dumps(o, ensure_ascii=False) + '\n')

    def bridge(self, mode=None, fail=()):
        b = FakeBridge(self.dir, fail)
        self.stores.append(b.store)
        if mode:
            b.store.set_mode(7, mode)
        return b


class Onboarding(Base):
    def test_new_person_gets_choice(self):
        b = self.bridge()
        b.on_message(self.msg('/start курс'))
        self.assertEqual(b.methods(), ['sendWidget'])
        self.assertEqual(json.loads(b.calls[0][1]['chat_data']), {'k': 'new'})

    def test_choice_falls_back_to_text_with_buttons(self):
        b = self.bridge(fail={'sendWidget'})
        b.on_message(self.msg('/start'))
        self.assertEqual(b.calls[1][1]['text'], tg_bridge.INTRO_NEW)
        self.assertIn('inline_keyboard', b.calls[1][1]['reply_markup'])

    def test_text_before_choice_asks_to_choose(self):
        b = self.bridge()
        b.on_message(self.msg('привет'))
        self.assertEqual(b.texts(), [tg_bridge.CHOOSE])
        self.assertEqual(self.inbox('demo') + self.inbox('own'), [])

    def test_choose_demo_from_widget(self):
        b = self.bridge()
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': 'mode:demo'})
        self.assertEqual(b.methods()[0], 'answerWidgetQuery')
        self.assertEqual(b.store.person(7)['mode'], 'demo')
        self.assertEqual(b.calls[1][1]['reply_markup']['keyboard'], tg_bridge.KEYBOARD['demo'])
        self.assertEqual(json.loads(b.calls[2][1]['chat_data'])['n'], 34)

    def test_choose_own_from_button(self):
        b = self.bridge()
        b.on_callback({'id': 'c', 'from': {'id': 7}, 'data': 'mode:own'})
        self.assertEqual(b.store.person(7)['mode'], 'own')
        self.assertEqual(b.texts(), [tg_bridge.OWN_HELLO])
        self.assertNotIn('sendWidget', b.methods())  # записей нет: сводка пустая, не шлётся

    def test_no_course_in_texts(self):
        for name in dir(tg_bridge):
            v = getattr(tg_bridge, name)
            if isinstance(v, str):
                for bad in ('ИТМО', 'Учебный', 'учебный', 'курс'):
                    self.assertNotIn(bad, v, name)


class OwnDiary(Base):
    def test_message_becomes_entry(self):
        b = self.bridge('own')
        b.on_message(self.msg('Не выспалась, на работе дедлайн'))
        rows = b.store.entries(7)
        self.assertEqual(rows[0]['topics'], ['работа', 'сон'])
        self.assertEqual(b.texts(), [tg_bridge.SAVED % 'темы: работа, сон'])
        self.assertEqual(self.inbox('own'), [])

    def test_tagger_edge_words(self):
        self.assertEqual(tg_store.tag('Поссорилась с мамой, потом долго не могла уснуть'), ['сон', 'отношения'])
        self.assertEqual(tg_store.tag('Вернула долг другу'), ['отношения', 'деньги'])
        self.assertEqual(tg_store.tag('Другой день, другие мысли'), [])

    def test_ask_needs_entries(self):
        b = self.bridge('own')
        b.on_message(self.msg('Спросить Лу'))
        self.assertEqual(b.texts(), [tg_bridge.NEED_ENTRIES])

    def test_ask_button_then_question(self):
        b = self.bridge('own')
        b.store.add_entry(7, 'бегала')
        b.on_message(self.msg('Спросить Лу'))
        b.on_message(self.msg('О чём я пишу чаще?'))
        self.assertEqual(self.inbox('own')[0]['text'], 'О чём я пишу чаще?')
        self.assertEqual(len(b.store.entries(7)), 1)
        self.assertIn('sendChatAction', b.methods())

    def test_ask_command_with_text(self):
        b = self.bridge('own')
        b.on_message(self.msg('/ask Что со сном?'))
        self.assertEqual(self.inbox('own')[0]['text'], 'Что со сном?')

    def test_entry_turned_into_question(self):
        b = self.bridge('own')
        b.on_message(self.msg('Почему я всё время устаю?'))
        b.on_callback({'id': 'c', 'from': {'id': 7}, 'data': 'ask:1', 'message': {'message_id': 5}})
        self.assertEqual(b.store.entries(7), [])
        self.assertEqual(self.inbox('own')[0]['text'], 'Почему я всё время устаю?')
        self.assertIn(tg_bridge.SAVED_AS_QUESTION, b.texts())

    def test_own_and_demo_memory_apart(self):
        b = self.bridge('own')
        b.on_message(self.msg('/ask вопрос о своём'))
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': 'q:0'})
        self.assertEqual(len(self.inbox('own')), 1)
        self.assertEqual(self.inbox('demo')[0]['text'], tg_widgets.QUESTIONS[0])

    def test_entry_request_by_mode(self):
        b = self.bridge('own')
        b.store.add_entry(7, 'Своя запись про сон')
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': 'e:own:0'})
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': 'e:demo:0'})
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': 'e:0'})
        own, demo, legacy = [p['data'] for m, p in b.calls]
        self.assertIn('Своя запись', own)
        self.assertEqual(demo, legacy)
        self.assertNotIn('Своя запись', demo)

    def test_forget(self):
        b = self.bridge('own')
        b.store.add_entry(7, 'запись')
        b.on_message(self.msg('/ask вопрос'))
        b.on_callback({'id': 'c', 'from': {'id': 7}, 'data': 'forget:yes', 'message': {'message_id': 5}})
        self.assertIn(tg_bridge.FORGET_DONE % 1, b.texts())
        self.assertFalse(os.path.exists(hub.home_of(hub.name_of(7, 'own'))))
        self.assertEqual(b.store.person(7)['mode'], '')


class DemoDiary(Base):
    def test_text_goes_to_inbox_with_typing(self):
        b = self.bridge('demo')
        b.on_message(self.msg('Как у меня с работой?'))
        self.assertEqual(self.inbox('demo')[0]['from'], 'tg:7')
        self.assertEqual(b.methods(), ['sendChatAction'])
        self.assertEqual(b.waiting_users(), ['7'])

    def test_question_button(self):
        b = self.bridge('demo')
        b.on_widget_query({'id': 'w1', 'from': {'id': 7}, 'message_id': 5, 'data': 'q:1'})
        self.assertEqual(self.inbox('demo')[0]['text'], tg_widgets.QUESTIONS[1])
        self.assertEqual(b.methods()[0], 'answerWidgetQuery')  # ответ на нажатие первым, до 15 секунд
        edit = [p for m, p in b.calls if m == 'editMessageWidgetData'][0]
        self.assertEqual(json.loads(edit['chat_data'])['u'], [1])
        self.assertEqual(b.texts(), [tg_bridge.QUESTION_SENT % tg_widgets.QUESTIONS[1]])

    def test_bad_request_is_refused(self):
        b = self.bridge('demo')
        for d in ('q:9', 'f:q1:maybe', 'zzz', 'e:x', 'e:other:1'):
            b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': d})
        self.assertEqual(self.inbox('demo'), [])
        self.assertTrue(all(m == 'answerWidgetQuery' for m in b.methods()))

    def test_feedback_logged_and_edited(self):
        b = self.bridge('demo')
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'message_id': 9, 'data': 'f:q1:partly'})
        with open(os.path.join(self.dir, 'tg_feedback.jsonl'), encoding='utf-8') as f:
            log = [json.loads(x) for x in f]
        self.assertEqual(log[0]['answer'], 'partly')
        edit = [p for m, p in b.calls if m == 'editMessageWidgetData'][0]
        self.assertEqual(json.loads(edit['chat_data']), {'a': {'q1': 'partly'}})


class Limits(Base):
    def test_personal_limit(self):
        b = self.bridge('demo')
        b.user(7)['count'] = tg_bridge.DAILY_LIMIT
        b.on_message(self.msg('вопрос'))
        self.assertEqual(self.inbox('demo'), [])
        self.assertIn('/key', b.texts()[0])

    def test_global_limit(self):
        b = self.bridge('demo')
        b.s['global'] = {'day': tg_bridge.now().date().isoformat(), 'count': tg_bridge.GLOBAL_LIMIT}
        b.on_widget_query({'id': 'w', 'from': {'id': 7}, 'data': 'q:0'})
        self.assertEqual(self.inbox('demo'), [])
        self.assertEqual(b.texts(), [tg_bridge.LIMIT_ALL])

    def test_entries_have_no_limit(self):
        b = self.bridge('own')
        b.user(7)['count'] = tg_bridge.DAILY_LIMIT
        b.on_message(self.msg('запись'))
        self.assertEqual(len(b.store.entries(7)), 1)

    def test_own_key_has_no_limit(self):
        b = self.bridge('demo')
        b.store.set_key(7, 'k' * 30, 'https://x', 'm')
        b.user(7)['count'] = tg_bridge.DAILY_LIMIT
        b.on_message(self.msg('вопрос'))
        self.assertEqual(len(self.inbox('demo')), 1)

    def test_limited_entry_question_stays_entry(self):
        b = self.bridge('own')
        b.on_message(self.msg('запись'))
        b.user(7)['count'] = tg_bridge.DAILY_LIMIT
        b.on_callback({'id': 'c', 'from': {'id': 7}, 'data': 'ask:1', 'message': {'message_id': 5}})
        self.assertEqual(len(b.store.entries(7)), 1)


class Keys(Base):
    def test_key_is_deleted_checked_and_saved(self):
        b = self.bridge('demo')
        old = tg_keys_check = tg_bridge.tg_keys.check
        tg_bridge.tg_keys.check = lambda k: ('NeuralDeep', 'https://x', 'qwen', None)
        try:
            b.on_message(self.msg('/key'))
            b.on_message(self.msg('nd-' + 'a' * 40, mid=55))
        finally:
            tg_bridge.tg_keys.check = old
        self.assertIn(('deleteMessage', {'chat_id': 7, 'message_id': 55}), b.calls)
        self.assertEqual(b.store.person(7)['key'], 'nd-' + 'a' * 40)
        self.assertEqual(b.texts()[-1], tg_bridge.KEY_OK % ('NeuralDeep', 'qwen'))
        self.assertEqual(self.inbox('demo'), [])
        for name in os.listdir(self.dir):  # ключ не попадает в журналы
            if name.endswith('.jsonl'):
                with open(os.path.join(self.dir, name), encoding='utf-8') as f:
                    self.assertNotIn('a' * 40, f.read())

    def test_bad_key(self):
        b = self.bridge('own')
        old = tg_bridge.tg_keys.check
        tg_bridge.tg_keys.check = lambda k: ('OpenAI', None, None, 'OpenAI не принял ключ')
        try:
            b.on_message(self.msg('sk-' + 'b' * 40))
        finally:
            tg_bridge.tg_keys.check = old
        self.assertIsNone(b.store.person(7)['key'])
        self.assertEqual(b.store.entries(7), [])  # ключ не стал записью дневника
        self.assertEqual(b.texts()[-1], tg_bridge.KEY_BAD % 'OpenAI не принял ключ')

    def test_key_off(self):
        b = self.bridge('demo')
        b.store.set_key(7, 'k' * 30, 'https://x', 'm')
        b.on_message(self.msg('/key_off'))
        self.assertIsNone(b.store.person(7)['key'])

    def test_provider_by_prefix(self):
        import tg_keys
        self.assertEqual(tg_keys.provider('sk-or-v1-abc')[0], 'OpenRouter')
        self.assertEqual(tg_keys.provider('sk-proj-abc')[0], 'OpenAI')
        self.assertEqual(tg_keys.provider('abcdef')[0], 'NeuralDeep')


class Delivery(Base):
    def test_reply_as_answer_widget_then_feedback(self):
        b = self.bridge('demo')
        b.on_message(self.msg('вопрос'))
        self.outbox('demo', 'Про работу у вас 14 записей.', 'Такого в дневнике нет.', 'Про сон 9 записей.')
        b.deliver()
        sent = [(m, p.get('caption') or p.get('text')) for m, p in b.calls if m in ('sendWidget', 'sendMessage')]
        self.assertEqual(sent[0], ('sendWidget', 'Про работу у вас 14 записей.'))
        self.assertEqual(sent[1], ('sendMessage', 'Такого в дневнике нет.'))
        self.assertEqual(sent[2], ('sendWidget', 'Про сон 9 записей.'))
        self.assertEqual(sent[3], ('sendWidget', tg_bridge.FEEDBACK_CAPTION))
        self.assertEqual(b.waiting_users(), [])

    def test_own_reply_uses_own_entries(self):
        b = self.bridge('own')
        b.store.add_entry(7, 'Не выспалась')
        self.outbox('own', 'Про сон у вас 1 запись.')
        b.deliver()
        d = json.loads(b.calls[0][1]['chat_data'])
        self.assertEqual((d['m'], d['t'][0][0], d['n']), ('own', 'сон', 1))

    def test_system_message_is_plain_text(self):
        b = self.bridge('demo')
        self.outbox('demo', hub.LIMIT_TOKENS, kind='system')
        b.deliver()
        self.assertEqual(b.methods(), ['sendMessage'])
        self.assertEqual(b.user(7)['replies'], 0)

    def test_reply_falls_back_to_text(self):
        b = self.bridge('demo', fail={'sendWidget'})
        self.outbox('demo', 'Про работу у вас 14 записей.')
        b.deliver()
        self.assertEqual(b.methods(), ['sendWidget', 'sendMessage'])

    def test_long_reply_text_then_widget(self):
        b = self.bridge('demo')
        self.outbox('demo', 'Про работу у вас 14 записей. ' + 'Тема «работа» встречается часто. ' * 40)
        b.deliver()
        self.assertEqual(b.methods(), ['sendMessage', 'sendWidget'])
        self.assertEqual(b.calls[1][1]['caption'], tg_bridge.ANSWER_SHORT_CAPTION)

    def test_waiting_notice_once(self):
        b = self.bridge('demo')
        b.on_message(self.msg('вопрос'))
        b.user(7)['waiting'] -= tg_bridge.WAIT_NOTICE + 1
        b.typing('7')
        b.typing('7', force=True)
        self.assertEqual(b.texts(), [tg_bridge.NOT_ANSWERING])


class Editing(Base):
    def cb(self, b, data, mid=50):
        b.on_callback({'id': 'c', 'from': {'id': 7}, 'data': data, 'message': {'message_id': mid}})

    def three(self, b):
        for i, t in enumerate(['Не выспалась', 'Дедлайн на работе', 'Бегала утром']):
            b.on_message(self.msg(t, mid=200 + i))

    def test_settings_screen(self):
        b = self.bridge('own')
        self.three(b)
        b.on_message(self.msg('Настройки'))
        self.assertIn(tg_bridge.SETTINGS_OWN % (3, 'записи'), b.texts()[-1])
        self.assertIn('Очистить мои записи', b.buttons())
        self.assertIn('Привязать свой ключ', b.buttons())

    def test_list_pages_and_card(self):
        b = self.bridge('own')
        for i in range(7):
            b.store.add_entry(7, 'запись %d' % i)
        b.on_message(self.msg('Мои записи'))
        self.assertTrue(b.texts()[-1].startswith(tg_bridge.LIST_HEAD % (1, 5, 7)))
        self.assertIn('Старее →', b.buttons())
        self.cb(b, 'ls:5')
        self.assertTrue(b.texts()[-1].startswith(tg_bridge.LIST_HEAD % (6, 7, 7)))
        self.assertEqual(b.calls[-1][0], 'editMessageText')  # экран меняется на месте
        self.cb(b, b.buttons()['1'])
        self.assertIn('запись 1', b.texts()[-1])
        self.assertIn('Изменить текст', b.buttons())

    def test_edit_by_button(self):
        b = self.bridge('own')
        b.on_message(self.msg('старый текст'))
        self.cb(b, 'ed:1')
        b.on_message(self.msg('Не спала из-за начальника'))
        e = b.store.entry(7, 1)
        self.assertEqual((e['text'], e['topics']), ('Не спала из-за начальника', ['работа', 'сон']))
        self.assertTrue(b.texts()[-1].startswith(tg_bridge.EDIT_DONE))
        self.assertEqual(len(b.store.entries(7)), 1)

    def test_edit_cancel(self):
        b = self.bridge('own')
        b.on_message(self.msg('текст'))
        self.cb(b, 'ed:1')
        self.cb(b, 'ed:cancel')
        b.on_message(self.msg('новая запись'))
        self.assertEqual(len(b.store.entries(7)), 2)

    def test_edit_in_chat(self):
        b = self.bridge('own')
        b.on_message(self.msg('старый текст', mid=300))
        reply_id = len(b.calls)
        b.on_edited_message(dict(self.msg('Поссорилась с мамой', mid=300)))
        self.assertEqual(b.store.entry(7, 1)['topics'], ['отношения'])
        m, p = b.calls[-1]
        self.assertEqual((m, p['message_id'], p['text']), ('editMessageText', reply_id, tg_bridge.SAVED_EDITED % 'темы: отношения'))

    def test_edit_of_question_ignored(self):
        b = self.bridge('demo')
        b.on_message(self.msg('вопрос', mid=301))
        n = len(b.calls)
        b.on_edited_message(dict(self.msg('другой вопрос', mid=301)))
        self.assertEqual(len(b.calls), n)

    def test_topics_toggle(self):
        b = self.bridge('own')
        b.on_message(self.msg('просто день'))
        self.cb(b, 'tp:1')
        self.cb(b, b.buttons()['🌙 сон'])
        self.assertEqual(b.store.entry(7, 1)['topics'], ['сон'])
        self.assertIn('✓ 🌙 сон', b.buttons())
        self.cb(b, b.buttons()['✓ 🌙 сон'])
        self.assertEqual(b.store.entry(7, 1)['topics'], [])

    def test_delete_with_confirm(self):
        b = self.bridge('own')
        b.on_message(self.msg('лишнее'))
        self.cb(b, 'dl:1')
        self.assertEqual(len(b.store.entries(7)), 1)  # пока только вопрос
        self.cb(b, b.buttons()['Да, удалить'])
        self.assertEqual(b.store.entries(7), [])
        self.assertTrue(b.texts()[-1].startswith(tg_bridge.ENTRY_DELETED))

    def test_other_persons_entry_untouchable(self):
        b = self.bridge('own')
        b.store.add_entry(8, 'чужая запись')
        self.cb(b, 'dl!:1')
        self.cb(b, 'tt:1:0')
        b.user(7)['awaiting'] = 'edit:1'
        b.on_message(self.msg('взлом'))
        self.assertEqual(b.store.entries(8)[0]['text'], 'чужая запись')
        self.assertEqual(b.store.entries(8)[0]['topics'], [])

    def test_clear_entries_keeps_key_and_resets_memory(self):
        b = self.bridge('own')
        self.three(b)
        b.store.set_key(7, 'k' * 30, 'https://x', 'm')
        b.on_message(self.msg('/ask вопрос'))
        self.cb(b, 'set:clear')
        self.assertEqual(len(b.store.entries(7)), 3)
        self.cb(b, 'set:clear!')
        self.assertEqual(b.store.entries(7), [])
        self.assertEqual(b.store.person(7)['key'], 'k' * 30)
        self.assertFalse(os.path.exists(hub.home_of(hub.name_of(7, 'own'))))
        self.assertEqual(b.texts()[-1], tg_bridge.CLEARED % 3)

    def test_forget_memory_keeps_entries(self):
        b = self.bridge('demo')
        b.on_message(self.msg('вопрос'))
        b.store.add_entry(7, 'запись')
        self.cb(b, 'set:mem')
        self.cb(b, 'set:mem!')
        self.assertFalse(os.path.exists(hub.home_of(hub.name_of(7, 'demo'))))
        self.assertEqual(len(b.store.entries(7)), 1)

    def test_export(self):
        b = self.bridge('own')
        self.three(b)
        self.cb(b, 'set:export')
        p = [p for m, p in b.calls if m == 'sendDocument'][0]
        self.assertLess(p['content'].index('Не выспалась'), p['content'].index('Бегала утром'))

    def test_delete_all_from_settings(self):
        b = self.bridge('own')
        self.three(b)
        self.cb(b, 'set:all')
        self.cb(b, b.buttons()['Удалить всё'])
        self.assertEqual(b.store.person(7)['mode'], '')

    def test_command_cancels_pending_edit(self):
        b = self.bridge('own')
        b.on_message(self.msg('текст'))
        self.cb(b, 'ed:1')
        b.on_message(self.msg('Настройки'))
        b.on_message(self.msg('новая запись'))
        self.assertEqual(b.store.entry(7, 1)['text'], 'текст')


class Dispatcher(Base):
    def make(self, code=0, spent=100, answer=True):
        store = tg_store.Store(os.path.join(self.dir, 'lu.db'))
        self.stores.append(store)
        runs = []

        def runner(name, env):
            runs.append((name, env))
            if answer:
                with open(os.path.join(hub.home_of(name), 'outbox.jsonl'), 'a', encoding='utf-8') as f:
                    f.write(json.dumps({'to': 'tg:7', 'at': '2999-01-01T00:00:00', 'text': 'ответ'}) + '\n')
            return code, spent
        h = hub.Hub(store, runner)
        h.save = lambda: None
        h.log = lambda *a, **k: None
        return h, store, runs

    def ask(self, mode, text='вопрос'):
        name = hub.ensure_workspace(7, mode)
        with open(os.path.join(hub.home_of(name), 'inbox.jsonl'), 'a', encoding='utf-8') as f:
            f.write(json.dumps({'from': 'tg:7', 'at': '2026-11-12T10:00:00', 'text': text}) + '\n')
        return name

    def test_workspace_from_template(self):
        name = hub.ensure_workspace(7, 'own')
        home = hub.home_of(name)
        for rel in ('PERSONA.md', 'SESSION_PROMPT.md', 'GOALS.md', 'memory/state.md', 'memory/README.md',
                    'memory/entities/человек.md', 'inbox.jsonl', 'outbox.jsonl'):
            self.assertTrue(os.path.exists(os.path.join(home, rel)), rel)

    def test_env_per_mode(self):
        h, store, runs = self.make()
        store.person(7)
        h.handle(self.ask('own'))
        h.handle(self.ask('demo'))
        own, demo = runs[0][1], runs[1][1]
        self.assertEqual(own['LU_DIARY_DB'], store.path)
        self.assertNotIn('LU_DIARY_FILE', own)
        self.assertEqual(demo['LU_DIARY_FILE'], hub.DEMO)
        self.assertNotIn('LU_DIARY_DB', demo)
        self.assertEqual(h.pending(), [])

    def test_own_key_in_env_and_outside_budget(self):
        h, store, runs = self.make(spent=500)
        store.set_key(7, 'secret' * 5, 'https://x/v1', 'm1')
        h.handle(self.ask('demo'))
        env = runs[0][1]
        self.assertEqual((env['LU_API_KEY'], env['LU_API_BASE'], env['LU_MODEL']), ('secret' * 5, 'https://x/v1', 'm1'))
        self.assertEqual(h.budget()['spent'], 0)

    def test_budget_exceeded_tells_person(self):
        h, store, runs = self.make()
        h.budget()['spent'] = hub.DAILY_TOKENS
        name = self.ask('demo')
        h.handle(name)
        self.assertEqual(runs, [])
        with open(os.path.join(hub.home_of(name), 'outbox.jsonl'), encoding='utf-8') as f:
            o = json.loads(f.read().splitlines()[-1])
        self.assertEqual((o['kind'], o['text']), ('system', hub.LIMIT_TOKENS))
        self.assertEqual(h.pending(), [])

    def test_failed_own_key_reported(self):
        h, store, runs = self.make(code=3, answer=False)
        store.set_key(7, 'secret' * 5, 'https://x/v1', 'm1')
        name = self.ask('own')
        h.handle(name)
        with open(os.path.join(hub.home_of(name), 'outbox.jsonl'), encoding='utf-8') as f:
            self.assertIn(hub.KEY_FAILED, f.read())

    def test_silent_session_reported(self):
        h, store, runs = self.make(answer=False)
        name = self.ask('demo')
        h.handle(name)
        with open(os.path.join(hub.home_of(name), 'outbox.jsonl'), encoding='utf-8') as f:
            self.assertIn(hub.NO_ANSWER, f.read())


if __name__ == '__main__':
    unittest.main()
