#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Адаптер к дневнику. Единственное место, которое знает, где лежат записи.

Настройка через переменные окружения:

    LU_DIARY_DB    путь к базе дневника (SQLite)
    LU_DIARY_URL   адрес API дневника, если ходим по сети
    LU_DIARY_USER  чей дневник читаем

Пока ничего не настроено, функции поднимают NotConfigured с текстом, который
персонажу понятно читать: он отличает «записей нет» от «инструмент не работает».
"""
import os


class NotConfigured(Exception):
    pass


def _target():
    db, url = os.environ.get('LU_DIARY_DB'), os.environ.get('LU_DIARY_URL')
    if not db and not url:
        raise NotConfigured(
            'дневник не подключен: не задана ни LU_DIARY_DB, ни LU_DIARY_URL. '
            'Это настройка окружения, а не отсутствие записей.')
    return db or url


def entries(topic=None, since=None, until=None, limit=50):
    """Записи дневника. Возвращает список словарей: date, text, topics."""
    _target()
    raise NotConfigured('чтение записей ещё не реализовано: подключить к дневнику в _diary.py')


def topics(since=None, until=None):
    """Сколько записей по каждой теме за период. Возвращает список пар (тема, число)."""
    _target()
    raise NotConfigured('статистика по темам ещё не реализована: подключить в _diary.py')


def link(topic_a, topic_b):
    """Сколько раз две темы встречались в одной записи. Возвращает число и список дат."""
    _target()
    raise NotConfigured('связь тем ещё не реализована: подключить в _diary.py')
