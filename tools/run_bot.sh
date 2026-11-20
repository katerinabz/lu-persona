#!/bin/zsh
# Бот в Телеграме целиком: диспетчер сессий (hub.py) и мост (tg_bridge.py).
#
#     LU_ENV_FILE=/путь/к/.env tools/run_bot.sh
#
# Файл .env задаёт ключ модели и токен бота. Если один из процессов упал, второй
# останавливается и скрипт выходит с ошибкой: launchd (KeepAlive) поднимет оба заново.
cd "$(dirname "$0")/.."
if [ -n "$LU_ENV_FILE" ]; then
  set -a; . "$LU_ENV_FILE"; set +a
fi
# Бот читает дневники из своей базы, а не из файла-примера разработчика.
unset LU_DIARY_FILE LU_DIARY_DB LU_DIARY_USER
export PYTHONUNBUFFERED=1
mkdir -p logs
PY=${LU_PYTHON:-python3}
$PY tools/hub.py >> logs/hub.out 2>&1 &
HUB=$!
$PY tools/tg_bridge.py >> logs/bridge.out 2>&1 &
BRIDGE=$!
# Пока бот работает, Mac не засыпает сам (крышка закрыта — всё равно уснёт).
caffeinate -i -w $$ &
trap 'kill $HUB $BRIDGE 2>/dev/null' EXIT TERM INT
while kill -0 $HUB 2>/dev/null && kill -0 $BRIDGE 2>/dev/null; do
  sleep 5
done
echo "$(date '+%F %T') один из процессов остановился, перезапуск через launchd" >> logs/bridge.out
exit 1
