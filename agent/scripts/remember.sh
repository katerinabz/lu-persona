#!/usr/bin/env bash
# Дописать датированную строку в заметку, заведя её, если её нет.
#
#   remember.sh <сущность> <что произошло>
#
# Дата проставляется за тебя: память без даты не упорядочивается, а неупорядоченная память это куча.
set -euo pipefail
[ $# -ge 2 ] || { echo "usage: remember.sh <сущность> <что произошло>" >&2; exit 2; }
who="$1"; shift
me="${AGENT_NAME:-lu}"
python3 - "agent/$me/memory/entities/$who.md" "$who" "$*" <<'PY'
import sys, io, os, datetime
path, who, text = sys.argv[1], sys.argv[2], sys.argv[3]
today = datetime.date.today().isoformat()
if not os.path.exists(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    io.open(path, "w", encoding="utf-8", newline="\n").write(
        "# %s\nтип: неизвестно | первая запись: %s\n\n## События\n\n## Разбирали\n\n## Связи\n" % (who, today))
body = io.open(path, encoding="utf-8").read()
mark = "## События"
i = body.index(mark) + len(mark)
body = body[:i] + "\n- %s — %s" % (today, text) + body[i:]
io.open(path, "w", encoding="utf-8", newline="\n").write(body)
print("запомнено про %s" % who)
PY
