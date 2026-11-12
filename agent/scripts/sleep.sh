#!/usr/bin/env bash
# Назначить себе следующее пробуждение, в минутах.
#
#   sleep.sh <минуты>
#
# Это решает персонаж, а не расписание. Много нового значит вернуться скоро, пусто значит позже.
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: sleep.sh <минуты>" >&2; exit 2; }
me="${AGENT_NAME:-lu}"
mkdir -p "agent/$me/state"
printf '%s\n' "$1" > "agent/$me/state/sleep.txt"
echo "следующее пробуждение через $1 минут"
