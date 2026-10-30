# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

A small, single-user GTD web app (Flask + Jinja + stdlib `sqlite3`). It replaced Mindwtr, which had
grown too complicated. Keep it simple: prefer the plain GTD workflow over options and settings.
The phone is only for capture; everything else is done on a desktop, so features need no touch or phone adaptation.

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
uv run pytest -m browser                        # browser tests (real Chromium, ~8s); excluded from the default run
```

Env vars:
- `GTD_DB` points to the SQLite file. Default: `gtd.db` next to the code. On the VM it is `/var/lib/apps/gtd/gtd.db`; `GTD dev` sets it to `dev.db`. `gtd.db*` and `dev.db*` are gitignored (the repo is public).
- `GTD_SECRET` sets the Flask session key used for flash messages.
- `GTD_DEBUG=1` turns on Flask's debug mode and auto-reload.
- `GTD_URL` overrides the live URL `GTD` opens. On the VM it is also the address a reminder's tap opens (`Click`); unset, a reminder has no link.
- `NTFY_URL` is the ntfy topic reminders are POSTed to (on the VM `http://127.0.0.1:5007/gtd`). Unset (dev, tests): no reminder thread, nothing is sent.

There is no build step, linter config or JS bundler.

`GTD dev` restarts a running local server when any `*.py`, template or static file is newer than `.server-started`, which it touches on every start. A server it didn't start counts as stale.

## Hosting and development

The app runs on `home`, a GCP free-tier e2-micro (us-east1) reached only over Tailscale; it holds the only live copy of the DB. VM-wide setup (systemd `app@.service`, deploy hook, nightly backups, port table) lives in `~/Github/personal/GCP-home-server`, not here; `deploy/gtd.env.example` is this app's `/etc/apps/gtd.env`. It runs as `gunicorn -w 1 --threads 4` (one worker keeps the timer and SQLite simple); `connect()` sets WAL and a 5 s busy timeout.
- Develop on the Mac only, with `GTD dev`. Never point a local server at the live DB. Don't develop on the VM (its 1 GB RAM is for the apps).
- Deploy with `git push home main` once tests pass. The hook backs up the DB (`/var/lib/apps/gtd/backups/gtd-predeploy-*.db`), checks out, `uv sync`s and restarts; a failed start prints the journal in the push output.
- There are no migrations: `SCHEMA` (`CREATE ... IF NOT EXISTS`) only adds what's missing. A change it can't make (a new column on an existing table, a changed `CHECK`) needs a one-off step: run it by hand on the VM (after a backup), check it first on `GTD dev`, and don't keep it in the code.
- Rollback: `git push -f home <good-sha>:main`; if a schema change damaged data, also restore the pre-deploy backup. A backup from before a schema change needs the code of its day.
- The VM's timezone is America/New_York (and `TZ` in the env file): `today()` and the timer's local times use the server's zone.

## Architecture

- **`db.py`** holds the schema and every query.
  - `connect()` runs `SCHEMA` (`CREATE ... IF NOT EXISTS`) once per file: it then stores `SCHEMA_VERSION` (a CRC of `SCHEMA`) in `PRAGMA user_version` and later connections skip it until `SCHEMA` changes. So schema changes must stay idempotent. Seed rows use `INSERT ... WHERE NOT EXISTS`.
  - SQLite can't alter a `CHECK`, so changing one needs a table rebuild, run with foreign keys off: dropping `projects` would null every task's `project_id`, and dropping `tasks` would cascade away the time entries.
  - The live DB still has an unused `projects.updated_at` column (dropped from `SCHEMA` on 2026-10-30); harmless.
  - **Transactions:** every public write function is `@atomic` (`transaction()`): its writes commit together or not at all, and nested calls join the outermost one, which alone commits. Helpers never call `commit()`. A route that does several writes as one action wraps them in `with db.transaction(c):` (abandoning a project with its area change, a task moved to Reference).
  - Writes go through `_insert`/`_update`, which whitelist columns (`TASK_FIELDS`, `PROJECT_FIELDS`, `HORIZON_FIELDS`) and raise on unknown fields.
  - Rows are `sqlite3.Row`.
