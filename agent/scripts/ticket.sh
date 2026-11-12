#!/usr/bin/env bash
# Сообщить, что инструмента не хватает или он сломан.
#
#   ticket.sh "что понадобилось, зачем, и что напечатал упавший скрипт"
set -euo pipefail
[ $# -ge 1 ] || { echo "usage: ticket.sh <текст>" >&2; exit 2; }
me="${AGENT_NAME:-lu}"
python3 - "agent/$me/outbox.jsonl" "$*" <<'PY'
import sys, io, json, datetime
path, text = sys.argv[1], sys.argv[2]
line = json.dumps({"to": "operator", "kind": "ticket",
                   "at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "text": text}, ensure_ascii=False)
io.open(path, "a", encoding="utf-8", newline="\n").write(line + "\n")
print("тикет записан")
PY
