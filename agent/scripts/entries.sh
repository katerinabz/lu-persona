#!/usr/bin/env bash
# Записи дневника.
#
#   entries.sh --topic работа
#   entries.sh --since 2026-06-01 --until 2026-07-01
#   entries.sh --since 2026-07-10 --limit 20
#
# Печатает по строке на запись: дата, темы, текст. Что спрашивать, решаешь ты.
set -euo pipefail
exec python3 "$(dirname "$0")/_run.py" entries "$@"
