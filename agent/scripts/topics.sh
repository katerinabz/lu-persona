#!/usr/bin/env bash
# Сколько записей по каждой теме за период.
#
#   topics.sh --since 2026-06-01 --until 2026-07-01
set -euo pipefail
exec python3 "$(dirname "$0")/_run.py" topics "$@"
