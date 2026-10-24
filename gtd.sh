#!/usr/bin/env bash
# Start the GTD app if it isn't running (or is running code older than what's on disk),
# then open it in the browser. Works on macOS and Linux.
# Installed as a global command with: ln -s "$PWD/gtd.sh" ~/.local/bin/GTD
set -e

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

# Stale if the code changed since the server started (or we didn't start it).
stale() {
  [[ ! -e $stamp ]] || [[ -n $(find "$dir"/*.py "$dir"/templates "$dir"/static -newer "$stamp" -print -quit) ]]
}

if curl -sf -o /dev/null "$url/inbox" && stale; then
  kill $(lsof -ti tcp:$port -sTCP:LISTEN) 2>/dev/null || true
  for _ in {1..25}; do curl -sf -o /dev/null "$url/inbox" || break; sleep 0.2; done
fi

if ! curl -sf -o /dev/null "$url/inbox"; then
  cd "$dir"
  touch "$stamp"
  nohup uv run app.py >> server.log 2>&1 &
  for _ in {1..50}; do curl -sf -o /dev/null "$url/inbox" && break; sleep 0.2; done
fi
if command -v open >/dev/null 2>&1; then
  open "$url"               # macOS
else
  xdg-open "$url" >/dev/null 2>&1 &   # Linux
fi
