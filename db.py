import functools
import os
import re
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timezone

import fuzzy
import recur

# On the server GTD_DB points to /var/lib/apps/gtd/gtd.db; locally `GTD dev` points it to dev.db.
DB_PATH = os.environ.get("GTD_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtd.db")

# A project, like a task, has a state: open, done, or abandoned (given up; on Done, revivable). It has no status: what
# it waits on is a waiting step. A task has a state and, inside a project or H0, a step: a project's are "next",
# "waiting" or "later" (an open project needs a next or waiting one, each chosen); an H0 task (ground level) is "next", "waiting" or
# "someday" (a single someday / maybe item). Inbox items (neither a project nor H0) have no step.
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'abandoned')),
  position INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'abandoned')),
  step TEXT CHECK (step IN ('next', 'waiting', 'later', 'someday')),
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  due_date TEXT,
  recur TEXT,
  ground INTEGER NOT NULL DEFAULT 0,
  position INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT,
  remind_at TEXT,
  reminded_at TEXT,
  CHECK ((step IS NULL) = (project_id IS NULL AND NOT ground)),
  CHECK (NOT (ground AND project_id IS NOT NULL)),
  CHECK (NOT (ground AND step = 'later')),
  CHECK (ground OR step != 'someday')
);
CREATE INDEX IF NOT EXISTS tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id);
CREATE INDEX IF NOT EXISTS tasks_open ON tasks(project_id) WHERE state = 'open';
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(due_date);
CREATE TABLE IF NOT EXISTS horizons (
  id INTEGER PRIMARY KEY,
  level TEXT NOT NULL CHECK (level IN ('purpose', 'vision', 'goal', 'area')),
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
INSERT INTO horizons (level, title) SELECT 'purpose', 'Purpose & principles'
  WHERE NOT EXISTS (SELECT 1 FROM horizons WHERE level = 'purpose');
INSERT INTO horizons (level, title) SELECT 'vision', 'Vision'
  WHERE NOT EXISTS (SELECT 1 FROM horizons WHERE level = 'vision');
-- Reference: information with no action attached, one flat list.
CREATE TABLE IF NOT EXISTS reference (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
-- Time tracking: every entry belongs to a task. Times are UTC ISO-8601; no end_time means running.
CREATE TABLE IF NOT EXISTS time_entries (
  id INTEGER PRIMARY KEY,
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  start_time TEXT NOT NULL,
  end_time TEXT
);
CREATE INDEX IF NOT EXISTS time_entries_task ON time_entries(task_id);
-- Weekly review: the steps ticked so far, and the date of each finished review.
CREATE TABLE IF NOT EXISTS review_checks (step TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS reviews (id INTEGER PRIMARY KEY, completed_on TEXT NOT NULL);
-- The review's scratchpad (Collect loose ends, Mind sweep): one row. `cleared` is the text its last Clear removed,
-- kept for Undo until the next Clear, an Undo, or the review's end.
CREATE TABLE IF NOT EXISTS scratchpad (
  id INTEGER PRIMARY KEY CHECK (id = 1),
  notes TEXT NOT NULL DEFAULT '',
  cleared TEXT NOT NULL DEFAULT ''
);
INSERT INTO scratchpad (id) SELECT 1 WHERE NOT EXISTS (SELECT 1 FROM scratchpad);
-- When each project, and each H0 someday task, was last looked at in a weekly review (UTC, iso()). Tables of their
-- own, not columns, so the live DB needs no ALTER.
CREATE TABLE IF NOT EXISTS project_reviews (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_reviews (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  reviewed_at TEXT NOT NULL
);
-- The reviews before each one's last, so the review's ✓ can be undone (unmark_reviewed()) back to the one before.
CREATE TABLE IF NOT EXISTS project_review_history (
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  reviewed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS task_review_history (
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  reviewed_at TEXT NOT NULL
);
-- A project's desired outcome: what done looks like. A table of its own, like project_reviews.
CREATE TABLE IF NOT EXISTS project_outcomes (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  outcome TEXT NOT NULL
);
-- A goal's or area's outcome: what done (goal) or kept up (area) looks like. A table of its own, like project_outcomes.
CREATE TABLE IF NOT EXISTS horizon_outcomes (
  horizon_id INTEGER PRIMARY KEY REFERENCES horizons(id) ON DELETE CASCADE,
  outcome TEXT NOT NULL
);
-- An area of focus's balance, set on its page: -3 far too little attention, 0 balanced, 3 far too much. A table of its
-- own, like project_outcomes; no row means not set.
CREATE TABLE IF NOT EXISTS area_balance (
  horizon_id INTEGER PRIMARY KEY REFERENCES horizons(id) ON DELETE CASCADE,
  balance INTEGER NOT NULL CHECK (balance BETWEEN -3 AND 3)
);
-- A task's place in the list it's in (Inbox, Next, Waiting, Someday: list_of()), dragged on that page. A table of its
-- own, not tasks.position, which is a step's place in its project: a waiting step is in both, in different orders.
CREATE TABLE IF NOT EXISTS list_positions (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  position INTEGER NOT NULL
);
-- A goal's or area's place in its list on Horizons, dragged there. A table of its own (no ALTER on the live DB); no
-- row sorts last, alphabetically.
CREATE TABLE IF NOT EXISTS horizon_positions (
  horizon_id INTEGER PRIMARY KEY REFERENCES horizons(id) ON DELETE CASCADE,
  position INTEGER NOT NULL
);
-- The horizons' links, set only on Horizons, each child under one parent at most: an area's vision line (H4 is built
-- from them), a goal's area, a project's goal or area, an H0 task's goal or area. Tables of their own (no ALTER).
CREATE TABLE IF NOT EXISTS area_visions (
  horizon_id INTEGER PRIMARY KEY REFERENCES horizons(id) ON DELETE CASCADE,
  vision TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS goal_areas (
  goal_id INTEGER PRIMARY KEY REFERENCES horizons(id) ON DELETE CASCADE,
  area_id INTEGER NOT NULL REFERENCES horizons(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS project_horizons (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  horizon_id INTEGER NOT NULL REFERENCES horizons(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS task_areas (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  area_id INTEGER NOT NULL REFERENCES horizons(id) ON DELETE CASCADE
);
-- Contexts: a task's tags, made by first use, kept in the order dragged on Site info. Tasks only. One word each
-- (the title mark @name ends at a space), one spelling whatever the case.
CREATE TABLE IF NOT EXISTS contexts (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE,
  position INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS task_contexts (
  task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  context_id INTEGER NOT NULL REFERENCES contexts(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, context_id)
);
CREATE INDEX IF NOT EXISTS task_contexts_context ON task_contexts(context_id);
-- A task's category: a word or two its title is filed under ("Discrete Math"), shown before it as "Discrete Math: …".
-- Any task, one at most; not a project. A table of its own (no ALTER). One spelling whatever the case (set_task_category()).
CREATE TABLE IF NOT EXISTS task_categories (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  name TEXT NOT NULL COLLATE NOCASE
);
CREATE UNIQUE INDEX IF NOT EXISTS time_entries_one_running ON time_entries((end_time IS NULL)) WHERE end_time IS NULL;
"""
# Stored in the file's user_version once SCHEMA has run on it, so a connection skips it until SCHEMA changes.
SCHEMA_VERSION = zlib.crc32(SCHEMA.encode()) & 0x7FFFFFFF

# State changes via complete/abandon/revive. remind_at is a UTC ISO time; reminded_at is set once it has been sent.
TASK_FIELDS = {"title", "notes", "step", "project_id", "due_date", "recur", "ground", "position", "remind_at"}
PROJECT_FIELDS = {"title", "notes"}  # state changes via close_project/revive_project
# Open tasks need a project unless deliberately marked H0 (ground level); the ones with neither are the inbox.
INBOX = "t.state = 'open' AND t.project_id IS NULL AND NOT t.ground"
# A task as every list shows it: its category, if any, before its title. `tk` is task_categories.
LABEL = "coalesce(tk.name || ': ', '') || t.title"
CATEGORY_JOIN = "LEFT JOIN task_categories tk ON tk.task_id = t.id"
# `contexts`: its context names, space-separated, in their dragged order; NULL with none. `category`: NULL with none;
# `label`: LABEL, what lists show (the task page edits title and category apart).
TASK_COLS = f"""t.*, tk.name AS category, {LABEL} AS label, p.title AS project_title, p.state AS project_state, ({INBOX}) AS inbox,
  lp.position AS list_position, (SELECT group_concat(name, ' ') FROM (SELECT x.name FROM task_contexts tc
    JOIN contexts x ON x.id = tc.context_id WHERE tc.task_id = t.id ORDER BY x.position)) AS contexts"""
TASK_FROM = f"""FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN list_positions lp ON lp.task_id = t.id
  {CATEGORY_JOIN}"""
TASK_SELECT = f"SELECT {TASK_COLS} {TASK_FROM}"
# The list an open task is in: inbox, next, waiting or someday (its step), or none (a later step, a closed task).
LIST = "CASE WHEN t.state != 'open' OR t.step = 'later' THEN NULL ELSE coalesce(t.step, 'inbox') END"
# Every task list's order: as dragged, new arrivals on top; tasks never placed (from before lists had an order) after
# those, in the list's old order.
LISTED = "lp.position IS NULL, lp.position"
# Next's order (and Horizons' ground card, dated ones too): as dragged; unplaced ones soonest due first, undated last.
NEXT_ORDER = f"{LISTED}, t.due_date IS NULL, t.due_date, t.created_at DESC, t.id DESC"
PLAIN_ORDER = f"{LISTED}, t.created_at DESC, t.id DESC"  # Inbox's and Someday's
WAITING_ORDER = f"{LISTED}, NOT t.ground, t.created_at DESC, t.id DESC"  # unplaced ones H0's first
LIST_ORDER = {"inbox": PLAIN_ORDER, "next": NEXT_ORDER, "waiting": WAITING_ORDER, "someday": PLAIN_ORDER}

# An H0 someday task: a single someday / maybe item. Its due date is its tickler: out of sight until then.
MAYBE = "t.state = 'open' AND t.step = 'someday'"
# Next actions: H0's and open projects'. Next lists only the undated ones, what can be done now; a dated one (every
# repeat) is a calendar item, on the Calendar on its day and in the Agenda from then on (Allen's hard landscape).
OPEN_PROJECT = "t.project_id IN (SELECT id FROM projects WHERE state = 'open')"
NEXT = f"t.state = 'open' AND t.step = 'next' AND (t.ground OR {OPEN_PROJECT})"
NEXT_LIST = f"{NEXT} AND t.due_date IS NULL"
AGENDA = "t.state = 'open' AND t.due_date <= ?"  # the Agenda and the Calendar's nav dot: due today or overdue
STEP_ORDER = """CASE WHEN t.step = 'next' THEN 0 WHEN t.step = 'waiting' THEN 1 WHEN t.step = 'later' THEN 2
  WHEN t.step = 'someday' THEN 3 ELSE 4 END"""


class Connection(sqlite3.Connection):
    depth = 0  # how many atomic() calls are under way; the outermost one commits


def connect(path=None):
    c = sqlite3.connect(path or DB_PATH, factory=Connection)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")  # gunicorn threads share the file; wait for a lock instead of failing
    c.execute("PRAGMA journal_mode = WAL")
    if c.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        c.executescript(SCHEMA)
        c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    return c


@contextmanager
def transaction(c):
    """Everything inside commits together or not at all. Nests: only the outermost one commits."""
    c.depth += 1
    try:
        yield
    except BaseException:
        c.depth -= 1
        if not c.depth:
            c.rollback()
        raise
    c.depth -= 1
    if not c.depth:
        c.commit()


def atomic(fn):
    """A db function whose writes all land, or none do."""
    @functools.wraps(fn)
    def run(c, *args, **kwargs):
        with transaction(c):
            return fn(c, *args, **kwargs)
    return run


def _insert(c, table, allowed, fields):
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(fields.values()))
    return cur.lastrowid


def _update(c, table, allowed, id, fields):
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*fields.values(), id])
    

# --- tasks -----------------------------------------------------------------

def _top(c, table, where, arg):
    """A position above every row of a list, so what arrives in it shows first."""
    return c.execute(f"SELECT coalesce(min(position), 0) - 1 FROM {table} WHERE {where}", (arg,)).fetchone()[0]


@atomic
def add_task(c, title, today=None, contexts=(), category=None, **fields):
    """`today` is for _fit_step(); `contexts`: names, made if new; `category`: set_task_category()."""
    if fields.get("project_id") and "position" not in fields:  # a new step goes to the top, under the next action
        fields["position"] = _top(c, "tasks", "project_id = ?", fields["project_id"])
    id = _insert(c, "tasks", TASK_FIELDS, _fit_step({"title": title, **fields}, today=today))
    _arrive(c, id, None)
    _reopen_project(c, id)
    _next_to_top(c, id, was_next=False)
    set_task_contexts(c, id, contexts)
    set_task_category(c, id, category)
    return id


@atomic
def update_task(c, id, today=None, **fields):
    """`today` is for _fit_step(). A `category` given is set_task_category()'s (None or blank clears it)."""
    if "category" in fields:
        set_task_category(c, id, fields.pop("category"))
    current = get_task(c, id)
    if current and fields.get("project_id") and fields["project_id"] != current["project_id"] and "position" not in fields:
        fields["position"] = _top(c, "tasks", "project_id = ?", fields["project_id"])  # filed: top of its project
    was = list_of(c, id)
    _update(c, "tasks", TASK_FIELDS, id, _fit_step(fields, current, today))
    _arrive(c, id, was)
    if current and "remind_at" in fields and fields["remind_at"] != current["remind_at"]:
        c.execute("UPDATE tasks SET reminded_at = NULL WHERE id = ?", (id,))  # a new time is sent again
    _reopen_project(c, id)
    _next_to_top(c, id, was_next=current is not None and current["step"] == "next")


@atomic
def send_to_inbox(c, id):
    """Back to the inbox, to be clarified from scratch: an open task keeps only its title, category, notes and time;
    its project or H0, step, due date, repeat, reminder, contexts, area and someday review go. On top of the Inbox."""
    t = get_task(c, id)
    if t is None or t["state"] != "open":
        return False
    update_task(c, id, project_id=None, ground=0, step=None, position=None, due_date=None, recur=None, remind_at=None)
    for table in ("task_contexts", "task_areas", "task_reviews", "task_review_history"):
        c.execute(f"DELETE FROM {table} WHERE task_id = ?", (id,))
    return True


def list_of(c, id):
    return (c.execute(f"SELECT {LIST} FROM tasks t WHERE t.id = ?", (id,)).fetchone() or [None])[0]


def _arrive(c, id, was):
    """A task that has just come into a list (it was in `was`) goes to its top, like every new item."""
    now = list_of(c, id)
    if now and now != was:
        c.execute(f"""INSERT OR REPLACE INTO list_positions (task_id, position) SELECT ?, coalesce(min(lp.position), 0) - 1
            FROM tasks t JOIN list_positions lp ON lp.task_id = t.id WHERE {LIST} = ?""", (id, now))


@atomic
def reorder_tasks(c, ids):
    """A task list's dragged rows (Inbox, a band of Next, Waiting, Someday, what the context filter leaves of them), in
    their new order: they take the places those rows hold in the whole list, so its other rows don't move. Rows of
    several lists (the Inbox's tickled someday tasks) each reorder their own list. Those in no list (closed ones) are
    ignored."""
    lists = {}
    for id in ids:
        if which := list_of(c, id):
            lists.setdefault(which, []).append(id)
    for which, moved in lists.items():
        _reorder_list(c, which, moved)


def _reorder_list(c, which, moved):
    whole = [r[0] for r in c.execute(f"""SELECT t.id FROM tasks t LEFT JOIN list_positions lp ON lp.task_id = t.id
        WHERE {LIST} = ? ORDER BY {LIST_ORDER[which]}""", (which,))]
    here = set(whole) & set(moved)
    order = iter([id for id in moved if id in here])
    placed = [next(order) if id in here else id for id in whole]
    c.executemany("INSERT OR REPLACE INTO list_positions (task_id, position) VALUES (?, ?)",
                  [(id, n) for n, id in enumerate(placed)])


def _reopen_project(c, id):
    """A done or abandoned project with an open task (a step revived) isn't closed any more: it comes back open,
    like a revived project."""
    p = c.execute("""SELECT id FROM projects WHERE state IN ('done', 'abandoned')
        AND id = (SELECT project_id FROM tasks WHERE id = ? AND state = 'open')""", (id,)).fetchone()
    if p:
        c.execute("UPDATE projects SET state = 'open', completed_at = NULL, position = ? WHERE id = ?",
                  (_top(c, "projects", "state = ?", "open"), p["id"]))


def _fit_step(fields, current=None, today=None):
    """An inbox item has no step, and drops any it's given. An H0 task is next, waiting or someday: next unless
    told or already one of the others (a later step arriving from a project is next). One joining a project leaves
    H0 and becomes a later step unless told otherwise, or already waiting. A someday H0 task's date is its tickler,
    so one becoming someday drops a date that has already come (`today`, else the system's date), or it would be
    back in the Inbox at once."""
    project_id = fields["project_id"] if "project_id" in fields else current and current["project_id"]
    ground = fields["ground"] if "ground" in fields else current and current["ground"]
    if not project_id and not ground:
        fields["step"] = None
        return fields
    step = fields.get("step") or (current and current["step"])
    if not project_id:
        fields["step"] = step if step in ("waiting", "someday") else "next"
        due = fields["due_date"] if "due_date" in fields else current and current["due_date"]
        day = (today or date.today()).isoformat()
        if fields["step"] == "someday" and due and due <= day and current and current["step"] != "someday":
            fields["due_date"] = None
        return fields
    if "project_id" in fields:
        moved = current and (current["ground"] or current["project_id"] != fields["project_id"])
        if moved and fields.get("step") in (None, "someday"):  # its next isn't this project's
            fields["step"] = "waiting" if current["step"] == "waiting" else "later"
        fields["ground"] = 0
    if fields.get("step") == "someday":  # H0's only
        fields["step"] = "later"
    if not fields.get("step") and not (current and current["step"]):
        fields["step"] = "later"
    elif "step" in fields and not fields["step"]:
        fields.pop("step")
    return fields


def _next_to_top(c, id, was_next):
    """A step that has just become next moves to the top of its project, the others shifting down in order."""
    t = get_task(c, id)
    if was_next or not t or t["state"] != "open" or t["step"] != "next" or not t["project_id"]:
        return
    reorder_project(c, t["project_id"], [id, *(s["id"] for s in project_tasks(c, t["project_id"]) if s["id"] != id)])


def has_next(c, project_id):
    return c.execute("SELECT 1 FROM tasks WHERE project_id = ? AND step = 'next' AND state = 'open'",
                     (project_id,)).fetchone() is not None


def get_task(c, id):
    return c.execute(f"{TASK_SELECT} WHERE t.id = ?", (id,)).fetchone()


@atomic
def delete_task(c, id):
    """Hard delete, time included. The UI abandons a task first; only an abandoned one is deleted for good."""
    c.execute("DELETE FROM tasks WHERE id = ?", (id,))


@atomic
def abandon_task(c, id, now=None):
    """A soft delete (stopping its timer): the task leaves every list but stays on Done, revivable."""
    running = timer_running(c)
    if running and running["task_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    c.execute("UPDATE tasks SET state = 'abandoned', completed_at = datetime('now') WHERE id = ?", (id,))


@atomic
def revive_task(c, id):
    """A done or abandoned task (the same but for the name) comes back where it was: a later step of its project, an
    H0 task (next, waiting or someday, as it was), or an inbox item. A done project it belongs to is reopened, since it
    has an open task again. A done repeat takes back the next instance its completion made, unless that has time on it."""
    t = get_task(c, id)
    if t is None or t["state"] == "open":
        return
    if t["state"] == "done" and t["recur"]:
        c.execute("""DELETE FROM tasks WHERE id = (SELECT n.id FROM tasks n WHERE n.state = 'open' AND n.id > ?
            AND n.title = ? AND n.recur = ? AND n.project_id IS ? AND n.created_at >= ?
            AND NOT EXISTS (SELECT 1 FROM time_entries e WHERE e.task_id = n.id) ORDER BY n.id LIMIT 1)""",
                  (id, t["title"], t["recur"], t["project_id"], t["completed_at"]))
    c.execute("""UPDATE tasks SET state = 'open', step = CASE WHEN project_id IS NULL THEN step ELSE 'later' END,
        completed_at = NULL WHERE id = ?""", (id,))
    _arrive(c, id, None)
    _reopen_project(c, id)


@atomic
def complete_task(c, id, today, now=None):
    """Mark done (stopping its timer); a recurring task spawns its next instance. Returns the new task id or None.
    A next step's instance comes back as a later step: picking the next action stays a conscious decision.
    Like abandoning, it leaves the task where it was (an inbox item stays one), so revive_task() undoes it."""
    t = get_task(c, id)
    if t is None or t["state"] != "open":
        return None
    running = timer_running(c)
    if running and running["task_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    if not c.execute("""UPDATE tasks SET state = 'done', completed_at = datetime('now')
            WHERE id = ? AND state = 'open'""", (id,)).rowcount:
        return None  # finished meanwhile (a second tap, the phone's Done button): one next instance only
    new_id = None
    if recur.parse(t["recur"]):
        due = date.fromisoformat(t["due_date"]) if t["due_date"] else None
        next_due = recur.next_due(t["recur"], due, today)
        step = "later" if t["step"] == "next" and t["project_id"] else t["step"]
        new_id = add_task(c, t["title"], today=today, notes=t["notes"], step=step, project_id=t["project_id"],
                          ground=t["ground"], recur=t["recur"], position=t["position"], due_date=next_due.isoformat(),
                          category=t["category"])
        c.execute("INSERT INTO task_areas (task_id, area_id) SELECT ?, area_id FROM task_areas WHERE task_id = ?",
                  (new_id, id))  # and keeps its area
        c.execute("INSERT INTO task_contexts (task_id, context_id) SELECT ?, context_id FROM task_contexts WHERE task_id = ?",
                  (new_id, id))  # and its contexts
        if t["list_position"] is not None:  # the next instance takes this one's place in its list
            c.execute("UPDATE list_positions SET position = ? WHERE task_id = ?", (t["list_position"], new_id))
    return new_id


def due_reminders(c, now):
    """Open tasks whose reminder time has come and that haven't been sent yet."""
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.reminded_at IS NULL AND t.remind_at <= ?
        ORDER BY t.remind_at, t.id""", (iso(now),)).fetchall()


@atomic
def mark_reminded(c, id, now):
    c.execute("UPDATE tasks SET reminded_at = ? WHERE id = ?", (iso(now), id))


def tasks_with_step(c, step):
    """Open tasks at this step (project and H0 tasks; inbox items have none), in their list's order (Waiting's:
    unplaced ones H0's first, newest first)."""
    return c.execute(f"{TASK_SELECT} WHERE t.state = 'open' AND t.step = ? ORDER BY {WAITING_ORDER}", (step,)).fetchall()


def inbox_tasks(c):
    return c.execute(f"{TASK_SELECT} WHERE {INBOX} ORDER BY {PLAIN_ORDER}").fetchall()


def inbox_list(c, today):
    """The Inbox page's one task list: the someday tasks whose tickler date has come (in Someday's order), then the
    inbox items. Each keeps its own list's order (reorder_tasks())."""
    return c.execute(f"{TASK_SELECT} WHERE {TICKLED} ORDER BY {PLAIN_ORDER}",
                     (today.isoformat(),)).fetchall() + inbox_tasks(c)


def next_list(c):
    """The Next tab: the undated next actions, H0's and open projects', in its order. Dated ones are the calendar's."""
    return c.execute(f"{TASK_SELECT} WHERE {NEXT_LIST} ORDER BY {NEXT_ORDER}").fetchall()


def overdue(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.step IN ('next', 'waiting') AND (t.ground OR {OPEN_PROJECT}) AND t.due_date < ?
        ORDER BY t.due_date""", (today.isoformat(),)).fetchall()


def project_tasks(c, project_id):
    """Open steps: the next action first, then the order dragged on the project page, new steps on top."""
    return c.execute(f"""{TASK_SELECT} WHERE t.project_id = ? AND t.state = 'open'
        ORDER BY t.step = 'next' DESC, t.position, t.id DESC""", (project_id,)).fetchall()


@atomic
def reorder_project(c, project_id, ids):
    """Number the project's tasks in the given order; ids from other projects are ignored."""
    c.executemany("UPDATE tasks SET position = ? WHERE id = ? AND project_id = ?",
                  [(n, id, project_id) for n, id in enumerate(ids)])


def counts(c, today):
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    return {
        "inbox": one(f"SELECT count(*) FROM tasks t WHERE {INBOX}") + one(f"SELECT count(*) FROM tasks t WHERE {TICKLED}", today.isoformat()),
        "agenda": one(f"SELECT count(*) FROM tasks t WHERE {AGENDA}", today.isoformat()),
        "overdue": one(f"SELECT count(*) FROM tasks t WHERE {AGENDA} AND t.due_date < ?", today.isoformat(), today.isoformat()),
        "waiting": one("SELECT count(*) FROM tasks WHERE state = 'open' AND step = 'waiting'"),
        "someday": one(f"SELECT count(*) FROM tasks t WHERE {MAYBE}"),
        # The Projects tab's: the open projects lacking a next action (or waiting-for) or a desired outcome.
        "flagged": one(f"SELECT count(*) FROM ({PROJECT_SELECT}) WHERE warning IS NOT NULL"),
    }


# --- contexts --------------------------------------------------------------

def contexts(c):
    """Every context in its dragged order, new ones on top, with `open`, its open tasks."""
    return c.execute("""SELECT x.*, (SELECT count(*) FROM task_contexts tc JOIN tasks t ON t.id = tc.task_id
        WHERE tc.context_id = x.id AND t.state = 'open') AS open FROM contexts x ORDER BY x.position, x.id""").fetchall()


def _context_id(c, name):
    """A context by name, whatever the case; made, on top of the list, the first time it's used."""
    row = c.execute("SELECT id FROM contexts WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    return c.execute("INSERT INTO contexts (name, position) VALUES (?, ?)",
                     (name, _top(c, "contexts", "? = 1", 1))).lastrowid


@atomic
def set_task_contexts(c, id, names):
    """A task's contexts are now exactly these names."""
    c.execute("DELETE FROM task_contexts WHERE task_id = ?", (id,))
    c.executemany("INSERT OR IGNORE INTO task_contexts (task_id, context_id) VALUES (?, ?)",
                  [(id, _context_id(c, name)) for name in names])


def tasks_in_contexts(c, ids):
    """The ids of tasks in any of these contexts."""
    marks = ", ".join("?" * len(ids))
    return {r[0] for r in c.execute(f"SELECT task_id FROM task_contexts WHERE context_id IN ({marks})", list(ids))}


def context_counts(c, task_ids):
    """How many of these tasks each context has: {context id: count}, a context with none left out."""
    if not task_ids:
        return {}
    marks = ", ".join("?" * len(task_ids))
    return dict(c.execute(f"SELECT context_id, count(*) FROM task_contexts WHERE task_id IN ({marks}) GROUP BY context_id",
                          list(task_ids)).fetchall())


def get_context(c, id):
    return next((x for x in contexts(c) if x["id"] == id), None)


@atomic
def rename_context(c, id, name):
    """Rename a context, or, if another already has that name, merge it into that one. Returns the id it ends up as."""
    other = c.execute("SELECT id FROM contexts WHERE name = ? AND id != ?", (name, id)).fetchone()
    if not other:
        c.execute("UPDATE contexts SET name = ? WHERE id = ?", (name, id))
        return id
    c.execute("""INSERT OR IGNORE INTO task_contexts (task_id, context_id)
        SELECT task_id, ? FROM task_contexts WHERE context_id = ?""", (other["id"], id))
    delete_context(c, id)
    return other["id"]


@atomic
def delete_context(c, id):
    """The context goes; its tasks stay, without it."""
    c.execute("DELETE FROM contexts WHERE id = ?", (id,))


@atomic
def reorder_contexts(c, ids):
    c.executemany("UPDATE contexts SET position = ? WHERE id = ?", [(n, id) for n, id in enumerate(ids)])


# --- categories ------------------------------------------------------------

def category_name(raw):
    """A category as typed, tidied: spaces collapsed, a trailing colon dropped (it's shown with one), each word's first
    letter capitalized (the rest left as typed: "AMS 210", "iPad" → "IPad"). '' if blank."""
    name = re.sub(r"\s+", " ", raw or "").strip().rstrip(":").strip()
    return " ".join(w[:1].upper() + w[1:] for w in name.split(" ")) if name else ""


@atomic
def set_task_category(c, id, name):
    """A task's category is now this name (None or blank: none), in title case (category_name()). A name used before,
    whatever its case, keeps the spelling it's used with most, title-cased, and every task under it takes that
    spelling, so one category never splits in two (and one stored before title case heals once it's used)."""
    name = category_name(name)
    c.execute("DELETE FROM task_categories WHERE task_id = ?", (id,))
    if name:
        # Matched by Python's casefold(), not the column's NOCASE, which folds only ASCII ("école" vs "École").
        spellings = [r["name"] for r in c.execute("""SELECT name FROM task_categories GROUP BY name COLLATE BINARY
            ORDER BY count(*) DESC, max(task_id) DESC""") if r["name"].casefold() == name.casefold()]
        name = category_name(spellings[0]) if spellings else name
        c.executemany("UPDATE task_categories SET name = ? WHERE name = ? COLLATE BINARY",
                      [(name, s) for s in spellings])
        c.execute("INSERT INTO task_categories (task_id, name) VALUES (?, ?)", (id, name))


def categories(c):
    """Every category any task has had (done and abandoned ones too: they're lasting), most used first, then the
    latest used: what the category box offers."""
    return [r[0] for r in c.execute("""SELECT name FROM task_categories GROUP BY name
        ORDER BY count(*) DESC, max(task_id) DESC""")]


# --- search ----------------------------------------------------------------

SEARCH_LIMIT = 50  # per kind


def _matching(q, cols):
    """WHERE clause and args: every word of q appears (case-insensitive) in one of cols."""
    words = q.split()
    if not words:
        return "0", []
    hay = " || ' ' || ".join(f"coalesce({col}, '')" for col in cols)
    like = lambda w: "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return " AND ".join(f"({hay}) LIKE ? ESCAPE '\\'" for _ in words), [like(w) for w in words]


def search(c, q):
    """Tasks (open, then the latest done), projects, horizons and reference whose text contains every word of q, at
    most SEARCH_LIMIT of each."""
    where, args = _matching(q, ["tk.name", "t.title", "t.notes"])
    tasks = c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND {where} ORDER BY
        {INBOX} DESC, {STEP_ORDER},
        lower(label) LIMIT {SEARCH_LIMIT}""", args).fetchall()
    done = _one_per_repeat(c.execute(f"""{TASK_SELECT} WHERE t.state != 'open' AND {where}
        ORDER BY t.completed_at DESC""", args))[:SEARCH_LIMIT]
    where, args = _matching(q, ["title", "notes"])
    projects = c.execute(f"""SELECT * FROM projects WHERE {where}
        ORDER BY state != 'open', lower(title) LIMIT {SEARCH_LIMIT}""", args).fetchall()
    horizons = c.execute(f"SELECT * FROM horizons WHERE {where} ORDER BY lower(title) LIMIT {SEARCH_LIMIT}", args).fetchall()
    references = c.execute(f"SELECT * FROM reference WHERE {where} ORDER BY lower(title) LIMIT {SEARCH_LIMIT}", args).fetchall()
    return {"tasks": tasks, "done": done, "projects": projects, "horizons": horizons, "references": references}


def _repeat_key(t):
    """A done repeat's instances share this; any other task is its own."""
    return (t["title"], t["project_id"], t["ground"], t["recur"]) if t["recur"] else t["id"]


def _one_per_repeat(rows):
    """Closed tasks, newest first, with only the latest of each repeat's done instances."""
    seen, out = set(), []
    for t in rows:
        if _repeat_key(t) not in seen:
            seen.add(_repeat_key(t))
            out.append(t)
    return out


def suggest(c, q, limit=10):
    """The best fuzzy title matches among tasks (done ones too, a repeat's latest only), projects, horizons and
    reference. Titles containing every query word come first, so scattered-letter matches can't crowd them out;
    then open items before done ones."""
    cols = f"t.id, {LABEL} AS title, t.state, t.step, t.ground, t.recur, t.project_id, p.title AS project_title"
    tasks = c.execute(f"""SELECT {cols}, ({INBOX}) AS inbox FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
        {CATEGORY_JOIN} ORDER BY t.state != 'open', t.completed_at DESC""").fetchall()  # no notes: only titles count
    rows = [("task", r) for r in tasks if r["state"] == "open"]
    rows += [("task", r) for r in _one_per_repeat(r for r in tasks if r["state"] != "open")]
    rows += [("project", r) for r in c.execute("SELECT id, title, state FROM projects")]
    rows += [("horizon", r) for r in c.execute("SELECT id, title, level FROM horizons")]
    rows += [("reference", r) for r in c.execute("SELECT id, title FROM reference")]
    hits = [(fuzzy.score(q, r["title"]), kind, r) for kind, r in rows]
    hits = [h for h in hits if h[0] is not None]
    closed = lambda kind, r: kind in ("task", "project") and r["state"] != "open"
    hits.sort(key=lambda h: (not fuzzy.exact(q, h[2]["title"]), closed(h[1], h[2]), -h[0], h[2]["title"].lower()))
    return [(kind, r) for _, kind, r in hits[:limit]]


# --- projects --------------------------------------------------------------

@atomic
def add_project(c, title, **fields):
    top = _top(c, "projects", "state = ?", "open")  # a new project goes to the top
    id = _insert(c, "projects", PROJECT_FIELDS, {"title": title, **fields})
    c.execute("UPDATE projects SET position = ? WHERE id = ?", (top, id))
    return id


@atomic
def update_project(c, id, **fields):
    _update(c, "projects", PROJECT_FIELDS, id, fields)


def project_deletable(c, id):
    """Only an empty project (no tasks at all, done and abandoned ones included) can be deleted."""
    return not c.execute("SELECT 1 FROM tasks WHERE project_id = ?", (id,)).fetchone()


@atomic
def delete_project(c, id):
    if not project_deletable(c, id):
        return False
    c.execute("DELETE FROM projects WHERE id = ?", (id,))
    return True


CLOSED = ("done", "abandoned")  # the same but for the name: either closes a project with its open steps


@atomic
def close_project(c, id, state, steps=None, now=None):
    """Close a project, done or abandoned: it and its open steps (marked `steps`, else as the project) close together
    (one shared completed_at, to the millisecond so no step closed on its own matches, tells revive which steps it
    took down), its timer is stopped and its time kept. Directly, not through complete_task(): a repeat spawns no
    next one. It leaves every list and picker for Done."""
    p = get_project(c, id)
    if not p or p["state"] in CLOSED:
        return
    running = timer_running(c)
    if running and running["project_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    stamp = c.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
    c.execute("UPDATE tasks SET state = ?, completed_at = ? WHERE project_id = ? AND state = 'open'",
              (steps or state, stamp, id))
    c.execute("UPDATE projects SET state = ?, completed_at = ?, position = NULL WHERE id = ?", (state, stamp, id))


@atomic
def revive_project(c, id):
    """A done or abandoned project comes back open, on top, with the steps closed along with it as later steps (so
    stalled, until a next action or waiting-for is picked)."""
    p = get_project(c, id)
    if not p or p["state"] not in CLOSED:
        return
    c.execute("""UPDATE tasks SET state = 'open', step = 'later', completed_at = NULL
        WHERE project_id = ? AND state IN ('done', 'abandoned') AND completed_at = ?""", (id, p["completed_at"]))
    c.execute("""UPDATE projects SET state = 'open', completed_at = NULL, position = ?
        WHERE id = ?""", (_top(c, "projects", "state = ?", "open"), id))


def closed_projects(c, state, limit=100, since=None):
    """Done (or abandoned) projects, newest first; with `since` (a datetime), only those closed since."""
    return c.execute("""SELECT * FROM projects WHERE state = ? AND completed_at >= ?
        ORDER BY completed_at DESC, id DESC LIMIT ?""", (state, stamp(since), limit)).fetchall()


REVIEWED = "(SELECT r.reviewed_at FROM project_reviews r WHERE r.project_id = p.id) AS reviewed_at"


# A project with its last review and desired outcome; `stalled` (open with neither a next action nor a waiting-for:
# nothing moves it); `vague` (open with no desired outcome); and `warning`, why it shows ⚠ (either or both), else NULL.
PROJECT_SELECT = f"""SELECT *, CASE WHEN stalled AND vague THEN 'No next action, no desired outcome'
    WHEN stalled THEN 'No next action' WHEN vague THEN 'No desired outcome' END AS warning
  FROM (SELECT p.*, {REVIEWED}, o.outcome,
    (p.state = 'open' AND NOT EXISTS (SELECT 1 FROM tasks n WHERE n.project_id = p.id AND n.state = 'open'
      AND n.step IN ('next', 'waiting'))) AS stalled,
    (p.state = 'open' AND o.outcome IS NULL) AS vague
  FROM projects p LEFT JOIN project_outcomes o ON o.project_id = p.id) p"""


def get_project(c, id):
    return c.execute(f"{PROJECT_SELECT} WHERE p.id = ?", (id,)).fetchone()


def projects(c):
    """The open projects (the Projects tab) in the order dragged, new ones on top."""
    return c.execute(f"{PROJECT_SELECT} WHERE p.state = 'open' ORDER BY p.position, p.id").fetchall()


def someday_tasks(c):
    """H0's someday tasks, in their list's order, each with `reviewed_at` and its due date as `tickle_on`."""
    return c.execute(f"""SELECT {TASK_COLS}, t.due_date AS tickle_on,
          (SELECT r.reviewed_at FROM task_reviews r WHERE r.task_id = t.id) AS reviewed_at
        {TASK_FROM} WHERE {MAYBE} ORDER BY {PLAIN_ORDER}""").fetchall()


def someday_to_review(c, today):
    """Someday / maybe for the weekly review: H0 someday tasks in Someday's order (a ✓ moves none); one tickled to a
    later date is out of sight until then."""
    return [t for t in someday_tasks(c) if not (t["tickle_on"] and t["tickle_on"] > today.isoformat())]


def projects_to_review(c):
    """The weekly review's project step: the open projects, flagged ones (⚠) first, then in Projects' order (a ✓
    moves none)."""
    return sorted(projects(c), key=lambda p: not p["warning"])


def reviewed_at(c, id, table="project"):
    """When a project (or with table="task", a task) was last looked at in a weekly review; None if never."""
    r = c.execute(f"SELECT reviewed_at FROM {table}_reviews WHERE {table}_id = ?", (id,)).fetchone()
    return r and r[0]


@atomic
def mark_reviewed(c, id, now, table="project"):
    """A project (or with table="task", an H0 someday task) looked at in a weekly review, at `now`; the review
    before goes to its history, for unmark_reviewed()."""
    c.execute(f"INSERT INTO {table}_review_history ({table}_id, reviewed_at) SELECT {table}_id, reviewed_at"
              f" FROM {table}_reviews WHERE {table}_id = ?", (id,))
    c.execute(f"INSERT OR REPLACE INTO {table}_reviews ({table}_id, reviewed_at) VALUES (?, ?)", (id, iso(now)))


@atomic
def unmark_reviewed(c, id, table="project"):
    """Undo the last review of a project (or task): the one before it, from its history, is its last again, or none.
    Returns that one's time, or None."""
    c.execute(f"DELETE FROM {table}_reviews WHERE {table}_id = ?", (id,))
    before = c.execute(f"SELECT rowid, reviewed_at FROM {table}_review_history WHERE {table}_id = ?"
                       " ORDER BY reviewed_at DESC, rowid DESC LIMIT 1", (id,)).fetchone()
    if before is None:
        return None
    c.execute(f"DELETE FROM {table}_review_history WHERE rowid = ?", (before[0],))
    c.execute(f"INSERT INTO {table}_reviews ({table}_id, reviewed_at) VALUES (?, ?)", (id, before[1]))
    return before[1]


@atomic
def set_project_outcome(c, id, outcome):
    """A project's desired outcome; blank clears it."""
    if outcome := (outcome or "").strip():
        c.execute("INSERT OR REPLACE INTO project_outcomes (project_id, outcome) VALUES (?, ?)", (id, outcome))
    else:
        c.execute("DELETE FROM project_outcomes WHERE project_id = ?", (id,))


@atomic
def reorder_projects(c, ids):
    """Number the open projects in the given order; closed projects' ids are ignored."""
    c.executemany("UPDATE projects SET position = ? WHERE id = ? AND state = 'open'",
                  [(n, id) for n, id in enumerate(ids)])


def open_steps(c):
    """{project id: its open steps in project-page order}, for every open project."""
    steps = {}
    for t in c.execute(f"""{TASK_SELECT} WHERE p.state = 'open' AND t.state = 'open'
            ORDER BY t.step = 'next' DESC, t.position, t.id DESC"""):
        steps.setdefault(t["project_id"], []).append(t)
    return steps


def done_tasks(c, limit=100, project_id=None, ground=None, state="done", since=None, once=False):
    """Finished (or, with state="abandoned", abandoned) tasks, newest first; optionally only one project's,
    or only H0 (ground=1) / non-H0 (ground=0), or (`since`, a datetime) only those closed since; `once`: a repeat's
    latest only. Without a project, the steps a closed project took down with it are left out (Done lists the project)."""
    where, args = "t.state = ? AND t.completed_at >= ?", [state, stamp(since)]
    if project_id is None:
        where += " AND NOT (coalesce(p.state IN ('done', 'abandoned'), 0) AND t.completed_at = p.completed_at)"
    else:
        where += " AND t.project_id = ?"
        args.append(project_id)
    if ground is not None:
        where += " AND t.ground = ?"
        args.append(ground)
    rows = c.execute(f"{TASK_SELECT} WHERE {where} ORDER BY t.completed_at DESC, t.id DESC LIMIT ?",
                     (*args, limit)).fetchall()
    return _one_per_repeat(rows) if once else rows


def open_projects(c):
    """Every open project, for pickers (the task dropdown, the drop panel, File to), in the Projects tab's drag
    order. Done and abandoned ones are closed: revive one to file into it."""
    return c.execute("SELECT * FROM projects WHERE state = 'open' ORDER BY position, id").fetchall()


def stalled_projects(c):
    """Open projects with neither a next action nor a waiting-for -- the weekly review's main target."""
    return [p for p in projects(c) if p["stalled"]]


# --- calendar --------------------------------------------------------------

def agenda(c, today):
    """Every open task due today or before, wherever it is (inbox, any step, any project), oldest due first."""
    return c.execute(f"""{TASK_SELECT} WHERE {AGENDA}
        ORDER BY t.due_date, t.created_at DESC, t.id DESC""", (today.isoformat(),)).fetchall()


def dated_tasks(c, start, end):
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.due_date BETWEEN ? AND ?
        ORDER BY t.due_date, t.created_at DESC, t.id DESC""", (start.isoformat(), end.isoformat())).fetchall()


# --- horizons (H5 purpose .. H2 areas: notes of their own, apart from projects) --

HORIZON_FIELDS = {"level", "title", "notes"}
SINGLE_HORIZONS = ("purpose", "vision")  # exactly one note each; can't be deleted


# A horizon with its balance, outcome, vision line (an area's) and area (a goal's), each None until set.
HORIZON_SELECT = """SELECT h.*, b.balance, o.outcome, v.vision, ga.area_id FROM horizons h
  LEFT JOIN area_balance b ON b.horizon_id = h.id LEFT JOIN horizon_outcomes o ON o.horizon_id = h.id
  LEFT JOIN area_visions v ON v.horizon_id = h.id LEFT JOIN goal_areas ga ON ga.goal_id = h.id
  LEFT JOIN horizon_positions hp ON hp.horizon_id = h.id"""


def horizons(c):
    """{level: rows}, each in its dragged order (new ones on top, never-placed ones last, alphabetical); an area's
    row has its balance (None until set)."""
    rows = c.execute(f"{HORIZON_SELECT} ORDER BY hp.position IS NULL, hp.position, lower(h.title)").fetchall()
    out = {level: [] for level in ("purpose", "vision", "goal", "area")}
    for h in rows:
        out[h["level"]].append(h)
    return out


@atomic
def add_horizon(c, level, title):
    id = _insert(c, "horizons", HORIZON_FIELDS, {"level": level, "title": title})
    c.execute("""INSERT INTO horizon_positions (horizon_id, position) SELECT ?, coalesce(min(hp.position), 0) - 1
        FROM horizons h JOIN horizon_positions hp ON hp.horizon_id = h.id WHERE h.level = ?""", (id, level))
    return id


@atomic
def reorder_horizons(c, level, ids):
    """Number the goals (or areas) in the given order; other levels' ids are ignored."""
    c.executemany("""INSERT OR REPLACE INTO horizon_positions (horizon_id, position)
                     SELECT id, ? FROM horizons WHERE id = ? AND level = ?""", [(n, id, level) for n, id in enumerate(ids)])


@atomic
def update_horizon(c, id, **fields):
    _update(c, "horizons", HORIZON_FIELDS - {"level"}, id, fields)


def get_horizon(c, id):
    return c.execute(f"{HORIZON_SELECT} WHERE h.id = ?", (id,)).fetchone()


@atomic
def set_horizon_outcome(c, id, outcome):
    """A goal's or area's outcome; blank clears it."""
    if outcome := (outcome or "").strip():
        c.execute("INSERT OR REPLACE INTO horizon_outcomes (horizon_id, outcome) VALUES (?, ?)", (id, outcome))
    else:
        c.execute("DELETE FROM horizon_outcomes WHERE horizon_id = ?", (id,))


@atomic
def set_area_balance(c, id, balance):
    """-3 (far too little) .. 3 (far too much); None clears it."""
    if balance is None:
        c.execute("DELETE FROM area_balance WHERE horizon_id = ?", (id,))
    else:
        c.execute("INSERT OR REPLACE INTO area_balance (horizon_id, balance) VALUES (?, ?)", (id, balance))


@atomic
def set_area_vision(c, id, vision):
    """An area's line in the vision (H4): what it looks like in 3–5 years; blank clears it."""
    _level(c, id, "area")
    if vision := (vision or "").strip():
        c.execute("INSERT OR REPLACE INTO area_visions (horizon_id, vision) VALUES (?, ?)", (id, vision))
    else:
        c.execute("DELETE FROM area_visions WHERE horizon_id = ?", (id,))


def _level(c, id, *levels):
    """Raise unless horizon id exists at one of these levels."""
    row = c.execute("SELECT level FROM horizons WHERE id = ?", (id,)).fetchone()
    if not row or row["level"] not in levels:
        raise ValueError(f"horizon {id} is not a {' or '.join(levels)}")


@atomic
def set_goal_area(c, goal_id, area_id):
    """The area a goal serves; None unlinks it."""
    _level(c, goal_id, "goal")
    c.execute("DELETE FROM goal_areas WHERE goal_id = ?", (goal_id,))
    if area_id is not None:
        _level(c, area_id, "area")
        c.execute("INSERT INTO goal_areas (goal_id, area_id) VALUES (?, ?)", (goal_id, area_id))


@atomic
def link_project(c, project_id, horizon_id):
    """Hang a project under a goal or an area (moving it from wherever it was); None unlinks it."""
    if not get_project(c, project_id):
        raise ValueError(f"no project {project_id}")
    c.execute("DELETE FROM project_horizons WHERE project_id = ?", (project_id,))
    if horizon_id is not None:
        _level(c, horizon_id, "goal", "area")
        c.execute("INSERT INTO project_horizons (project_id, horizon_id) VALUES (?, ?)", (project_id, horizon_id))


@atomic
def link_task(c, task_id, horizon_id):
    """Hang an H0 task under a goal or an area (moving it from wherever it was); None unlinks it. Its table keeps the
    old name, task_areas (no ALTER on the live DB): its area_id holds a goal's or an area's id."""
    t = get_task(c, task_id)
    if not t or not t["ground"]:
        raise ValueError(f"task {task_id} is not an H0 task")
    c.execute("DELETE FROM task_areas WHERE task_id = ?", (task_id,))
    if horizon_id is not None:
        _level(c, horizon_id, "goal", "area")
        c.execute("INSERT INTO task_areas (task_id, area_id) VALUES (?, ?)", (task_id, horizon_id))


# The open projects and H0 tasks the horizons' links show: closed ones drop out (their links stay, for a revive).
LINKED_PROJECTS = f"""SELECT p.id, p.title, p.warning, p.outcome, ph.horizon_id, u.title AS under
  FROM ({PROJECT_SELECT}) p LEFT JOIN project_horizons ph ON ph.project_id = p.id
  LEFT JOIN horizons u ON u.id = ph.horizon_id
  WHERE p.state = 'open' ORDER BY p.position, p.id"""
LINKED_TASKS = f"""SELECT t.id, {LABEL} AS title, t.step, t.due_date, ta.area_id AS horizon_id, u.title AS under FROM tasks t
  LEFT JOIN task_areas ta ON ta.task_id = t.id LEFT JOIN horizons u ON u.id = ta.area_id
  LEFT JOIN list_positions lp ON lp.task_id = t.id {CATEGORY_JOIN}
  WHERE t.state = 'open' AND t.ground AND t.step IN ('next', 'waiting')
  ORDER BY t.step != 'next', lp.position IS NULL, lp.position, t.id"""


def horizon_links(c, h):
    """What a goal's or area's page links to: {goals (an area's), projects, tasks (H0 actions), and for each the ones
    not linked here, to add (more_goals, more_projects, more_tasks)}. Each project and task row says where it is (`under`)."""
    projects = c.execute(LINKED_PROJECTS).fetchall()
    tasks = c.execute(LINKED_TASKS).fetchall()
    goals = horizons(c)["goal"] if h["level"] == "area" else []
    return {"goals": [g for g in goals if g["area_id"] == h["id"]],
            "more_goals": [g for g in goals if g["area_id"] != h["id"]],
            "projects": [p for p in projects if p["horizon_id"] == h["id"]],
            "tasks": [t for t in tasks if t["horizon_id"] == h["id"]],
            "more_projects": [p for p in projects if p["horizon_id"] != h["id"]],
            "more_tasks": [t for t in tasks if t["horizon_id"] != h["id"]]}


def horizon_tree(c):
    """Everything the Horizons page shows: {purpose, vision (their rows), goals, areas, projects, tasks}.
    Each goal and area is a dict: its row (`h`), a goal's area's title (`area`), what hangs under it (`goals`, an area's; `projects` and `tasks`, open
    ones) and `warning`, why it's ⚠ ("" if not): a goal with nothing moving it (no open project or H0 next action),
    serving no area, or with no "Done when…"; an area with nothing under it, or no standard. `projects` and `tasks`
    are every open project and H0 next or waiting action, each with `horizon_id` (None: loose, serving nothing)."""
    hs = horizons(c)
    projects = c.execute(LINKED_PROJECTS).fetchall()
    tasks = c.execute(LINKED_TASKS).fetchall()
    under = lambda rows, id: [r for r in rows if r["horizon_id"] == id]
    warn = lambda *checks: ", ".join(text for bad, text in checks if bad)
    area_titles = {a["id"]: a["title"] for a in hs["area"]}
    goals = []
    for g in hs["goal"]:
        ps, ts = under(projects, g["id"]), under(tasks, g["id"])
        moving = bool(ps) or any(t["step"] == "next" for t in ts)
        goals.append({"h": g, "area": area_titles.get(g["area_id"]), "projects": ps, "tasks": ts, "warning": warn(
            (not moving, "Nothing moving it"), (not g["area_id"], "Serves no area"), (not g["outcome"], "No desired outcome"))})
    areas = []
    for a in hs["area"]:
        gs = [g for g in goals if g["h"]["area_id"] == a["id"]]
        ps, ts = under(projects, a["id"]), under(tasks, a["id"])
        areas.append({"h": a, "goals": gs, "projects": ps, "tasks": ts, "warning": warn(
            (not (gs or ps or ts), "Nothing under it"), (not a["outcome"], "No standard"))})
    return {"purpose": hs["purpose"][0], "vision": hs["vision"][0], "goals": goals, "areas": areas,
            "projects": projects, "tasks": tasks}


@atomic
def delete_horizon(c, id):
    """Purpose and vision can't be deleted."""
    c.execute("DELETE FROM horizons WHERE id = ? AND level NOT IN ('purpose', 'vision')", (id,))


# --- tickler ---------------------------------------------------------------

# H0 someday tasks whose tickler date (?, today) has come: back in the Inbox until dealt with.
TICKLED = f"{MAYBE} AND t.due_date <= ?"



# --- turning a task into a reference item -----------------------------------

@atomic
def task_to_reference(c, ids, now):
    """Not actionable after all: each task becomes a reference item (title, notes) and goes: deleted, or abandoned
    when it has time on it, so the time is kept. Returns the items' ids."""
    items = []
    for id in ids:
        t = get_task(c, id)
        items.append(add_reference(c, t["label"], notes=t["notes"]))
        if task_seconds(c, id, now):
            abandon_task(c, id, now)
        else:
            delete_task(c, id)
    return items


# --- reference -------------------------------------------------------------

REFERENCE_FIELDS = {"title", "notes"}


def references(c):
    return c.execute("SELECT * FROM reference ORDER BY lower(title)").fetchall()


@atomic
def add_reference(c, title, **fields):
    return _insert(c, "reference", REFERENCE_FIELDS, {"title": title, **fields})


@atomic
def update_reference(c, id, **fields):
    _update(c, "reference", REFERENCE_FIELDS, id, fields)


def get_reference(c, id):
    return c.execute("SELECT * FROM reference WHERE id = ?", (id,)).fetchone()


CHECKLIST_LINE = re.compile(r"^\s*(?:[-*+]|\d+[.)]) \[[ xX]\](?: |$)", re.M)


def checklists(c):
    """Reference items holding a checklist (a `- [ ]` line), alphabetical: the weekly review's checklists step."""
    rows = c.execute("""SELECT * FROM reference WHERE notes LIKE '%[ ]%' OR notes LIKE '%[x]%'
        ORDER BY lower(title)""").fetchall()  # LIKE ignores case, so [X] too
    return [r for r in rows if CHECKLIST_LINE.search(r["notes"])]


@atomic
def reference_to_inbox(c, id, title, category=None):
    """↩: an item needing action after all becomes an inbox task (title, category, notes) and goes. The task's id."""
    tid = add_task(c, title, category=category, notes=get_reference(c, id)["notes"])
    delete_reference(c, id)
    return tid


@atomic
def delete_reference(c, id):
    c.execute("DELETE FROM reference WHERE id = ?", (id,))


# --- weekly review ---------------------------------------------------------

def review_checked(c):
    return {r[0] for r in c.execute("SELECT step FROM review_checks")}


@atomic
def toggle_review_step(c, step):
    if not c.execute("DELETE FROM review_checks WHERE step = ?", (step,)).rowcount:
        c.execute("INSERT INTO review_checks (step) VALUES (?)", (step,))


@atomic
def reset_review(c):
    c.execute("DELETE FROM review_checks")


@atomic
def complete_review(c, today):
    """Record a finished review and clear the ticks for next week."""
    c.execute("INSERT INTO reviews (completed_on) VALUES (?)", (today.isoformat(),))
    c.execute("DELETE FROM review_checks")
    c.execute("UPDATE scratchpad SET cleared = ''")  # Undo is for a slip during the review; the pad's text stays


def scratchpad(c):
    return c.execute("SELECT notes, cleared FROM scratchpad").fetchone()


@atomic
def update_scratchpad(c, id, notes):
    c.execute("UPDATE scratchpad SET notes = ? WHERE id = ?", (notes, id))


@atomic
def clear_scratchpad(c):
    """Empty the pad, keeping its text for Undo (an empty pad keeps what an earlier Clear kept)."""
    c.execute("UPDATE scratchpad SET cleared = notes, notes = '' WHERE notes != ''")


@atomic
def undo_clear_scratchpad(c):
    """Put the cleared text back, above anything typed since."""
    c.execute("UPDATE scratchpad SET notes = cleared || CASE WHEN notes = '' THEN '' ELSE char(10) || notes END,"
              " cleared = '' WHERE cleared != ''")


def reviews(c):
    return [r[0] for r in c.execute("SELECT completed_on FROM reviews ORDER BY completed_on DESC, id DESC")]


# --- timer -----------------------------------------------------------------

ENTRY_FIELDS = {"task_id", "start_time", "end_time"}
# `title`: the task's label (LABEL), as lists show it.
ENTRY_SELECT = f"""SELECT e.*, {LABEL} AS title, t.state AS task_state, t.ground, t.project_id, p.title AS project_title,
  ({INBOX}) AS inbox FROM time_entries e JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = t.project_id
  {CATEGORY_JOIN}"""
SECONDS = "(julianday(coalesce(e.end_time, ?)) - julianday(e.start_time)) * 86400"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def stamp(dt):
    """A datetime as SQLite's datetime('now') writes it (UTC, a space before the time), to compare with
    completed_at; None is before every stamp."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def timer_running(c):
    return c.execute(f"{ENTRY_SELECT} WHERE e.end_time IS NULL").fetchone()


def timer_last(c):
    """The entry stopped last."""
    return c.execute(f"{ENTRY_SELECT} WHERE e.end_time IS NOT NULL ORDER BY e.end_time DESC, e.id DESC LIMIT 1").fetchone()


def time_entries(c, limit=200):
    """The running entry first, then the rest, newest first."""
    return c.execute(f"{ENTRY_SELECT} ORDER BY e.end_time IS NOT NULL, e.start_time DESC LIMIT ?", (limit,)).fetchall()


def get_entry(c, id):
    return c.execute("SELECT * FROM time_entries WHERE id = ?", (id,)).fetchone()


@atomic
def timer_start(c, task_id, now):
    """Start timing a task, stopping whatever was running. Returns the new entry's id."""
    timer_stop(c, now)
    return _insert(c, "time_entries", ENTRY_FIELDS, {"task_id": task_id, "start_time": iso(now)})


@atomic
def timer_stop(c, now):
    c.execute("UPDATE time_entries SET end_time = ? WHERE end_time IS NULL", (iso(now),))


@atomic
def update_entry(c, id, **fields):
    _update(c, "time_entries", ENTRY_FIELDS, id, fields)


@atomic
def delete_entry(c, id):
    c.execute("DELETE FROM time_entries WHERE id = ?", (id,))


def task_seconds(c, task_id, now):
    return c.execute(f"SELECT coalesce(sum({SECONDS}), 0) FROM time_entries e WHERE e.task_id = ?",
                     (iso(now), task_id)).fetchone()[0]


def step_totals(c, project_id, now):
    """[(task label, seconds, task id)] for a project's timed tasks, done ones included, most time first."""
    rows = c.execute(f"""SELECT t.id, {LABEL} AS title, sum({SECONDS}) AS secs FROM time_entries e
        JOIN tasks t ON t.id = e.task_id {CATEGORY_JOIN} WHERE t.project_id = ? GROUP BY t.id ORDER BY secs DESC, t.id""", (iso(now), project_id)).fetchall()
    return [(r["title"], round(r["secs"]), r["id"]) for r in rows]


def week_totals(c, since, now):
    """[(project title, or H0 / inbox, seconds)] for entries started since `since`, most time first."""
    rows = c.execute(f"""SELECT CASE WHEN p.id IS NOT NULL THEN p.title
          WHEN t.ground THEN 'H0 · ground level' WHEN t.state = 'open' THEN 'Inbox (unprocessed)' ELSE '(no project)' END
          AS label, sum({SECONDS}) AS secs
        FROM time_entries e JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = t.project_id
        WHERE e.start_time >= ? GROUP BY label ORDER BY secs DESC""", (iso(now), iso(since))).fetchall()
    return [(r["label"], round(r["secs"])) for r in rows]


def open_tasks(c):
    """Every open task, for the timer's picker: next actions first."""
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' ORDER BY {STEP_ORDER}, lower(label)""").fetchall()


# --- autosave --------------------------------------------------------------

TEXT_TABLES = {"tasks": update_task, "projects": update_project, "horizons": update_horizon, "reference": update_reference,
               "scratchpad": update_scratchpad}
TEXT_FIELDS = {"scratchpad": ("notes",)}  # the fields autosave keeps, by table; ("title", "notes") when not listed


def text_fields(table):
    return TEXT_FIELDS.get(table, ("title", "notes"))


@atomic
def save_text(c, table, id, fields, base):
    """A record page's autosave of its title and notes. A field changed elsewhere since the page last saw it
    (now neither `base` nor the new text) is a conflict: nothing is written and the current text is returned
    for the page to ask about. Otherwise writes and returns None."""
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()
    if not c.in_transaction:  # no other save between the check and the write (inside one, its lock holds already)
        c.execute("BEGIN IMMEDIATE")
    row = c.execute(f"SELECT {', '.join(text_fields(table))} FROM {table} WHERE id = ?", (id,)).fetchone()
    if any(norm(row[k]) not in (norm(base.get(k)), norm(v)) for k, v in fields.items()):
        return dict(row)  # nothing written; the outermost transaction ends it
    TEXT_TABLES[table](c, id, **fields)
    return None
