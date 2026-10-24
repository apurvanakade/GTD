#!/usr/bin/env bash
# GTD              open the live app on the `home` VM (over Tailscale).
# GTD dev          pull a fresh snapshot of the live data into dev.db, run the app locally on it, and open it.
# GTD dev --keep   the same, reusing the existing dev.db (offline, or to keep local test data).
# The local server never touches the live DB; anything done in dev is thrown away.
# Installed as a global command with: ln -s "$PWD/gtd.sh" ~/.local/bin/GTD
set -eo pipefail

open_url() {
  if command -v open >/dev/null 2>&1; then
    open "$1"                          # macOS
  else
    xdg-open "$1" >/dev/null 2>&1 &    # Linux
  fi
}

if [[ $1 != dev ]]; then
  # GTD_URL overrides; otherwise https://home.<tailnet>.ts.net from Tailscale's MagicDNS suffix.
  url=${GTD_URL:-}
  if [[ -z $url ]]; then
    ts=$(command -v tailscale || echo /Applications/Tailscale.app/Contents/MacOS/Tailscale)  # the Mac app has no CLI on PATH
    suffix=$("$ts" status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["MagicDNSSuffix"])' 2>/dev/null) || true
    [[ -n $suffix ]] || { echo "GTD: Tailscale isn't running; start it or set GTD_URL." >&2; exit 1; }
    url=https://home.$suffix
  fi
  open_url "$url"
  exit
fi

# Resolve the real path of this script, following symlinks (the GTD command is one).
src=${BASH_SOURCE[0]}
while [ -h "$src" ]; do
  d=$(cd -P "$(dirname "$src")" && pwd)
  src=$(readlink "$src")
  [[ $src != /* ]] && src=$d/$src
done
dir=$(cd -P "$(dirname "$src")" && pwd)
port=${GTD_PORT:-5005}
url=http://127.0.0.1:$port
stamp=$dir/.server-started  # touched when this script starts the server
dev_db=$dir/dev.db

running() { curl -sf -o /dev/null "$url/inbox"; }
stop() {
  kill $(lsof -ti tcp:$port -sTCP:LISTEN) 2>/dev/null || true
  for _ in {1..25}; do running || break; sleep 0.2; done
}

if [[ ! -e $dev_db || $2 != --keep ]]; then
  # .dump gives a consistent copy even while the live app is writing.
  echo "GTD: pulling a snapshot of the live data into dev.db"
  running && stop
  tmp=$(mktemp "$dir/dev.db.XXXXXX")
  rm -f "$tmp"
  ssh home sqlite3 /var/lib/apps/gtd/gtd.db .dump | sqlite3 "$tmp"
  rm -f "$dev_db" "$dev_db-wal" "$dev_db-shm"
  mv "$tmp" "$dev_db"
fi

# Stale if the code changed since the server started (or we didn't start it).
stale() {
  [[ ! -e $stamp ]] || [[ -n $(find "$dir"/*.py "$dir"/templates "$dir"/static -newer "$stamp" -print -quit) ]]
}

running && stale && stop

if ! running; then
  cd "$dir"
  touch "$stamp"
  GTD_DB=$dev_db nohup uv run app.py >> server.log 2>&1 &
  for _ in {1..50}; do running && break; sleep 0.2; done
fi
open_url "$url"
