#!/bin/zsh
# Start the GTD app if it isn't running, then open it in the browser.
# Installed as a global command with: ln -s "$PWD/gtd.sh" ~/.local/bin/GTD
set -e
dir=${0:A:h}
url=http://127.0.0.1:${GTD_PORT:-5005}
if ! curl -sf -o /dev/null "$url/inbox"; then
  cd "$dir"
  nohup uv run app.py >> server.log 2>&1 &
  for _ in {1..50}; do curl -sf -o /dev/null "$url/inbox" && break; sleep 0.2; done
fi
open "$url"
