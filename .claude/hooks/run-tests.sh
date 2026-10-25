#!/bin/bash
# Stop hook: Claude can't finish a turn while the test suite is red.
# On a second failure in a row (stop_hook_active) it lets Claude stop, so it can report instead of looping.
# A green run records a fingerprint of the working tree; a turn that changed nothing since skips the tests.
cd "$(dirname "$0")/../.." || exit 0
active=$(jq -r '.stop_hook_active // false' 2>/dev/null)
stamp=$(git rev-parse --git-path tests-green)
tree=$({ git rev-parse HEAD; git diff HEAD; git ls-files -o --exclude-standard -z | xargs -0 cat 2>/dev/null; } | shasum)
# Browser tests are slow: only when templates or static files (where the JS lives) have uncommitted changes.
browser=$([[ -n $(git status --porcelain -- templates static) ]] && echo 1)
want="$tree ${browser:-0}"
[[ -f $stamp && $(<"$stamp") == "$want" ]] && exit 0
out=$(uv run pytest -q -x 2>&1) || fail=1
if [[ -z $fail && -n $browser ]]; then
  out=$(uv run pytest -q -x -m browser 2>&1) || fail=1
fi
[[ -z $fail ]] && { echo "$want" >"$stamp"; exit 0; }
[[ $active == true ]] && { echo "Tests are still failing — tell the user." >&2; exit 0; }
printf 'Tests fail. Fix them (or tell the user why not) before finishing:\n%s\n' "$(tail -40 <<<"$out")" >&2
exit 2
