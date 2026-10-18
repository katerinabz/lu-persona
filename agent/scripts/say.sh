#!/usr/bin/env bash
# Дописать сообщение в исходящие.
#
#   say.sh <кому> <текст>
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: say.sh <кому> <текст>" >&2; exit 2; }
to="$1"; shift
me="${AGENT_NAME:-lu}"
python3 - "agent/$me/outbox.jsonl" "$to" "$*" <<'PY'
import sys, io, json, datetime
path, to, text = sys.argv[1], sys.argv[2], sys.argv[3]
line = json.dumps({"to": to, "at": datetime.datetime.now().isoformat(timespec="seconds"),
                   "text": text}, ensure_ascii=False)
io.open(path, "a", encoding="utf-8", newline="\n").write(line + "\n")
print("сказано %s" % to)
PY
