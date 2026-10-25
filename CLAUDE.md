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
GTD                                             # open the live app: https://home.<tailnet>.ts.net (GTD_URL to override)
GTD dev                                         # pull a fresh snapshot of live data into dev.db, run locally, open http://127.0.0.1:5005
GTD dev --keep                                  # same, reusing the existing dev.db
git push home main                              # deploy (after tests pass); also push origin
uv run app.py                                   # bare dev server (GTD_HOST / GTD_PORT to override)
uv run pytest                                   # all tests
uv run pytest tests/test_app.py::test_horizons  # one test
uv run pytest -k calendar                       # by keyword
uv run pytest -m browser                        # browser tests (real Chromium, ~5s); excluded from the default run
```

Env vars:
- `GTD_DB` points to the SQLite file. Default: `gtd.db` next to the code. On the VM it is `/var/lib/apps/gtd/gtd.db`; `GTD dev` sets it to `dev.db`. `gtd.db*` and `dev.db*` are gitignored (the repo is public).
- `GTD_SECRET` sets the Flask session key used for flash messages.
- `GTD_DEBUG=1` turns on Flask's debug mode and auto-reload.
- `GTD_URL` overrides the live URL `GTD` opens.

There is no build step, linter config or JS bundler.

`GTD dev` restarts a running local server when any `*.py`, template or static file is newer than `.server-started`, which it touches on every start. A server it didn't start counts as stale.

## Hosting and development

The app runs on `home`, a GCP free-tier e2-micro (us-east1) reached only over Tailscale; it holds the only live copy of the DB. VM-wide setup (systemd `app@.service`, deploy hook, nightly backups, port table) lives in `~/Github/personal/GCP-home-server`, not here; `deploy/gtd.env.example` is this app's `/etc/apps/gtd.env`. It runs as `gunicorn -w 1 --threads 4` (one worker keeps the timer and SQLite simple); `connect()` sets WAL and a 5 s busy timeout.
- Develop on the Mac only, with `GTD dev`. Never point a local server at the live DB. Don't develop on the VM (its 1 GB RAM is for the apps).
- Deploy with `git push home main` once tests pass. The hook backs up the DB (`/var/lib/apps/gtd/backups/gtd-predeploy-*.db`), checks out, `uv sync`s and restarts; a failed start prints the journal in the push output.
- A `_migrate()` change runs on the live DB as soon as it is deployed. Before pushing one, run `GTD dev` and check the migration on real data.
- Rollback: `git push -f home <good-sha>:main`; if a migration damaged data, also restore the pre-deploy backup.
- The VM's timezone is America/New_York (and `TZ` in the env file): `today()` and the timer's local times use the server's zone.
- Fallback to the old local + Dropbox setup: branch `pre-cloud-local`.

## Architecture

- **`db.py`** holds the schema and every query.
  - `connect()` runs `SCHEMA` (`CREATE ... IF NOT EXISTS`) on each connection, so schema changes must stay idempotent. Seed rows use `INSERT ... WHERE NOT EXISTS`.
  - `_migrate()` holds one-off schema changes for older databases, each detected from `sqlite_master`/`table_info` and a no-op once applied. SQLite can't alter a `CHECK`, so changing one needs a table rebuild, run with foreign keys off: dropping `projects` would null every task's `project_id`, and dropping `tasks` would cascade away the time entries.
  - Delete a migration once no database you might restore predates it, pre-deploy and nightly backups included (older ones were pruned on 2026-10-24). To restore an older backup, check out the code of its day first.
  - Writes go through `_insert`/`_update`, which whitelist columns (`TASK_FIELDS`, `PROJECT_FIELDS`, `HORIZON_FIELDS`) and raise on unknown fields.
  - Rows are `sqlite3.Row`.
- **`app.py`** holds the Flask routes.
  - Uses one connection per request (`g.db`).
  - `today()` reads `app.config["TODAY"]`, which lets tests pin the date. Never call `date.today()` directly.
  - A context processor injects `counts`, `today` and the running `timer` into every template.
  - `task_fields_from_form()` is the single place that turns form input into task fields; it validates the repeat rule.
- **`recur.py`** is pure date math.
  - Rules are `daily|weekly|monthly|yearly` or `every N day|week|month|year(s)`.
  - Both step from their anchor by `n*k`, so month-end days don't drift.
  - `next_due()` returns the first such date after today, stepping from the old due date.
  - A rule ending in `after done` or `after completion` (e.g. `every 2 weeks after done`, `weekly after done`) counts from the completion date: `next_due()` steps from today.
- **`dates.py`** is pure date parsing (`parse(text, today)`): ISO, `today|tomorrow|yesterday`, weekdays (always the next one after today), `next week|month`, `in N days|weeks|months`, `oct 30`.
  - `task_fields_from_form()` runs every `due_date` through it, so the task page, Process and the add lines take words. Their `date_input()` macro (`_task.html`) is a text box plus a 📅 button; the button opens a hidden native `<input type="date">` and the JS in `base.html` copies the pick into the text box. An unparseable date is flashed and ignored. The calendar's add form keeps its native date picker.
- **Done** (`/done`, `db.done_tasks()`) lists done projects, the latest 100 done tasks and the latest 100 abandoned tasks (`state="abandoned"`), newest first. Abandoned rows show ✕ and a Revive button.
  - A "Show completed" toggle (`?done=1`, `completed()` in `app.py`, `_done_toggle.html`) does the same on Next (non-H0 tasks), H0, and a project page (that project's steps); Projects lists done projects. Done rows show a ✓ and the completion date instead of a tick-off button, so a done repeating task can't be completed twice.
- **Calendar** (`/calendar`) shows a month (`?m=YYYY-MM`) or one week (`?w=<any date>`, Sunday-first; the Timer's week totals still start Monday); the Month/Week buttons switch. Each task shows once, on its due date: a repeat's future dates aren't projected. A bare `/calendar` (the nav link) opens the view last used, kept in the `cal_view` cookie, on today's month or week.
  - Each task has a colored left stripe and tint by kind (`k-next|h0|later|waiting|someday|inbox`; H0 wins, then an inbox item, then a someday project, then waiting task or project, else the status), with a dot legend below the grid.
  - The header's actions are `‹ Today ›` and a Month|Week toggle. Clicking a day number sets the add line's date and placeholder.
  - Dragging a task onto another day moves its due date (`/tasks/<id>/due`, 204 for fetch); the JS in `calendar.html` moves it in place, so the page doesn't reload. Only open tasks move.
  - The crawler follows calendar links only within `CALENDAR_MONTHS`.
- **Timestamps** (`created_at`, `completed_at`) are SQLite `datetime('now')`, i.e. UTC. Show their date with the `local_date` filter, never `[:10]`, or an evening's work shows as tomorrow.
- **Templates** extend `base.html`. Keep it lean and uniform: every page has the same layout, built from the same parts, so no page needs its own reading.
  - Nav: three groups split by a thin rule. Timer, Calendar (minute-by-minute work); Next, H0, Waiting, Inbox (task lists); Projects, Someday, Reference (lists you file into). Review, Horizons and Done (the weekly-review pages) sit muted on the right in `nav .aux`.
  - The top bar is identical on every page so switching tabs never moves anything: a round `+` (capture), search, ◐. Then the running timer, the capture `<dialog>`, and the page's JS (plus `static/notes.js`, below).
  - "+" and `/` capture: on the Inbox page they focus its own add line, elsewhere they open the dialog. `f` focuses search.
  - Single letters go to a page (`go_keys` in `base.html`: t Timer, c Calendar, n Next, h H0, w Waiting, i Inbox, p Projects, s Someday, r Reference, v Review, o Horizons, d Done), and `?` opens a help overlay listing them. None fire while typing in a field or while a dialog is open. Add a new shortcut to `go_keys` and the overlay follows.
  - **Page layout**, in this order: `<header class="page">` from `base.html` (optional `crumb` block for a link up to the parent, plain text with no ← since it isn't a back button, `heading` block defaulting to `<h1>` of the title, `actions` block for a few small controls), then the page's add line, then the list, then "Show completed". Record pages (task, project, horizon) leave `heading` empty: their big title input is the heading. `test_every_page_has_the_same_skeleton` (crawl) enforces it.
  - **Add line:** the `add_form()` macro (`_task.html`), one per page at most, on Inbox, Next, H0, Projects, project page, Waiting, Someday, Calendar and Timer. It shows only a title box; the fields in its `{% call %}` block (`.add-more`) and its ⏎ button appear, in CSS, once the box is focused or filled. Enter submits.
  - Buttons are ghost (no border, muted) by default; only `.primary` is filled. Prefer an icon with `title` and `aria-label` (✓ ▶ ■ ✕) over a word.
  - Small bars show proportions: project progress (done steps / all steps, `done_count` in `db.projects()`) and time per project on the Timer page.
  - Buttons inside a task row (▶, ■, Make next, Activate) are compact, so every row is the same height.
  - That JS submits `form.done` with the header `X-Requested-With: fetch`. The complete route returns `204` for that header, and the row fades out in place.
  - **No jumps.** An action stays on the page it was made on (`back()` in `app.py` redirects to the Referer). Only a link, "Save & exit", ✓→ (below), adding a reference item (it opens, for its details), or deleting the page's own record (a project, a horizon, an abandoned task) goes elsewhere. Don't add helpful redirects.
  - Search suggests as you type: the JS fetches `/search/suggest?q=` (JSON, `db.suggest()`), the 10 best fuzzy title matches among tasks (open, plus the newest 200 done), projects, horizons and reference; open items rank before done ones. ↑/↓ move, Enter or a click opens the highlighted one, Esc closes.
  - Fuzzy scoring is `fuzzy.score()` (pure): each query word must appear in the title as a substring (best, more so at a word start) or as letters in order; shorter titles win ties.
  - Enter with no suggestions submits to the full-text page (`/search?q=`, `db.search()`), a plain case-insensitive `LIKE`: every word must appear in a task's title/notes, or a project's, horizon's or reference item's title/notes. Done tasks are included, newest 50 only.
  - A ◐ button next to the search box toggles light/dark. The pick sets `data-theme` on `<html>`, is kept in `localStorage`, and overrides `prefers-color-scheme`; a tiny script in `<head>` applies it before first paint.
  - A project page's steps are reordered by dragging their ⠿ grip (`row(grip=True)`). On drop the JS posts the ids in order to `/projects/<id>/order`, which numbers `tasks.position` (`db.reorder_project()`). `project_tasks()` sorts the next action first, then by position; steps never dragged (`NULL`) go last, oldest first. The next action is pinned: its row has no grip (an empty `.grip.slot` keeps the column) and `data-pinned` stops the JS dropping anything above it. Dragging never changes a step. A step that becomes `next` (Make next, the step picker, adding one) takes position 0 and the rest shift down in order (`_one_next()`), so the old next action lands second.
  - Projects and Someday reorder their projects the same way (`projects.position`, `/projects/order/<status>`, `db.reorder_projects()`); undragged ones go last, by title, and a project changing status loses its position, so it lands at the bottom of its new list. The drag JS only moves the list's own rows, not the steps a ▸ folds out.
  - On Projects and Someday, each row's ▸ (`peek_button()`/`peek_steps()` in `_task.html`, `db.open_steps()`) folds out that project's open steps, read-only: a title opens its task page; there's no add line or tick-off there. "Expand all"/"Collapse all" in the header does every row. The JS in `base.html` toggles them.
  - A project page's add line takes step, due date, repeat rule and notes, so a step needs no trip to its task page.
  - On the task page, Save stays on the page (flashes "Saved.", carries where you came from as `?back=`); "Save & exit" (`exit=1`) returns there.
  - **Notes are Markdown** with live styling, like the Journal app's editor. `static/notes.js` hides every `textarea[name=notes]` (task, project, horizon, reference, Process) and puts a contenteditable `.md-editor` after it that writes its text back into the textarea, so forms submit as before. The rendered HTML's `textContent` must equal the raw text exactly (markup only wraps), because the caret is tracked as plain-text offsets; re-rendering wipes native undo, so it keeps its own. Markers (`**`, `` ` ``, `[](…)`) show only at the caret; links open with ⌘/Ctrl-click. Only an edit writes the textarea, so an untouched save changes nothing. The go-keys ignore it (`isContentEditable`).
  - Task rows come from the `row()` macro in `_task.html`. The project dropdown comes from its `project_select()` macro.
  - A row's date and ↻ slots are always rendered, empty when unused, so columns line up. On a project page every open step is labeled (next/waiting/later), and rows without "Make next" hold its width with a hidden `.make-next.slot`.
  - `project_select()` ends with a "＋ New…" option (`__new__`, `NEW` in `app.py`). Picking it reveals the `new_project` box in CSS; on its own it means no choice.
