#!/usr/bin/env bash
# Сколько раз две темы встречались в одной записи.
#
#   link.sh работа сон
set -euo pipefail
exec python3 "$(dirname "$0")/_run.py" link "$@"
