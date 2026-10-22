# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A small, single-user GTD web app (Flask + Jinja + stdlib `sqlite3`). It replaced Mindwtr, which had
grown too complicated. Keep it simple: prefer the plain GTD workflow over options and settings.

**Keep this file current.** Update it in the same change whenever any of these change:
- a command or environment variable
- the architecture
- a GTD rule built into the code
- the test setup

Delete anything here that has become wrong.

## Commands

```bash
GTD                                             # global command (~/.local/bin/GTD -> gtd.sh): (re)start server, open browser
uv run app.py                                   # http://127.0.0.1:5005 (GTD_HOST / GTD_PORT to override)
uv run pytest                                   # all tests
uv run pytest tests/test_app.py::test_horizons  # one test
uv run pytest -k calendar                       # by keyword
```

Env vars:
- `GTD_DB` points to a different SQLite file. The default is `./gtd.db`, which holds the user's real data and is gitignored.
- `GTD_SECRET` sets the Flask session key used for flash messages.
- `GTD_DEBUG=1` turns on Flask's debug mode and auto-reload.

There is no build step, linter config or JS bundler.

`GTD` restarts a running server when any `*.py`, template or static file is newer than `.server-started`, which it touches on every start. A server it didn't start counts as stale. Without this, a long-running server would serve new templates with old Python code.

## Architecture

- **`db.py`** holds the schema and every query.
  - `connect()` runs `SCHEMA` (`CREATE ... IF NOT EXISTS`) on each connection, so schema changes must stay idempotent. Seed rows use `INSERT ... WHERE NOT EXISTS`.
  - SQLite can't alter a `CHECK`, so a status change needs a table rebuild in `_migrate()`. It detects the old schema from `sqlite_master` and runs with foreign keys off; otherwise dropping `projects` would null every task's `project_id`.
  - Writes go through `_insert`/`_update`, which whitelist columns (`TASK_FIELDS`, `PROJECT_FIELDS`, `HORIZON_FIELDS`) and raise on unknown fields.
  - Rows are `sqlite3.Row`.
- **`app.py`** holds the Flask routes.
  - Uses one connection per request (`g.db`).
  - `today()` reads `app.config["TODAY"]`, which lets tests pin the date. Never call `date.today()` directly.
  - A context processor injects `counts`, `CONTEXTS` (from the DB) and `today` into every template.
  - `task_fields_from_form()` is the single place that turns form input into task fields; it validates the repeat rule.
- **`recur.py`** is pure date math.
  - Rules are `daily|weekly|monthly|yearly` or `every N day|week|month|year(s)`.
  - `next_due()` steps forward from the old due date until it is after today.
  - `occurrences()` steps from the original anchor by `n*k`, so month-end days don't drift. Use it for projections such as the calendar.
- **Templates** extend `base.html`, which holds the nav, capture (an inline box on the Inbox page only; elsewhere a "+ Inbox" button opens a `<dialog>`; `/` does either), the search box (`f` focuses it) and the only JS on the page.
  - That JS submits `form.done` with the header `X-Requested-With: fetch`. The complete route returns `204` for that header, and the row fades out in place. If the response was redirected, the JS navigates there instead.
  - Search (`/search?q=`, `db.search()`) is a plain case-insensitive `LIKE`: every word must appear in a task's title/notes/context/waiting-on, or a project's or horizon's title/notes. Done tasks are included, newest 50 only.
  - Task rows come from the `row()` macro in `_task.html`. The context dropdown comes from its `context_select()` macro.
- **`import_mindwtr.py`** was a one-off migration from Mindwtr's Local API, already applied on 2026-10-22.
  - `plan()` is a pure function and is tested.
  - `--apply` refuses to run if the DB already has tasks.
## GTD rules encoded in the code

- **Task statuses:** `inbox | next | waiting | later | done`.
  - `later` means a project step that isn't the next action yet. It needs a project; the task form sends a `later` task with no project back to the inbox.
- **Project statuses:** `active | waiting | someday | done`. Someday and waiting exist only on projects, never on tasks.
  - A task page links up to its project and can move the whole project to someday from there.
  - Process → "Someday / maybe" turns the item into a new someday project.
- **One next action per project.** `db.update_task`/`add_task` enforce it: when a project task becomes `next`, the project's other `next` tasks become `later`.
  - When a project's next action is finished, the complete route redirects to the project page so you can choose which `later` task becomes next ("Make next"). Nothing is promoted automatically.
  - Adding a step to a project that already has a next action makes it `later` by default, and so does processing an inbox item into such a project.
  - An active project with no `next` task is "stalled" (⚠).
  - Tasks of waiting and someday projects stay off Next Actions (`VISIBLE_NEXT`). Waiting projects are listed on the Waiting page.
- **One context per task.** Contexts live in the `contexts` table (managed at the bottom of the Horizons page), not in code, because the repo is public.
  - Any context used by an open task is re-added on every `connect()`, so the list and the tasks can't disagree.
  - A context still used by open tasks can't be removed.
  - The Process flow requires a context for next actions.
- **No areas on projects.** H2 areas exist only as notes on the Horizons page.
- **Recurring tasks** stay `next` but are hidden until their due date (`VISIBLE_NEXT` in `db.py`).
  - `complete_task()` marks the task done and inserts the next instance.
  - A repeat rule without a due date gets today as its due date.
- **Projects can't be marked done while they have open tasks.**
- **Deleting a task is a hard delete.**
- **Horizons** live in one table keyed by `level`:
  - `purpose` and `vision` are single seeded notes that can't be deleted.
  - `goal` and `area` are lists.

## Tests

Tests are in `tests/test_app.py` and use the Flask test client with a temporary DB.
- The `client` fixture pins `TODAY = 2026-10-22`.
- The `conn` fixture opens a second connection to the same file, so assertions can go straight to `db.*`.
- New date-dependent behavior should be tested against that fixed date.