- **`app.py`** holds the Flask routes.
  - No login, so `same_origin_only()` refuses a POST whose `Sec-Fetch-Site` is anything but `same-origin` or `none` (CSRF from another site's page). Clients that aren't browsers (ntfy's buttons) don't send the header and pass.
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
  - `task_fields_from_form()` runs every `due_date` through it, so the task page and the add lines take words. Their `date_input()` macro (`_task.html`) is a text box plus a 📅 button; the button opens a hidden native `<input type="date">` and the JS in `static/app.js` copies the pick into the text box (and fires `change`, so a task page saves it; while the picker is open the box is `.picking` and waits). The task page's Remind box uses it too, with `type='datetime-local'` (date and time; the pick lands as `YYYY-MM-DD HH:MM` and updates the preview). An unparseable date is flashed and ignored. The calendar's add form keeps its native date picker.
  - **Live previews:** on the task page, a `.hint.when` beside the Due, Repeats and Remind boxes shows how each reads as you type (`→ Fri Oct 23`, `→ every 2 weeks · next Fri Nov 6`, `not understood`), from `/parse/<kind>` (`due|recur|remind`, `parse_preview()` in `app.py`; one loop in `static/app.js`). The Repeats hint gives the rule in words (`describe_recur()`) and the date completing it would set (`recur.next_due()` from the Due box, or today; an `after done` rule adds "if done today"), and follows edits to the Due box too. Due and Repeats show their saved value's reading on load.
- **New items go to the top** of every list they arrive in: Inbox and Waiting list newest first, Next/H0 and calendar days break due-date ties newest first, and project steps and projects get a top position (above). Reference and horizons stay alphabetical.
- **Done** (`/done`, `db.done_tasks()`) lists archived areas (if any), done projects, the latest 100 done tasks, and under Abandoned the latest 100 abandoned projects (`db.abandoned_projects()`) and tasks (`state="abandoned"`), newest first. An abandoned project's steps aren't listed one by one there; its page lists them.
  - A project page lists its abandoned steps under Abandoned, below Completed and with it (`completed(abandoned=True)`, `_done_toggle.html`), each with Revive. Abandoned rows show ✕ and a Revive button. There is no separate archive tab.
  - A "Show completed" toggle (`?done=1`, `completed()` in `app.py`, `_done_toggle.html`) does the same on Next (non-H0 tasks), H0, and a project page (that project's steps); hidden by default everywhere; Projects lists done projects. Done rows show a ✓ and the completion date instead of a tick-off button, so a done repeating task can't be completed twice.
- **Calendar** (`/calendar`) shows a month (`?m=YYYY-MM`) or one week (`?w=<any date>`, Sunday-first; the Timer's week totals still start Monday); the Month/Week buttons switch. Each task shows once, on its due date: a repeat's future dates aren't projected. A bare `/calendar` (the nav link) opens the view last used, kept in the `cal_view` cookie, on today's month or week.
  - Each task has a colored left stripe and tint by kind (`k-next|h0|later|waiting|someday|inbox`; H0 wins, then an inbox item, then a someday project, then waiting task or project, else the status), with a dot legend below the grid.
  - The header's actions are `‹ Today ›` and a Month|Week toggle. Clicking a day number sets the add line's date and placeholder.
  - Dragging a task onto another day moves its due date (`/tasks/<id>/due`, 204 for fetch); the JS in `calendar.html` moves it in place, so the page doesn't reload. Only open tasks move.
  - The crawler follows calendar links only within `CALENDAR_MONTHS`.
- **Timestamps** (`created_at`, `completed_at`) are SQLite `datetime('now')`, i.e. UTC. Show their date with the `local_date` filter, never `[:10]`, or an evening's work shows as tomorrow.
- **Templates** extend `base.html`. Page JS lives in `static/app.js` (loaded at the end of every page, after the page's own inline script; only `GO` is inline), plus `notes.js`, `autosave.js` and `move.js`. Keep it lean and uniform: every page has the same layout, built from the same parts, so no page needs its own reading.
  - Nav counts are dots, not digits (the `badge()` macro in `_nav.html`): sized by the count (1–2, 3–5, 6+), red when Next or H0 has an overdue task (`next_overdue`, `h0_overdue` in `db.counts()`), amber for stalled projects; the number is the tooltip, and a zero shows nothing.
  - Nav: three groups split by a thin rule. Timer, Calendar (minute-by-minute work); Next, H0, Waiting, Inbox (task lists); Projects, Someday, Reference (lists you file into). Review, Horizons and Done (the weekly-review pages) sit muted on the right in `nav .aux`.
  - The top bar is identical on every page so switching tabs never moves anything: a round `+` (capture), search, ◐. Then the running timer, the capture `<dialog>`, and the page's JS (plus `static/notes.js`, below).
  - "+" and `/` capture: on the Inbox page they focus its own add line, elsewhere they open the dialog. `f` focuses search.
  - Single letters go to a page (`go_keys` in `base.html`, which sets `GO` inline for `static/app.js`: t Timer, c Calendar, n Next, h H0, w Waiting, i Inbox, p Projects, s Someday, r Reference, v Review, o Horizons, d Done), and `?` opens a help overlay listing them. None of them, nor `/` and `f`, fire while typing in a field or while a dialog is open. Add a new shortcut to `go_keys` and the overlay follows.
  - **Page layout**, in this order: `<header class="page">` from `base.html` (optional `crumb` block for a link up to the parent, plain text with no ← since it isn't a back button, `heading` block defaulting to `<h1>` of the title, `actions` block for a few small controls), then the page's add line, then the list, then "Show completed". Record pages (task, project, horizon, reference) leave `heading` empty: their big title input is the heading, on a `.title-line` with their few controls as quiet selects and icons (no visible Save; a `<button hidden>Save</button>` stays first so Enter saves). Only horizon and reference pages keep a crumb. `test_every_page_has_the_same_skeleton` (crawl) enforces it.
  - **Lean pages.** No explanatory paragraphs: a hint is a few words at most, a state shows as ⚠ with a tooltip. Prefer an icon to a word, a select that saves on pick to a row of buttons.
  - **The nav autohides** (`static/app.js`, `body.nav-hidden`): it slides away while scrolling down and returns on scrolling up, at the top, or with the mouse at the top edge. Scroll-driven, so it works with touch (iPad).
  - **Record pages open centered:** `static/app.js` pads `main` so that, scrolled past the nav and search bar (the nav then hides), the body (title to the end of the page) sits centered in the window, or starts near the top when taller. Only a fresh load scrolls (not reload or Back). It measures with 1px of padding already set, or the header's margins collapse through `main` and the numbers are off.
  - **Flash messages** are a toast (`.flashes`, before `<main>`) that fades after 5 s; background saves show theirs the same way (`toast()` in `static/app.js`).
  - **Add line:** the `add_form()` macro (`_task.html`), one per page at most, on Inbox, Next, H0, Projects, project page, Waiting, Someday, Calendar, Timer, Reference and Horizons' goal and area cards (a hidden `level`). It shows only a title box; the fields in its `{% call %}` block (`.add-more`) and its ⏎ button appear, in CSS, once the box is focused or filled. Enter submits.
  - **Title marks:** a new task's title (task add lines and capture, `task_fields_from_form()` without a `current` task; not Timer, not the task page) can end with `>fri` (due), `*weekly` (repeat) and `!3pm` (reminder), in any order (`split_title()`, `TITLE_MARKS` in `app.py`). Each needs a space before it and none after, so `Yes! do it` and `a > b` stay titles. They're peeled off the end; the first unreadable one is flashed and it and everything before it stay in the title, so nothing is lost. A mark wins over the line's own box. The `title_marks()` hint (`_task.html`) sits in those add lines' `.add-more` (`add_form(marks=True)`) and the capture dialog; `?` lists them too. Capture's flash echoes what was set.
  - An action list with nothing left (Inbox, Next, H0, Waiting) shows one big faint ✓ (`.empty.clear`); its sentence stays in the HTML at `font-size: 0`. Other empty states (no matches, no reviews yet) keep their sentence.
  - Buttons are ghost (no border, muted) by default; only `.primary` is filled. Prefer an icon with `title` and `aria-label` (✓ ▶ ■ ✕) over a word.
  - The Timer page shows Today and This week side by side (`tally()`, below; no table). A `title` gives the exact time.
  - Projects and Someday list their projects in one column, grouped by area (`area_groups()` in `_task.html`, `db.by_area()`): a `section.area-group` per area that has any, its `h2.area-head` linking to its page; each group is its own `ul[data-reorder]`; dragging reorders within an area or moves a project to another (below). There the cards are tight plain rows, no pill box (`.area-groups`). (Side-by-side area columns were tried on 2026-10-29 and dropped: they didn't fit the rest of the app.) The card is the `project_card()` macro (`_task.html`), shared with an area's page: grip, ▸, title (⚠ and amber when stalled). Nothing else: no progress fill, step count or idle fade (removed 2026-10-29).
  - **No colored stripes outside the calendar.** They were removed on 2026-10-27: Next/H0 striped by due date and project pages by step, so a color meant different things on different pages. The calendar keeps its kind colors and legend. Only an overdue date is red; a date due today is plain (amber means stalled).
  - Task rows have no divider lines: spacing and a rounded hover tint set them apart. Every list of projects (Projects, Someday, Waiting's projects, Horizons' H1 card, Done) is `ul.tasks.cards`, a rounded pill per project (except on Projects and Someday, above) with a small gap, padded 1px less so the border keeps rows one height.
  - Buttons inside a task row (▶, ■, Make next, and `.row-btn`: Revive, Activate) are compact, so every row is the same height.
  - That JS submits `form.done` with the header `X-Requested-With: fetch`. The complete route returns `204` for that header, and the row fades out in place; its band's count drops and the nav is refetched (`refreshNav()`, from `/nav`, which renders `_nav.html` alone; the current tab keeps its highlight), so its dots stay right.
  - Every background save (tick-off, reorder, calendar drag, moving a task by drag) goes through `post()` in `static/app.js`: on a failed response or network error it reloads the page, so the screen never shows a change that wasn't saved.
  - A click anywhere on a task or project row opens it, as its title link would (`static/app.js`); its buttons, links, fields, grip and folded-out steps keep their own jobs, and ⌘/Ctrl-click opens a new tab.
  - **No jumps.** An action stays on the page it was made on (`back()` in `app.py` redirects to the Referer). Only a link, ✓→ (below), adding a reference item (it opens, for its details), or deleting the page's own record (a project, a horizon, an abandoned task) goes elsewhere. Don't add helpful redirects.
  - Search suggests as you type: the JS fetches `/search/suggest?q=` (JSON, `db.suggest()`), the 10 best fuzzy title matches among tasks (done and abandoned too), projects, horizons and reference. Titles containing every query word (`fuzzy.exact()`) rank first, so scattered-letter matches can't crowd them out; then open items before done ones. ↑/↓ move, Enter or a click opens the highlighted one, Esc closes.
  - Fuzzy scoring is `fuzzy.score()` (pure): each query word must appear in the title as a substring (best, more so at a word start) or as letters in order; shorter titles win ties.
  - Enter with no suggestions submits to the full-text page (`/search?q=`, `db.search()`), a plain case-insensitive `LIKE`: every word must appear in a task's title/notes, or a project's, horizon's or reference item's title/notes. Done tasks are included, newest 50 only.
  - Colors are tokens on `:root`, each `light-dark(light, dark)`; `color-scheme` picks the side. Don't define a token twice.
  - A ◐ button next to the search box toggles light/dark. The pick sets `data-theme` on `<html>`, is kept in `localStorage`, and overrides `prefers-color-scheme`; a tiny script in `<head>` applies it before first paint.
  - A project page's steps are reordered by dragging their ⠿ grip (`row(grip=True)`). On drop the JS posts the ids in order to `/projects/<id>/order`, which numbers `tasks.position` (`db.reorder_project()`). `project_tasks()` sorts the next action first, then by position; a new or filed step gets a position above the rest (`_top()` in `db.py`). The next action is pinned: `data-pinned` stops the JS dropping anything above it or moving it down; its grip only drags it out to another project (below). Reordering never changes a step. A step that becomes `next` (Make next, the step picker, adding one) takes position 0 and the rest shift down in order (`_one_next()`), so the old next action lands second.
  - Projects and Someday reorder their projects the same way (`projects.position`, `/projects/order/<status>`, `db.reorder_projects()`); a new project, and one changing status (or reopened by an open task), goes to the top of its list. Every step and every project but an abandoned one has a position (backfilled on the live DB on 2026-10-30), so lists sort by it alone. The drag JS only moves the list's own rows, not the steps a ▸ folds out.
  - On Projects and Someday, each row's ▸ (`peek_button()`/`peek_steps()` in `_task.html`, `db.open_steps()`) folds out that project's open steps, read-only: a title opens its task page; there's no add line or tick-off there. "Expand all"/"Collapse all" in the header does every row. `static/app.js` toggles them.
  - A project page's add line takes step, due date, repeat rule and notes, so a step needs no trip to its task page.
  - **Autosave:** a record page's (task, project, horizon, reference) title and notes save themselves (`static/autosave.js`, `form[data-autosave]`, `/autosave/<table>/<id>`, `db.save_text()`): 2 s after typing stops, on leaving a field, and on hiding or leaving the page. The other fields save as described per page. Each save sends only changed fields with the text the page last saw (`base_<field>`); a field changed elsewhere since (now neither base nor the new text, compared trimmed) gets a 409 with the current text, and the `.autosave` line under the notes offers load theirs / keep mine / merge (notes get git-style conflict markers, the title stays mine). Focusing the tab refetches: untouched fields update, touched ones ask. A blank title isn't saved. No schema change: no version column, the base text is the version.
    - Autosave alone saves title and notes: `task_view()` and `project_view()` ignore them, so a select posting a stale form can't overwrite edits made elsewhere. Any other submit of the form (a select, ✓, ▶) waits for autosave to save first, and is dropped on a conflict; Enter in the title (the hidden Save) only saves.
  - **The task page** (`form[data-save-on-change]`): the title line is title, ▶/■ and time spent, ✓, ✓→ (a project step), ✕ (abandon; an abandoned task gets ↺ revive and ✕ delete for good, which goes to Done). Below the notes, two rows: Project (with ↗ to open it) and Step; then Due, Repeats and Remind. Each field saves once changed (`static/app.js`): a select (or a new project's name) submits the form and reloads, since the page depends on it; a text box (due, repeats, remind; on leaving it or Enter) posts due, repeats and remind in the background (`task_view()` answers a fetch with `{"flashes": […]}`), so moving between boxes never reloads. "＋ New project…" waits for its name.
  - **Notes are Markdown** with live styling, like the Journal app's editor. `static/notes.js` hides every `textarea[name=notes]` (task, project, horizon, reference) and puts a contenteditable `.md-editor` after it that writes its text back into the textarea, so forms submit as before. The rendered HTML's `textContent` must equal the raw text exactly (markup only wraps), because the caret is tracked as plain-text offsets; re-rendering wipes native undo, so it keeps its own. Markers (`**`, `` ` ``, `[](…)`) show only at the caret; links open with ⌘/Ctrl-click. Only an edit writes the textarea, so an untouched save changes nothing. The go-keys ignore it (`isContentEditable`).
  - Task rows come from the `row()` macro in `_task.html`.
  - **One project list.** The project dropdown (`project_select()`) and Inbox's drop panel (`project_targets()`), both in `_task.html`, show the same list in the same order: H0, then one group per open area (alphabetical, empty ones too), then Done (`db.project_groups()`: `(label, projects, area id)`; `<optgroup>`s in the dropdown, `h3` headings in the panel). An area's group starts with its own "＋ New project…" (`__new__:<area id>` in the dropdown, `data-area` in the panel, which posts `area_id`), so a new project always gets an area; then its active, waiting (`· waiting`) and someday projects, each in its list's drag order. Routes pass `project_groups`; the macros read it from the context.
  - A row's date and ↻ slots are always rendered, empty when unused, so columns line up. On a project page every open step is labeled (next/waiting/later), and rows without "Make next" hold its width with a hidden `.make-next.slot`. Waiting and later steps both get Make next. There (`.tasks.by-step`) the step word and Make next show only on hover.
  - Picking a "＋ New project…" option (`NEW` in `app.py`) reveals the `new_project` box in CSS; on its own it means no choice. A name without an area (a bare `__new__`) creates nothing and is flashed.
- **Moving tasks by drag** (Inbox, Next, H0, Waiting, a project page): dragging an open row slides in the project list (`.drop-panel`, `move_panel()` in `_task.html`, `static/move.js`; hidden otherwise so the layout doesn't change). Rows drag from anywhere, except on a project page, where the ⠿ grip drags them, so one drag reorders or moves out. The header's Projects button (`pin_button()`) pins it open (`body.pinned-panel`, floating over the page, which doesn't move) until pressed again; the choice is kept in `localStorage` per kind of page (`<key>-panel`: `inbox`, `next`, `h0`, `waiting`, `project`). The panel leaves out where the page's tasks already are (H0 on H0, the project on its page).
  - A drop posts to `/tasks/move` (`task_move()` in `app.py`, fetch only: 204, or 400 if the task isn't open or no project was given). Into a project it becomes a `later` step (next stays a conscious choice; the old project may stall), but a `waiting` step stays waiting; H0 drops the step. Dropped where it already is, nothing changes.
  - The row is removed in place (its band's count drops, the nav is refetched). Waiting (`reload=True`, its moved row usually stays) and a project's next action (the project now stalls) reload instead.
  - The Reference target (`project_id=reference`) turns the task into a reference item (title, notes) and deletes it, or abandons it if it has time on it, so the time is kept. Dropping on "＋ New…" keeps the panel open for a name: Enter creates the project in that area and moves it there, Esc or clicking away cancels. One row per drag; no tick boxes (minimal look).
- **Reference** (`/reference`, table `reference`, `REFERENCE_FIELDS`) is one flat list of title + notes: information with no action, kept here so it needn't live in another app. No projects, tags or folders (yet); search finds it.
  - Only material worth looking up goes here: given-up projects are abandoned (Done), not turned into reference, so Reference doesn't fill with dead items.
  - Adding an item opens its page, so its notes go in right away.
  - An item's page (`/reference/<id>`) lists the URLs in its notes as links above the textarea (the `links` filter in `app.py`: any `scheme://`, so `obsidian://` too). Deleting is a hard delete and returns to the list.
- **Reminders** push a task to the phone at a chosen time through ntfy, self-hosted on the VM (set up by `ntfy-setup.sh` in the home-server repo; tailnet only, no auth; the phone's ntfy app subscribes to `gtd`). They are rare, time-sensitive pings, separate from due dates: nothing links the two, and a repeat's next instance gets no reminder.
  - `tasks.remind_at` (UTC ISO, `db.iso()`) and `tasks.reminded_at` (set once sent). The task page's Remind box takes words (`dates.parse_when()`: `tomorrow 9am`, `fri 14:00`, `oct 30 at 5pm`, `in 2 hours`; a date alone means 9:00, a time alone the next one) and shows local `YYYY-MM-DD HH:MM` (`local_minute` filter), which it reads back unchanged. As you type, the hint beside it shows how it reads (`/parse/remind`, above); untouched, it shows "sent" or nothing. A new time already past is flashed and not set; the shown value saved unchanged is left alone, however old.
  - A row shows 🔔 (its time as the tooltip) while a reminder is still to come; the `.remind` slot is always rendered, like ↻.
  - With `NTFY_URL` set, a daemon thread in `app.py` (`reminder_loop()`, one per gunicorn worker, so one) runs `send_reminders()` on every minute: open tasks with `remind_at <= now` and no `reminded_at` (`db.due_reminders()`). A failed send stays unsent and is retried next minute, except a 4xx (not 429), which is logged and given up. Done and abandoned tasks never fire.
  - `ntfy()` publishes JSON to the server root (topic from `NTFY_URL`): title = task title; message = project (or H0 / Inbox) and due date, the first notes line, and "Late: was due …" when sent over `LATE` (2 min) after its time. With `GTD_URL`, a tap opens the task, and two ntfy `http` action buttons call the app straight from the notification: Done (`/tasks/<id>/complete` with `X-Requested-With: fetch`, 204) and Snooze 15m (`/tasks/<id>/snooze`, 204; `SNOOZE`).
  - Changing `remind_at` clears `reminded_at` (`update_task()`), so a new time (a snooze too) fires again.
- **Timer** (`/timer`) replaced a standalone time tracker on 2026-10-22. Every time entry belongs to a task.
  - Table `time_entries` (`task_id` → `tasks`, `ON DELETE CASCADE`; `start_time`, `end_time` as UTC ISO-8601 to the second, written by `db.iso()`). No `end_time` means running. A partial unique index allows only one running entry.
  - `now()` in `app.py` reads `app.config["NOW"]` (pinned in tests), like `today()`. Never call `datetime.now()` in a route. Forms show and take local time (`local_input()`).
  - `db.timer_start()` stops whatever is running first. `complete_task()` stops the task's timer.
  - ▶ on every open task row (`row()` macro, always visible, never hover-only) starts it. On the task page, ▶/■ and the time spent sit at the right of the project line above the title (`.task-timer`, an H0 or inbox task gets the line for it alone); while running, that total ticks (`data-seconds`, the clock JS in `static/app.js`). The running timer shows as a pill pinned to the bottom of the window on every page (task link, clock, ■ stop), so it never scrolls away; one clock script in `static/app.js` ticks both it and the task page's total.
  - The Timer page's start box matches an open task by title (case-insensitive), or captures what you typed to the inbox and times that, so everything timed is in the system.
  - Editing an entry saves only fields that differ from what the form showed, so untouched times keep their seconds. Setting an end on the running entry stops it.
  - The history has no visible Save: `static/app.js` submits an entry when focus leaves its row with a time changed (moving between its start and end doesn't), or on Enter. An end before the start isn't sent; the row gets `.bad` until fixed. A hidden Save stays first in the form so Enter saves rather than pressing ▶.
  - Totals: time spent on the task page; per area today and this week on the Timer page ("H0 · ground level", "Inbox (unprocessed)" for the rest), in two columns side by side (`db.week_totals()` from local midnight and from Monday); per step at the bottom of a project page (`db.step_totals()`, done and abandoned steps included, untimed ones left out; each label opens its task), folded to its total until "Show split" (`tally(fold=True)`, a `<details>`). All are the `tally()` macro (`_task.html`): a big total, then one accent bar per item, longest first.
## GTD rules encoded in the code

- **A task has a state and, only inside a project, a step.** There is no task "status".
  - `tasks.state` is `open | done | abandoned`; every open-task query says `state = 'open'` and never looks at `step` for that.
  - `tasks.step` is `next | waiting | later`, and is `NULL` exactly when `project_id` is: H0 tasks and inbox items have none. `later` means a project step that isn't the next action yet. Beware SQL's `NULL`: `step != 'later'` skips every task without a step.
  - The schema enforces it: `CHECK ((step IS NULL) = (project_id IS NULL))` and no H0 task in a project. `_fit_step()` (run by `add_task`/`update_task`) drops a step given to a projectless task, and takes a task joining a project out of H0 and makes it a `later` step unless a step is given.
  - Writes reject unknown fields (`_insert` as well as `_update`), so a stray `status=` raises.
- **The inbox is not a step or a state.** It is every open task with neither a project nor H0, "ground level" (`tasks.ground`): `INBOX` in `db.py`, `db.inbox_tasks()`, and an `inbox` column on every `TASK_SELECT` row. Only H0 is a deliberate choice of no project; any other projectless task is an orphan, and orphans are the inbox.
  - Filing by drag: see "Moving tasks by drag" below; Inbox is one of its lists.
  - Completing an inbox item files it under H0 (`complete_task()` sets `ground`), so done work and its time always have a home and the Timer shows no "(no project)" row; a repeat's next instance stays in the inbox. Abandoned ones are left as they are.
  - There is no Process page (removed 2026-10-27): the Inbox page is where items get dealt with (drag to file, ✓, or open one). Don't add a second page doing the same.
  - The task page shows the step picker only for a project's open task. `orphaned()` in `app.py` flashes when a task lands in the inbox.
- **H0 tasks are always next actions**, so they have no step: the H0 tab lists every open H0 task.
  - H0 is the `h0` option in the `project_select()` macro, so choosing a project and choosing H0 are one decision. Its "＋ New…" option reveals a `new_project` box; `task_fields_from_form()` creates an active project from it and uses it, overriding the dropdown. A repeat rule doesn't exempt a task.
  - H0 tasks are listed on the Horizons page (`db.h0_actions()`), and their rows show an H0 tag. Their open tasks have their own H0 tab right of Next (`/h0`, `db.h0_actions()`, counted by `VISIBLE_H0`: only tasks due today or overdue, so undated ones aren't in the number) and stay off Next Actions and its count (`DUE_NEXT`). The H0 tab has its own add box (`/h0/add`: title, due date, repeat), so a ground-level task needs no Inbox trip.
  - The task form and the Next, calendar and Waiting add lines send a next/waiting task with neither a project nor H0, or a waiting one with H0, to the inbox.
  - The Next page's add line (`/next/add`, `add_next_action()` in `app.py`, shared with the calendar) adds a next action directly: a `later` step if its project already has one.
  - The Waiting page's add box (`/waiting/add`: title, project (no H0 option), follow-up date) adds a `waiting` task; the Someday page's (`/someday/add`) adds a someday project.
- **Weekly review** (`/review`) is a checklist you finish.
  - Its steps are `REVIEW_STEPS` in `app.py`, one card each in `review.html`. A tick posts to `/review/<step>` (ticking again unticks) and is stored in `review_checks`, so a half-done review survives a closed tab.
  - One card shows at a time (a deck, `ol.deck`): `?step=`, else the first unticked. A rail of dots in the header (ticked, current, to do) links to each, between ‹ › buttons (`rel="prev|next"`): the `stepper()` macro in `_deck.html`, shared with Horizons. ← → follow those links (`static/app.js`, on any page that has them). Ticking moves on to the next unticked step; unticking stays. A step with something to count leads with it as a big number (✓ when zero), then a one-line hint. The inbox step links to the Inbox page.
  - Ticking the last step records the date in `reviews` (`db.complete_review()`), flashes "Weekly review completed on …" and clears the ticks. Past reviews show as a streak of the last 26 weeks (Monday-first squares, filled when reviewed; the date is the tooltip).
  - It lists next actions to scan, leaving out any due today or later (overdue ones stay), so the review can be finished.
- **Project statuses:** `active | waiting | someday | done | abandoned`. Someday and waiting exist only on projects, never on tasks.
  - A project page's status is one select (active, waiting, someday, done, abandoned) that saves on pick (`project_view()`, `PROJECT_STATUSES`). Picking abandoned (`db.abandon_project()`) gives a project up from any status: it and its open steps are abandoned with one shared `completed_at` (to the millisecond), its timer stopped, its time kept. It leaves every list and picker (`PROJECT_GROUPS` has no abandoned group; a task page of its step keeps it selectable through `project_select(keep=)`), and its page's select shows "abandoned".
  - Revive (Done's button, `/projects/<id>/revive`, or picking another status on its page; `db.revive_project()`) brings it back as someday, then the picked status, its steps that share that `completed_at` as `later` steps; one abandoned on its own before stays abandoned. Reviving (or adding) one step of an abandoned project also brings it back as someday (`_reopen_project()`).
  - A task page links up to its project (an H0 task has no such link) and can move the whole project to someday from there.
- **One next action per project.** `db.update_task`/`add_task` enforce it: when a project task becomes `next`, the project's other `next` tasks become `later`.
  - When a project's next action is finished, nothing is promoted automatically and nothing navigates: the project shows as stalled (⚠) until you pick one with "Make next" on its page.
  - ✓→ (on a project step's task page only; `/tasks/<id>/done-then-project`, `complete_then_project()` in `app.py`) completes the step and opens its project, so you can pick the next one right away. It's the plain ✓ plus a deliberate jump; a task without a project just stays on the page.
  - **`next` is always a conscious choice, never a default.** A step added on a project page defaults to `later` (its step picker preselects it), even when the project has no next action, and so does filing an inbox item. Only Make next, the step picker, or the Next page's add line (below) set `next`.
  - An active project with no `next` task is "stalled" (⚠).
  - Tasks of waiting and someday projects stay off Next Actions (`VISIBLE_NEXT`). Waiting projects are listed on the Waiting page.
- **No contexts.** They were removed on 2026-10-23: they were never used, and having to pick one slowed processing. Don't bring them back without being asked.
- **Every project belongs to an area (H2).** Areas are `horizons` rows with `level='area'`; `projects.area_id` points to one (`_check_area()` rejects anything else).
  - Required where a project is made: the Projects and Someday add lines (`area_select()`), an area's page (hidden `area_id`), and the pickers' per-area "＋ New project…" (`add_project_from_form()`, `task_fields_from_form()`). The db layer enforces it (`_check_area()`): `add_project()` without one, or `update_project(area_id=None)`, raises. The column itself allows `NULL` (SQLite can't add `NOT NULL` without a rebuild), but no row has it since 2026-10-30. Tests make projects with `add_project()` from `conftest.py`, which puts them in the first open area.
  - Two ways to change it: the area select on the project page's title line, which saves as soon as it's picked (`area_select(submit=True)`, `select[data-submit]` in `static/app.js`; it has no blank choice once set), and dragging a project on Projects or Someday into another area's group (`data-area` on its list; `/projects/order/<status>` gets `moved` and `area_id`). An area's group emptied by a drag disappears.
  - Task rows show only the project, not its area (removed 2026-10-29).
  - An area's page (`/horizons/<id>`) is its hub: notes, an add line for a new project there, its projects by status (`AREA_LISTS`), and "Show completed" for its done ones. Only an area no project is in (done ones too) can be deleted.
  - **Archiving** (`/horizons/<id>/archive`, one button that also unarchives; `horizons.archived_at`): an area that's over, like a done project. Refused while it has an open project (`OPEN_PROJECT`). An archived area leaves the pickers, the groupings and the Horizons card (behind its "Show archived"), and is listed on Done. An open project in an archived area unarchives it (`_reopen_area()`), as an open task reopens a done project.
  - No area on tasks: loose area work goes in a catch-all project of the area.
- **Next and H0 tabs hide nothing** (`db.next_list()`, `db.h0_actions()`): repeats not yet due are listed too. Both sort by due date (undated last).
  - The rows fold into due bands (`banded()` in `_task.html`, `BANDS`, `soon()` and the `bands` filter in `app.py`), each a `<details>` with a count: Now (overdue + today) open; This week, Later and No date folded. Empty bands aren't shown. `static/app.js` remembers a band you click open or folded, per page (`localStorage`, `fold:<path>:<band>`); only clicks are saved, since a band that starts open fires `toggle` too.
  - Dates and ↻ show on hover (an overdue date always), and the H0 page drops the rows' H0 tag.
- **A task row's project link follows its title** (`.name` in `row()`), so it doesn't float alone in the right-hand column once dates hide; the title has `flex-basis: 0`, so a long one wraps instead of pushing the buttons onto a line of their own.
- **Recurring tasks** keep their step (or H0), except a next step comes back as later (below), but don't count until their due date: the weekly review and `db.next_actions()` use `VISIBLE_NEXT`.
- **The Next and H0 nav counts** show only tasks due today or overdue (`DUE_NEXT`, `VISIBLE_H0`), so undated tasks and future repeats aren't in the number.
  - `complete_task()` marks the task done and inserts the next instance. A repeating `next` step's instance is a `later` step, so the project stalls until you pick its next action; waiting steps and H0 tasks keep theirs.
  - A repeat rule without a due date gets today as its due date.
- **Projects can't be marked done while they have open tasks.** Moving a project out of done clears its `completed_at`.
  - An open task joining a done project (filing, the dropdown, adding, reviving) reopens it as active (`_reopen_project()` in `add_task`/`update_task`/`revive_task`).
- **Only an empty project can be deleted** (no tasks at all, done ones included). The project page shows the button only then.
- **Deleting a task abandons it** (`db.abandon_task()`: state `abandoned`, `completed_at` set, its timer stopped, its time kept).
  - Revive (`/tasks/<id>/revive`, `db.revive_task()`) brings it back where it was: a later step of its project (reopening the project if it's done), an H0 task, or the inbox.
  - Deleting an abandoned task is a hard delete (its time too).
  - Completing an abandoned task does nothing. Search and suggestions include abandoned tasks with the done ones.
- **Horizons** live in one table keyed by `level`:
  - `purpose` and `vision` are single seeded notes that can't be deleted.
  - `goal` and `area` are lists; areas hold projects (above).
  - The Horizons page is a deck like the review (no ticks): one card per `HORIZON_STEPS` key (purpose, vision, goal, area, projects, ground: H5 down to H0), `?level=`, else area (H2); titles in `HORIZON_TITLES`. Purpose and vision cards autosave their notes; goal and area cards get the page's add line; deleting a goal or area returns to its card.

## Tests

Every change must be verified before it is called done:
1. Write or extend a test that fails without the change, and watch it fail first.
2. Run the whole suite (`uv run pytest`, about 2s).
3. For template or JS changes, also run `uv run pytest -m browser`, and add a browser test for new JS behavior. Click through the real app (`GTD`) for anything visual.

Enforcement:
- A Claude Code Stop hook (`.claude/settings.json` → `.claude/hooks/run-tests.sh`) won't let a turn end while tests fail. It also runs the browser tests when `templates/` or `static/` have uncommitted changes. After a green run it stores a fingerprint of the working tree (`.git/tests-green`) and skips the tests on a turn that changed nothing since.
- `.githooks/pre-commit` refuses commits while tests fail. It also runs the browser tests when the commit touches `templates/` or `static/`. It is enabled per clone with `git config core.hooksPath .githooks`.

Layout (Flask test client, temporary DB):
- `tests/conftest.py` holds the fixtures:
  - `client` pins `TODAY = 2026-10-22` and makes Jinja undefined variables raise errors, so a template typo fails instead of rendering blank.
  - `conn` opens a second connection to the same file, so assertions can go straight to `db.*`.
  - `seed(c)` builds one of everything the UI can show (each task status, H0, repeats, waiting/someday/done projects, horizons). Its ids are deterministic.
  - New date-dependent behavior should be tested against the fixed date.
- `tests/test_app.py` holds the targeted tests: GTD rules, routes, recurrence, edge cases.
- `tests/test_browser.py` (marker `browser`, Playwright + Chromium, one live server per run on a random port, a freshly seeded DB per test) drives the JS: tick-off, search suggestions, theme toggle, capture dialog, dragging project steps and projects, the project rows' ▸ and Expand all, `/` and `f` keys, date picker, timer clock, timer entry autosave, the Markdown notes editor (rendering, saving, undo), the Due, Repeats and Remind box previews, autosave and its conflict choices, the add line and "＋ New…" reveals, calendar day click and drag-to-reschedule, dragging rows of every task list onto projects, and fails on any JS error. One-time setup: `uv run playwright install chromium`.
- `tests/test_crawl.py` holds the generic tests. They usually catch accidental bugs before anyone writes a targeted test for them:
  - Every link reachable from `/` must load.
  - Every form, with each submit button and each `<select>` option, must submit and land on a 200 page. Each submission gets a fresh copy of the seeded DB and sends its own page as the Referer.
  - Saving any edit page without changing anything must leave the record unchanged.
  - Task rows must match what the tick-off script expects.
  - When you add a page, field, state or step, add it to `seed()` so the crawler reaches it.
