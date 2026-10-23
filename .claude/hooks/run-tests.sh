#!/bin/bash
# Stop hook: Claude can't finish a turn while the test suite is red.
# On a second failure in a row (stop_hook_active) it lets Claude stop, so it can report instead of looping.
cd "$(dirname "$0")/../.." || exit 0
active=$(jq -r '.stop_hook_active // false' 2>/dev/null)
out=$(uv run pytest -q -x 2>&1) || fail=1
# Browser tests are slow: only when templates or static files (where the JS lives) have uncommitted changes.
if [[ -z $fail && -n $(git status --porcelain -- templates static) ]]; then
  out=$(uv run pytest -q -x -m browser 2>&1) || fail=1
fi
[[ -z $fail ]] && exit 0
[[ $active == true ]] && { echo "Tests are still failing — tell the user." >&2; exit 0; }
printf 'Tests fail. Fix them (or tell the user why not) before finishing:\n%s\n' "$(tail -40 <<<"$out")" >&2
exit 2
