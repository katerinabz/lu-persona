#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Проверка текста Лу перед отправкой человеку.

    from _guard import check
    for v in check(text):
        print(v['rule'], v['kind'], v['fragment'])

Ловит грубые срывы, которые видно по словам: совет (правило 2 из PERSONA.md), термин
психологии (правило 4), эмодзи и восклицательный знак (правило 5), заглушка из примеров
PERSONA.md вроде [N] или [дата] вместо числа из данных и, если передана опора, число,
которого нет ни в промпте, ни в ответах инструментов (правило 6). Тон, утешение и оценку
человека по словам не поймать, их оценивает человек при чтении отчёта.

Одна проверка на двух местах: `say` в tools/run_session.py не отправляет сообщение с
нарушением.

    python3 agent/scripts/_guard.py "текст"     печатает нарушения, код 1, если они есть
"""
import re
import sys

# Шаблоны ищутся в тексте в нижнем регистре. \b в Python понимает кириллицу, поэтому
# «стоит» не срабатывает на «состоит» и «постоит».
ADVICE = [
    r'\bсоветую\b', r'\bсоветовал[аи]? бы\b', r'\bмой совет\b',
    r'\bстоит\b', r'\bстоило бы\b',
    r'\bпопробуй(?:те)?\b',
    r'\bрекомендую\b', r'\bрекомендовал[аи]? бы\b',
    r'\bлучше бы\b',
    r'\b(?:вам|тебе) (?:нужно|надо|следует)\b',
    r'\bпредлагаю\b',
]

TERMS = [
    r'\bдепресс\w*', r'\bвыгоран\w*', r'\bвыгорел\w*', r'\bтревожн\w*',
    r'\bтравм\w*', r'\bрасстройств\w*',
]

# Основные диапазоны эмодзи и пиктограмм. Обычная типографика (кавычки, тире, стрелки)
# в них не попадает.
EMOJI = re.compile(
    '[\U0001F000-\U0001FAFF'   # пиктограммы, смайлики, транспорт, флаги, дополнения
    '☀-➿'            # разные символы и дингбаты: ☀ ✅ ✨ ❤
    '⬀-⯿'            # стрелки-пиктограммы и звёзды: ⭐
    '️‍]'            # селектор эмодзи и склейка
)

# Заглушки из примеров в PERSONA.md. В ответе они значат, что модель скопировала пример, а не
# сходила за данными.
PLACEHOLDER = re.compile(r'\[(?:N|дата|тема)\]')

NUM = re.compile(r'\d+')


def numbers(text):
    """Все целые числа текста. Дата 2026-07-11 даёт 2026, 7 и 11."""
    return {int(n) for n in NUM.findall(text or '')}

RULES = (
    ('совет', 2, ADVICE),
    ('термин', 4, TERMS),
)


def _snippet(text, start, end, pad=20):
    a = max(0, start - pad)
    b = min(len(text), end + pad)
    return text[a:b].replace('\n', ' ').strip()


def check(text, support=None):
    """Список нарушений. Каждое: kind, rule (номер из PERSONA.md), fragment, context.

    support: всё, что модель видела за сессию (промпт, сообщения, ответы инструментов). Если
    задан, число в тексте без пары в support считается выдуманным (правило 6). В прогоне
    10.11 Лу назвал «всего 30 записей», хотя ни один инструмент такого числа не вернул.
    """
    text = text or ''
    low = text.lower()
    out = []
    for kind, rule, patterns in RULES:
        for p in patterns:
            for m in re.finditer(p, low):
                out.append({'kind': kind, 'rule': rule,
                            'fragment': text[m.start():m.end()],
                            'context': _snippet(text, m.start(), m.end())})
    for m in EMOJI.finditer(text):
        out.append({'kind': 'эмодзи', 'rule': 5, 'fragment': m.group(0),
                    'context': _snippet(text, m.start(), m.end())})
    for m in PLACEHOLDER.finditer(text):
        out.append({'kind': 'заглушка', 'rule': 6, 'fragment': m.group(0),
                    'context': _snippet(text, m.start(), m.end())})
    if support is not None:
        allowed = numbers(support)
        for m in NUM.finditer(text):
            if int(m.group(0)) not in allowed:
                out.append({'kind': 'число без опоры', 'rule': 6, 'fragment': m.group(0),
                            'context': _snippet(text, m.start(), m.end())})
    for m in re.finditer('!', text):
        out.append({'kind': 'восклицание', 'rule': 5, 'fragment': '!',
                    'context': _snippet(text, m.start(), m.end())})
    out.sort(key=lambda v: (v['rule'], v['kind']))
    return out


def describe(violations):
    """Коротко, одной строкой: для ответа инструмента и для таблицы отчёта."""
    return '; '.join('правило %d (%s): «%s»' % (v['rule'], v['kind'], v['fragment'])
                     for v in violations)


if __name__ == '__main__':
    found = check(' '.join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read())
    for v in found:
        print('правило %d, %s: «%s» в «%s»' % (v['rule'], v['kind'], v['fragment'], v['context']))
    sys.exit(1 if found else 0)
