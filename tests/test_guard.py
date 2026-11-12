# -*- coding: utf-8 -*-
"""Проверка _guard.py: 5 строк с нарушениями, 5 чистых и все примеры из PERSONA.md.

    python3 -m unittest discover -s tests
"""
import io
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'agent', 'scripts'))
import _guard  # noqa: E402

BAD = [
    ('Про работу у вас 14 записей. Советую взять отпуск.', 2, 'Советую'),
    ('Вам стоит поговорить с руководителем.', 2, 'стоит'),
    ('Попробуйте ложиться раньше, в 9 записях вы пишете про сон.', 2, 'Попробуйте'),
    ('Похоже на выгорание: 9 записей про усталость.', 4, 'выгорание'),
    ('С 3 июня у вас 13 записей про тревогу 🙂', 5, '🙂'),
    ('Вы молодец, записей стало больше!', 5, '!'),
    ('Это похоже на тревожное расстройство.', 4, 'тревожное'),
    ('Лучше бы вы спали больше. Вам нужно отдохнуть.', 2, 'Лучше бы'),
]

CLEAN = [
    'Про работу у вас 14 записей с 3 июня по 11 июля.',
    'Тема «тревога» встречается 13 раз, последняя запись от 11 июля.',
    'Записей за прошлый год нет, самая ранняя от 3 июня. Сравнить не с чем.',
    'Тема «работа» состоит из 14 записей, тема «сон» из 9.',
    'Что с этим делать, в записях не сказано.',
]


def persona_examples():
    """Ответы Лу из раздела «Примеры» PERSONA.md: строки, начинающиеся с «> »."""
    p = os.path.join(ROOT, 'agent', 'lu', 'PERSONA.md')
    with io.open(p, encoding='utf-8') as f:
        return [l[2:].strip() for l in f if l.startswith('> ')]


class GuardTest(unittest.TestCase):
    def test_violations_found(self):
        for text, rule, fragment in BAD:
            with self.subTest(text=text):
                found = _guard.check(text)
                self.assertTrue(found, text)
                self.assertIn((rule, fragment), [(v['rule'], v['fragment']) for v in found])

    def test_clean_pass(self):
        for text in CLEAN:
            with self.subTest(text=text):
                self.assertEqual(_guard.check(text), [])

    def test_placeholder_caught(self):
        found = _guard.check('Про работу у вас [N] записей с [дата].')
        self.assertEqual([(v['rule'], v['fragment']) for v in found], [(6, '[N]'), (6, '[дата]')])

    def test_describe_names_rule_and_fragment(self):
        s = _guard.describe(_guard.check('Рекомендую поспать!'))
        self.assertIn('правило 2', s)
        self.assertIn('«Рекомендую»', s)
        self.assertIn('правило 5', s)

    def test_number_without_support_caught(self):
        support = 'работа 14\nтревога 13\n2026-06-03 | работа | ...\n2026-07-11 | сон | ...'
        found = _guard.check('Всего 30 записей, про работу 14, с 3 июня по 11 июля.', support)
        self.assertEqual([(v['rule'], v['fragment']) for v in found], [(6, '30')])

    def test_numbers_from_support_pass(self):
        support = 'работа 14\n2026-06-03 | работа | ...\n2026-07-11 | сон | ...'
        self.assertEqual(_guard.check('Про работу 14 записей с 2026-06-03 по 11 июля.', support), [])

    def test_no_support_no_number_check(self):
        self.assertEqual(_guard.check('Всего 30 записей.'), [])

    def test_empty_text(self):
        self.assertEqual(_guard.check(''), [])
        self.assertEqual(_guard.check(None), [])


if __name__ == '__main__':
    unittest.main()