- **Reference** (`/reference`, table `reference`, `REFERENCE_FIELDS`) is one flat list of title + notes: information with no action, kept here so it needn't live in another app. No projects, tags or folders (yet); search finds it.
  - Adding an item opens its page, so its notes go in right away.
  - An item's page (`/reference/<id>`) lists the URLs in its notes as links above the textarea (the `links` filter in `app.py`: any `scheme://`, so `obsidian://` too). Deleting is a hard delete and returns to the list.
- **Timer** (`/timer`) replaced a standalone time tracker on 2026-10-22. Every time entry belongs to a task.
  - Table `time_entries` (`task_id` → `tasks`, `ON DELETE CASCADE`; `start_time`, `end_time` as UTC ISO-8601 to the second, written by `db.iso()`). No `end_time` means running. A partial unique index allows only one running entry.
  - `now()` in `app.py` reads `app.config["NOW"]` (pinned in tests), like `today()`. Never call `datetime.now()` in a route. Forms show and take local time (`local_input()`).
  - `db.timer_start()` stops whatever is running first. `complete_task()` stops the task's timer.
  - ▶ on every open task row (`row()` macro) and the task page starts it. The running timer shows as a pill pinned to the bottom of the window on every page (task link, clock, ■ stop), so it never scrolls away; its clock is the only timer JS (in `base.html`).
  - The Timer page's start box matches an open task by title (case-insensitive), or captures what you typed to the inbox and times that, so everything timed is in the system.
  - Editing an entry saves only fields that differ from what the form showed, so untouched times keep their seconds. Setting an end on the running entry stops it.
  - The history has no visible Save: the JS in `base.html` submits an entry when focus leaves its row with a time changed (moving between its start and end doesn't), or on Enter. An end before the start isn't sent; the row gets `.bad` until fixed. A hidden Save stays first in the form so Enter saves rather than pressing ▶.
  - Totals: time spent on the task page, the project total on the project page (done steps included), and per project this week on the Timer page (`db.week_totals()`).
- **`import_mindwtr.py`** was a one-off migration from Mindwtr's Local API, already applied on 2026-10-22.
  - `plan()` is a pure function and is tested.
  - `--apply` refuses to run if the DB already has tasks.
## GTD rules encoded in the code

- **A task has a state and, only inside a project, a step.** There is no task "status".
  - `tasks.state` is `open | done | abandoned`; every open-task query says `state = 'open'` and never looks at `step` for that.
  - `tasks.step` is `next | waiting | later`, and is `NULL` exactly when `project_id` is: H0 tasks and inbox items have none. `later` means a project step that isn't the next action yet. Beware SQL's `NULL`: `step != 'later'` skips every task without a step.
  - The schema enforces it: `CHECK ((step IS NULL) = (project_id IS NULL))` and no H0 task in a project. `_fit_step()` (run by `add_task`/`update_task`) drops a step given to a projectless task, and takes a task joining a project out of H0 and makes it a `later` step unless a step is given.
  - Writes reject unknown fields (`_insert` as well as `_update`), so a stray `status=` raises.
  - `_migrate()` split the old `status` column: done/abandoned became the state; a project task kept next/waiting/later as its step (inbox, someday → `later`); a task without a project got no step.
- **The inbox is not a step or a state.** It is every open task with neither a project nor H0, "ground level" (`tasks.ground`): `INBOX` in `db.py`, `db.inbox_tasks()`, and an `inbox` column on every `TASK_SELECT` row. Only H0 is a deliberate choice of no project; any other projectless task is an orphan, and orphans are the inbox.
  - The task page shows the step picker only for a project's open task. `orphaned()` in `app.py` flashes when a task lands in the inbox.
- **H0 tasks are always next actions**, so they have no step: the H0 tab lists every open H0 task.
  - H0 is the `h0` option in the `project_select()` macro, so choosing a project and choosing H0 are one decision. Its "＋ New…" option reveals a `new_project` box; `task_fields_from_form()` creates an active project from it and uses it, overriding the dropdown. A repeat rule doesn't exempt a task.
  - H0 tasks are listed on the Horizons page (`db.h0_actions()`), and their rows show an H0 tag. Their open tasks have their own H0 tab right of Next (`/h0`, `db.h0_actions()`, counted by `VISIBLE_H0`) and stay off Next Actions and its count (`VISIBLE_NEXT`). The H0 tab has its own add box (`/h0/add`: title, due date, repeat), so a ground-level task needs no Inbox or Process trip.
  - Process acts only on inbox items (a stale tab or resubmit is refused), and refuses next/waiting with neither, and waiting with H0. The task form and the Next, calendar and Waiting add lines send such a task to the inbox instead.
  - The Next page's add line (`/next/add`, `add_next_action()` in `app.py`, shared with the calendar) adds a next action directly: a `later` step if its project already has one.
  - The Waiting page's add box (`/waiting/add`: title, project (no H0 option), follow-up date) adds a `waiting` task; the Someday page's (`/someday/add`) adds a someday project.
- **Weekly review** (`/review`) is a checklist you finish.
  - Its steps are `REVIEW_STEPS` in `app.py`, one section each in `review.html`. A tick posts to `/review/<step>` (ticking again unticks) and is stored in `review_checks`, so a half-done review survives a closed tab. Ticked steps collapse to their heading.
  - Ticking the last step records the date in `reviews` (`db.complete_review()`), flashes "Weekly review completed on …" and clears the ticks. Past reviews are listed at the bottom, newest first.
  - It lists next actions to scan, leaving out any due today or later (overdue ones stay), so the review can be finished.
- **Project statuses:** `active | waiting | someday | done`. Someday and waiting exist only on projects, never on tasks.
  - A task page links up to its project (an H0 task has no such link) and can move the whole project to someday from there.
  - Process → "Someday / maybe" asks where the item goes: a new someday project, or a `later` step of an existing project. Nothing happens until you pick.
- **One next action per project.** `db.update_task`/`add_task` enforce it: when a project task becomes `next`, the project's other `next` tasks become `later`.
  - When a project's next action is finished, nothing is promoted automatically and nothing navigates: the project shows as stalled (⚠) until you pick one with "Make next" on its page.
  - ✓→ (on a project step's row outside its project page, and on its task page; `/tasks/<id>/done-then-project`, `complete_then_project()` in `app.py`) completes the step and opens its project, so you can pick the next one right away. It's the plain ✓ plus a deliberate jump; a task without a project just stays on the page.
  - Adding a step to a project that already has a next action makes it `later` by default, and so does processing an inbox item into such a project.
  - An active project with no `next` task is "stalled" (⚠).
  - Tasks of waiting and someday projects stay off Next Actions (`VISIBLE_NEXT`). Waiting projects are listed on the Waiting page.
- **No contexts.** They were removed on 2026-10-23: they were never used, and having to pick one slowed processing. Don't bring them back without being asked.
- **No areas on projects.** H2 areas exist only as notes on the Horizons page.
- **Next and H0 tabs hide nothing** (`db.next_list()`, `db.h0_actions()`): repeats not yet due are listed too. Both sort by due date (undated last) and stripe each row by how soon it's due (`row(soon=True)`, the `soon` filter and `SOON` in `app.py`: overdue, today, ≤7 days, ≤30 days, later), with a dot legend. A gap (CSS sibling rules in `style.css`) splits the rows into three bands: overdue + today, this week, everything later.
- **Recurring tasks** keep their step (or H0) but don't count until their due date: the nav counts, the weekly review and `db.next_actions()` use `VISIBLE_NEXT`/`VISIBLE_H0`.
  - `complete_task()` marks the task done and inserts the next instance.
  - A repeat rule without a due date gets today as its due date.
- **Projects can't be marked done while they have open tasks.** Moving a project out of done clears its `completed_at`.
- **Only an empty project can be deleted** (no tasks at all, done ones included). The project page shows the button only then.
- **Deleting a task abandons it** (`db.abandon_task()`: state `abandoned`, `completed_at` set, its timer stopped, its time kept). Process → trash does the same.
  - Revive (`/tasks/<id>/revive`, `db.revive_task()`) brings it back where it was: a later step of its project (reopening the project if it's done), an H0 task, or the inbox.
  - Deleting an abandoned task is a hard delete (its time too). So is `db.delete_task()`, which Process also uses when an item becomes a project (`into_new_project()` in `app.py`), unless the item has tracked time: then it stays as a `later` step of the new project.
  - Completing an abandoned task does nothing. Search and suggestions include abandoned tasks with the done ones.
- **Horizons** live in one table keyed by `level`:
  - `purpose` and `vision` are single seeded notes that can't be deleted.
  - `goal` and `area` are lists.

## Tests

Every change must be verified before it is called done:
1. Write or extend a test that fails without the change, and watch it fail first.
2. Run the whole suite (`uv run pytest`, about 2s).
3. For template or JS changes, also run `uv run pytest -m browser`, and add a browser test for new JS behavior. Click through the real app (`GTD`) for anything visual.

Enforcement:
- A Claude Code Stop hook (`.claude/settings.json` → `.claude/hooks/run-tests.sh`) won't let a turn end while tests fail. It also runs the browser tests when `templates/` or `static/` have uncommitted changes.
- `.githooks/pre-commit` refuses commits while tests fail. It also runs the browser tests when the commit touches `templates/` or `static/`. It is enabled per clone with `git config core.hooksPath .githooks`.

Layout (Flask test client, temporary DB):
- `tests/conftest.py` holds the fixtures:
  - `client` pins `TODAY = 2026-10-22` and makes Jinja undefined variables raise errors, so a template typo fails instead of rendering blank.
  - `conn` opens a second connection to the same file, so assertions can go straight to `db.*`.
  - `seed(c)` builds one of everything the UI can show (each task status, H0, repeats, waiting/someday/done projects, horizons). Its ids are deterministic.
  - New date-dependent behavior should be tested against the fixed date.
- `tests/test_app.py` holds the targeted tests: GTD rules, routes, recurrence, migrations, the import, edge cases.
- `tests/test_browser.py` (marker `browser`, Playwright + Chromium, a live server on a random port with the seeded DB) drives the JS: tick-off, search suggestions, theme toggle, capture dialog, dragging project steps and projects, the project rows' ▸ and Expand all, `/` and `f` keys, date picker, timer clock, timer entry autosave, the Markdown notes editor (rendering, saving, undo), the add line and "＋ New…" reveals, calendar day click, and fails on any JS error. One-time setup: `uv run playwright install chromium`.
- `tests/test_crawl.py` holds the generic tests. They usually catch accidental bugs before anyone writes a targeted test for them:
  - Every link reachable from `/` must load.
  - Every form, with each submit button and each `<select>` option, must submit and land on a 200 page. Each submission gets a fresh copy of the seeded DB and sends its own page as the Referer.
  - Saving any edit page without changing anything must leave the record unchanged.
  - Task rows must match what the tick-off script expects.
  - When you add a page, field, state or step, add it to `seed()` so the crawler reaches it.
