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

# Someday and waiting are project statuses; an abandoned project is given up (on Done, revivable). A task has a state
# (open, done, or abandoned: deleted but revivable) and, inside a project or H0, a step: an active project has exactly
# one "next" step, its other open steps are "waiting" or "later"; an H0 task (ground level) is "next", "waiting" or
# "someday" (a single someday / maybe item). Inbox items (neither a project nor H0) have no step.
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'waiting', 'someday', 'done', 'abandoned')),
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
-- A someday project's tickler date (a local date): out of sight until then, then back in the Inbox. A table of its
-- own, like project_reviews. An H0 someday task's tickler is its due date.
CREATE TABLE IF NOT EXISTS project_tickles (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  tickle_on TEXT NOT NULL
);
-- A task's place in the list it's in (Inbox, Next, Waiting, Someday: list_of()), dragged on that page. A table of its
-- own, not tasks.position, which is a step's place in its project: a waiting step is in both, in different orders.
CREATE TABLE IF NOT EXISTS list_positions (
  task_id INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  position INTEGER NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS time_entries_one_running ON time_entries((end_time IS NULL)) WHERE end_time IS NULL;
"""
# Stored in the file's user_version once SCHEMA has run on it, so a connection skips it until SCHEMA changes.
SCHEMA_VERSION = zlib.crc32(SCHEMA.encode()) & 0x7FFFFFFF

# State changes via complete/abandon/revive. remind_at is a UTC ISO time; reminded_at is set once it has been sent.
TASK_FIELDS = {"title", "notes", "step", "project_id", "due_date", "recur", "ground", "position", "remind_at"}
PROJECT_FIELDS = {"title", "notes", "status"}
# Open tasks need a project unless deliberately marked H0 (ground level); the ones with neither are the inbox.
INBOX = "t.state = 'open' AND t.project_id IS NULL AND NOT t.ground"
TASK_COLS = f"""t.*, p.title AS project_title, p.status AS project_status, ({INBOX}) AS inbox,
  lp.position AS list_position"""
TASK_FROM = "FROM tasks t LEFT JOIN projects p ON p.id = t.project_id LEFT JOIN list_positions lp ON lp.task_id = t.id"
TASK_SELECT = f"SELECT {TASK_COLS} {TASK_FROM}"
# The list an open task is in: inbox, next, waiting or someday (its step), or none (a later step, a closed task).
LIST = "CASE WHEN t.state != 'open' OR t.step = 'later' THEN NULL ELSE coalesce(t.step, 'inbox') END"
# Every task list's order: as dragged, new arrivals on top; tasks never placed (from before lists had an order) after
# those, in the list's old order.
LISTED = "lp.position IS NULL, lp.position"
# Next's order (and the lists drawn from it): as dragged; unplaced ones soonest due first, undated ones last.
NEXT_ORDER = f"{LISTED}, t.due_date IS NULL, t.due_date, t.created_at DESC, t.id DESC"

# An H0 someday task: a single someday / maybe item. Its due date is its tickler: out of sight until then.
MAYBE = "t.state = 'open' AND t.step = 'someday'"
# Next Actions: H0's and active projects' next actions; recurring tasks count from their due day.
ACTIVE = "t.project_id IN (SELECT id FROM projects WHERE status = 'active')"
NEXT = f"t.state = 'open' AND t.step = 'next' AND (t.ground OR {ACTIVE})"
READY = "(t.recur IS NULL OR t.due_date IS NULL OR t.due_date <= ?)"
VISIBLE_NEXT = f"{NEXT} AND {READY}"
DUE_NEXT = f"{NEXT} AND t.due_date <= ?"  # the nav count: due today or overdue only
H0_NEXT = "t.state = 'open' AND t.ground AND t.step = 'next'"  # Horizons' ground card; H0's waiting tasks are on Waiting
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
def add_task(c, title, today=None, **fields):
    """`today` is for _fit_step()."""
    if fields.get("project_id") and "position" not in fields:  # a new step goes to the top, under the next action
        fields["position"] = _top(c, "tasks", "project_id = ?", fields["project_id"])
    id = _insert(c, "tasks", TASK_FIELDS, _fit_step({"title": title, **fields}, today=today))
    _arrive(c, id, None)
    _reopen_project(c, id)
    _one_next(c, id, was_next=False)
    return id


@atomic
def update_task(c, id, today=None, **fields):
    """Returns the titles of project tasks demoted to later because this one became next.
    `today` is for _fit_step()."""
    current = get_task(c, id)
    if current and fields.get("project_id") and fields["project_id"] != current["project_id"] and "position" not in fields:
        fields["position"] = _top(c, "tasks", "project_id = ?", fields["project_id"])  # filed: top of its project
    was = list_of(c, id)
    _update(c, "tasks", TASK_FIELDS, id, _fit_step(fields, current, today))
    _arrive(c, id, was)
    if current and "remind_at" in fields and fields["remind_at"] != current["remind_at"]:
        c.execute("UPDATE tasks SET reminded_at = NULL WHERE id = ?", (id,))  # a new time is sent again
    _reopen_project(c, id)
    return _one_next(c, id, was_next=current is not None and current["step"] == "next")


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
    """Number a task list's tasks in the given order (Inbox, a band of Next, Waiting, Someday); closed ones are ignored."""
    c.executemany("""INSERT OR REPLACE INTO list_positions (task_id, position)
        SELECT id, ? FROM tasks WHERE id = ? AND state = 'open'""", [(n, id) for n, id in enumerate(ids)])


def _reopen_project(c, id):
    """A done project with an open task isn't done any more: filing into one (pickers list done projects) reopens it.
    An abandoned one (a step revived) comes back as someday, like a revived project."""
    p = c.execute("""SELECT id, status FROM projects WHERE status IN ('done', 'abandoned')
        AND id = (SELECT project_id FROM tasks WHERE id = ? AND state = 'open')""", (id,)).fetchone()
    if p:
        status = "active" if p["status"] == "done" else "someday"
        c.execute("UPDATE projects SET status = ?, completed_at = NULL, position = ? WHERE id = ?",
                  (status, _top(c, "projects", "status = ?", status), p["id"]))


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
        if current and current["ground"] and fields.get("step") in (None, "someday"):  # out of H0: its next isn't the project's
            fields["step"] = "waiting" if current["step"] == "waiting" else "later"
        fields["ground"] = 0
    if fields.get("step") == "someday":  # H0's only: a project's someday is the project's status
        fields["step"] = "later"
    if not fields.get("step") and not (current and current["step"]):
        fields["step"] = "later"
    elif "step" in fields and not fields["step"]:
        fields.pop("step")
    return fields


def _one_next(c, id, was_next):
    """A project has at most one next action: if this task is it, the project's other next tasks become later.
    A task that has just become next moves to the top of its project, the others shifting down.
    Returns the titles it demoted."""
    t = get_task(c, id)
    if not t or t["state"] != "open" or t["step"] != "next" or not t["project_id"]:
        return []
    others = c.execute("SELECT id, title FROM tasks WHERE project_id = ? AND step = 'next' AND state = 'open' AND id != ?",
                       (t["project_id"], id)).fetchall()
    c.executemany("UPDATE tasks SET step = 'later' WHERE id = ?", [(o["id"],) for o in others])
    if not was_next:
        reorder_project(c, t["project_id"], [id, *(s["id"] for s in project_tasks(c, t["project_id"]) if s["id"] != id)])
    return [o["title"] for o in others]


def project_next(c, project_id):
    return c.execute("SELECT * FROM tasks WHERE project_id = ? AND step = 'next' AND state = 'open'",
                     (project_id,)).fetchone()


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
    """An abandoned task comes back where it was: a later step of its project, an H0 task (next, waiting or someday,
    as it was), or an inbox item. A done project it belongs to is reopened, since it has an open task again."""
    c.execute("""UPDATE tasks SET state = 'open', step = CASE WHEN project_id IS NULL THEN step ELSE 'later' END,
        completed_at = NULL WHERE id = ? AND state = 'abandoned'""", (id,))
    _arrive(c, id, None)
    _reopen_project(c, id)


@atomic
def complete_task(c, id, today, now=None):
    """Mark done (stopping its timer); a recurring task spawns its next instance. Returns the new task id or None.
    A next step's instance comes back as a later step: picking the next action stays a conscious decision.
    A finished inbox item is filed under H0, so done work (and its time) always has a home."""
    t = get_task(c, id)
    if t is None or t["state"] != "open":
        return None
    running = timer_running(c)
    if running and running["task_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    if not c.execute("""UPDATE tasks SET state = 'done', completed_at = datetime('now'), ground = ground OR project_id IS NULL,
            step = CASE WHEN project_id IS NULL THEN coalesce(step, 'next') ELSE step END
            WHERE id = ? AND state = 'open'""", (id,)).rowcount:
        return None  # finished meanwhile (a second tap, the phone's Done button): one next instance only
    new_id = None
    if recur.parse(t["recur"]):
        due = date.fromisoformat(t["due_date"]) if t["due_date"] else None
        next_due = recur.next_due(t["recur"], due, today)
        step = "later" if t["step"] == "next" and t["project_id"] else t["step"]
        new_id = add_task(c, t["title"], today=today, notes=t["notes"], step=step, project_id=t["project_id"],
                          ground=t["ground"], recur=t["recur"], position=t["position"], due_date=next_due.isoformat())
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
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.step = ?
        ORDER BY {LISTED}, NOT t.ground, t.created_at DESC, t.id DESC""", (step,)).fetchall()


def next_actions(c, today):
    """Next actions that count today: next_list() minus repeats not yet due."""
    return c.execute(f"""{TASK_SELECT} WHERE {VISIBLE_NEXT}
        ORDER BY {NEXT_ORDER}""", (today.isoformat(),)).fetchall()


def h0_actions(c):
    """Every H0 next action, repeats not yet due included, in Next's order."""
    return c.execute(f"{TASK_SELECT} WHERE {H0_NEXT} ORDER BY {NEXT_ORDER}").fetchall()


def inbox_tasks(c):
    return c.execute(f"{TASK_SELECT} WHERE {INBOX} ORDER BY {LISTED}, t.created_at DESC, t.id DESC").fetchall()


def next_list(c):
    """The Next tab: every next action, H0's and active projects', repeats not yet due included, in its order (the
    page splits it into due bands)."""
    return c.execute(f"{TASK_SELECT} WHERE {NEXT} ORDER BY {NEXT_ORDER}").fetchall()


def overdue(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.step IN ('next', 'waiting') AND (t.ground OR {ACTIVE}) AND t.due_date < ?
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
        "inbox": one(f"SELECT count(*) FROM tasks t WHERE {INBOX}") + one(f"SELECT count(*) FROM ({TICKLED})", today.isoformat()),
        "next": one(f"SELECT count(*) FROM tasks t WHERE {DUE_NEXT}", today.isoformat()),
        "next_overdue": one(f"SELECT count(*) FROM tasks t WHERE {DUE_NEXT} AND t.due_date < ?", today.isoformat(), today.isoformat()),
        "waiting": one("SELECT count(*) FROM tasks WHERE state = 'open' AND step = 'waiting'")
                   + one("SELECT count(*) FROM projects WHERE status = 'waiting'"),
        "someday": one("SELECT count(*) FROM projects WHERE status = 'someday'") + one(f"SELECT count(*) FROM tasks t WHERE {MAYBE}"),
        "stalled": one(f"SELECT count(*) FROM ({PROJECT_SELECT} WHERE p.status = 'active') WHERE stalled"),
    }


# --- search ----------------------------------------------------------------

def _matching(q, cols):
    """WHERE clause and args: every word of q appears (case-insensitive) in one of cols."""
    words = q.split()
    if not words:
        return "0", []
    hay = " || ' ' || ".join(f"coalesce({col}, '')" for col in cols)
    like = lambda w: "%" + w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    return " AND ".join(f"({hay}) LIKE ? ESCAPE '\\'" for _ in words), [like(w) for w in words]


def search(c, q):
    """Tasks (open, then the latest 50 done), projects, horizons and reference whose text contains every word of q."""
    where, args = _matching(q, ["t.title", "t.notes"])
    tasks = c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND {where} ORDER BY
        {INBOX} DESC, {STEP_ORDER},
        lower(t.title)""", args).fetchall()
    done = _one_per_repeat(c.execute(f"""{TASK_SELECT} WHERE t.state != 'open' AND {where}
        ORDER BY t.completed_at DESC""", args))[:50]
    where, args = _matching(q, ["title", "notes"])
    projects = c.execute(f"""SELECT * FROM projects WHERE {where}
        ORDER BY status IN ('done', 'abandoned'), lower(title)""", args).fetchall()
    horizons = c.execute(f"SELECT * FROM horizons WHERE {where} ORDER BY lower(title)", args).fetchall()
    references = c.execute(f"SELECT * FROM reference WHERE {where} ORDER BY lower(title)", args).fetchall()
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
    cols = "t.id, t.title, t.state, t.step, t.ground, t.recur, t.project_id, p.title AS project_title"
    tasks = c.execute(f"""SELECT {cols}, ({INBOX}) AS inbox FROM tasks t LEFT JOIN projects p ON p.id = t.project_id
        ORDER BY t.state != 'open', t.completed_at DESC""").fetchall()  # no notes: only titles count
    rows = [("task", r) for r in tasks if r["state"] == "open"]
    rows += [("task", r) for r in _one_per_repeat(r for r in tasks if r["state"] != "open")]
    rows += [("project", r) for r in c.execute("SELECT id, title, status FROM projects")]
    rows += [("horizon", r) for r in c.execute("SELECT id, title, level FROM horizons")]
    rows += [("reference", r) for r in c.execute("SELECT id, title FROM reference")]
    hits = [(fuzzy.score(q, r["title"]), kind, r) for kind, r in rows]
    hits = [h for h in hits if h[0] is not None]
    closed = lambda kind, r: (kind == "task" and r["state"] != "open") or (kind == "project" and r["status"] in ("done", "abandoned"))
    hits.sort(key=lambda h: (not fuzzy.exact(q, h[2]["title"]), closed(h[1], h[2]), -h[0], h[2]["title"].lower()))
    return [(kind, r) for _, kind, r in hits[:limit]]


# --- projects --------------------------------------------------------------

@atomic
def add_project(c, title, **fields):
    top = _top(c, "projects", "status = ?", fields.get("status", "active"))  # a new project goes to the top
    id = _insert(c, "projects", PROJECT_FIELDS, {"title": title, **fields})
    c.execute("UPDATE projects SET position = ? WHERE id = ?", (top, id))
    return id


@atomic
def update_project(c, id, **fields):
    if "status" in fields:
        # A project changing lists goes to the top of the new one, rather than to its place in the old one.
        c.execute("UPDATE projects SET completed_at = CASE WHEN ? = 'done' THEN coalesce(completed_at, datetime('now')) END,"
                  " position = CASE WHEN status = ? THEN position"
                  " ELSE (SELECT coalesce(min(position), 0) - 1 FROM projects WHERE status = ?) END WHERE id = ?",
                  (fields["status"], fields["status"], fields["status"], id))
        if fields["status"] != "someday":  # a tickler date is for someday projects only
            set_project_tickle(c, id, None)
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


@atomic
def abandon_project(c, id, now=None):
    """Give up on a project: it and its open steps are abandoned together (one shared completed_at, to the
    millisecond so no step abandoned on its own matches, tells revive which steps it took down), its timer is
    stopped and its time kept. It leaves every list for Done."""
    p = get_project(c, id)
    if not p or p["status"] == "abandoned":
        return
    running = timer_running(c)
    if running and running["project_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    stamp = c.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
    c.execute("UPDATE tasks SET state = 'abandoned', completed_at = ? WHERE project_id = ? AND state = 'open'", (stamp, id))
    c.execute("""UPDATE projects SET status = 'abandoned', completed_at = ?, position = NULL WHERE id = ?""", (stamp, id))
    set_project_tickle(c, id, None)


@atomic
def revive_project(c, id):
    """An abandoned project comes back as someday, with the steps abandoned along with it as later steps."""
    p = get_project(c, id)
    if not p or p["status"] != "abandoned":
        return
    c.execute("""UPDATE tasks SET state = 'open', step = 'later', completed_at = NULL
        WHERE project_id = ? AND state = 'abandoned' AND completed_at = ?""", (id, p["completed_at"]))
    c.execute("""UPDATE projects SET status = 'someday', completed_at = NULL, position = ?
        WHERE id = ?""", (_top(c, "projects", "status = ?", "someday"), id))


def abandoned_projects(c, limit=100):
    """Abandoned projects, newest first."""
    return c.execute("SELECT * FROM projects WHERE status = 'abandoned' ORDER BY completed_at DESC, id DESC LIMIT ?",
                     (limit,)).fetchall()


REVIEWED = """(SELECT r.reviewed_at FROM project_reviews r WHERE r.project_id = p.id) AS reviewed_at,
  (SELECT k.tickle_on FROM project_tickles k WHERE k.project_id = p.id) AS tickle_on"""


# A project with its last review, tickler date, next action's title, and `stalled` (active with no next action). A
# project has at most one next action (update_task()), so the join adds no rows.
PROJECT_SELECT = f"""SELECT p.*, {REVIEWED}, n.title AS next_title, (p.status = 'active' AND n.id IS NULL) AS stalled
  FROM projects p LEFT JOIN tasks n ON n.project_id = p.id AND n.state = 'open' AND n.step = 'next'"""


def get_project(c, id):
    return c.execute(f"{PROJECT_SELECT} WHERE p.id = ?", (id,)).fetchone()


def projects(c, status="active"):
    """Projects with the given status in the order dragged, new ones on top."""
    return c.execute(f"{PROJECT_SELECT} WHERE p.status = ? ORDER BY p.position, p.id", (status,)).fetchall()


def _least_reviewed(rows):
    """Never reviewed first, then the longest ago."""
    return sorted(rows, key=lambda r: (r["reviewed_at"] is not None, r["reviewed_at"] or ""))


def someday_tasks(c):
    """H0's someday tasks, in their list's order, each with `reviewed_at` and its due date as `tickle_on`, as a project has."""
    return c.execute(f"""SELECT {TASK_COLS}, t.due_date AS tickle_on,
          (SELECT r.reviewed_at FROM task_reviews r WHERE r.task_id = t.id) AS reviewed_at
        {TASK_FROM} WHERE {MAYBE} ORDER BY {LISTED}, t.created_at DESC, t.id DESC""").fetchall()


def someday_to_review(c, today):
    """Someday / maybe for the weekly review, H0 someday tasks and someday projects together, least recently reviewed
    first; one tickled to a later date is out of sight until then. Each row has a `kind`: "task" or "project"."""
    rows = [{**dict(p), "kind": "project"} for p in projects(c, "someday")]
    rows += [{**dict(t), "kind": "task"} for t in someday_tasks(c)]
    return _least_reviewed(r for r in rows if not (r["tickle_on"] and r["tickle_on"] > today.isoformat()))


def projects_to_review(c):
    """The weekly review's project step: active and waiting projects, stalled ones first, then least recently
    reviewed first."""
    rows = _least_reviewed(projects(c) + projects(c, "waiting"))
    return sorted(rows, key=lambda p: not p["stalled"])


@atomic
def mark_reviewed(c, id, now, table="project"):
    """A project (or with table="task", an H0 someday task) looked at in a weekly review, at `now`."""
    c.execute(f"INSERT OR REPLACE INTO {table}_reviews ({table}_id, reviewed_at) VALUES (?, ?)", (id, iso(now)))


@atomic
def set_project_tickle(c, id, tickle_on):
    """A someday project's tickler date (a local ISO date), or None to clear it."""
    if tickle_on:
        c.execute("INSERT OR REPLACE INTO project_tickles (project_id, tickle_on) VALUES (?, ?)", (id, tickle_on))
    else:
        c.execute("DELETE FROM project_tickles WHERE project_id = ?", (id,))


@atomic
def reorder_projects(c, status, ids):
    """Number the projects with this status in the given order; other projects' ids are ignored."""
    c.executemany("UPDATE projects SET position = ? WHERE id = ? AND status = ?",
                  [(n, id, status) for n, id in enumerate(ids)])


def open_steps(c, status="active"):
    """{project id: its open steps in project-page order}, for every project with the given status."""
    steps = {}
    for t in c.execute(f"""{TASK_SELECT} WHERE p.status = ? AND t.state = 'open' ORDER BY t.position, t.id DESC""",
                       (status,)):
        steps.setdefault(t["project_id"], []).append(t)
    return steps


def done_tasks(c, limit=100, project_id=None, ground=None, state="done", since=None):
    """Finished (or, with state="abandoned", abandoned) tasks, newest first; optionally only one project's,
    or only H0 (ground=1) / non-H0 (ground=0), or only those finished since a UTC datetime('now') stamp. Without a
    project, an abandoned project's steps are left out: Done lists the project instead."""
    where, args = "t.state = ?", [state]
    if since:
        where += " AND t.completed_at >= ?"
        args.append(since)
    if project_id is None:
        where += " AND coalesce(p.status, '') != 'abandoned'"
    else:
        where += " AND t.project_id = ?"
        args.append(project_id)
    if ground is not None:
        where += " AND t.ground = ?"
        args.append(ground)
    return c.execute(f"{TASK_SELECT} WHERE {where} ORDER BY t.completed_at DESC, t.id DESC LIMIT ?",
                     (*args, limit)).fetchall()


def done_since(c, since):
    """Tasks finished since a UTC datetime('now') stamp, newest first, as (task, times): a repeat's instances count
    as one row, the latest, so a daily task doesn't fill the review's look back."""
    rows = {}
    for t in done_tasks(c, limit=-1, since=since):
        task, n = rows.get(_repeat_key(t), (t, 0))
        rows[_repeat_key(t)] = (task, n + 1)
    return list(rows.values())


def project_groups(c):
    """Every project, for pickers (the task dropdown, Inbox's drop panel): [(label, projects, offers "＋ New…")]:
    the open ones (active, then waiting, then someday, each in its list's own order; even with none, a new project
    can start there), then done ones if any."""
    rows = c.execute("""SELECT * FROM projects WHERE status != 'abandoned'
        ORDER BY position, id""").fetchall()
    rank = {"active": 0, "waiting": 1, "someday": 2}
    live = sorted((p for p in rows if p["status"] in rank), key=lambda p: rank[p["status"]])  # stable: keeps the drag order
    done = [p for p in rows if p["status"] == "done"]
    return [("Projects", live, True)] + [("Done", done, False)] * bool(done)


def stalled_projects(c):
    """Active projects with no next action -- the weekly review's main target."""
    return [p for p in projects(c) if p["stalled"]]


# --- calendar --------------------------------------------------------------

def dated_tasks(c, start, end):
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.due_date BETWEEN ? AND ?
        ORDER BY t.due_date, t.created_at DESC, t.id DESC""", (start.isoformat(), end.isoformat())).fetchall()


# --- horizons (H5 purpose .. H2 areas: notes of their own, apart from projects) --

HORIZON_FIELDS = {"level", "title", "notes"}
SINGLE_HORIZONS = ("purpose", "vision")  # exactly one note each; can't be deleted


def horizons(c):
    """{level: rows}, each alphabetical."""
    rows = c.execute("SELECT * FROM horizons ORDER BY lower(title)").fetchall()
    out = {level: [] for level in ("purpose", "vision", "goal", "area")}
    for h in rows:
        out[h["level"]].append(h)
    return out


@atomic
def add_horizon(c, level, title):
    return _insert(c, "horizons", HORIZON_FIELDS, {"level": level, "title": title})


@atomic
def update_horizon(c, id, **fields):
    _update(c, "horizons", HORIZON_FIELDS - {"level"}, id, fields)


def get_horizon(c, id):
    return c.execute("SELECT * FROM horizons WHERE id = ?", (id,)).fetchone()


@atomic
def delete_horizon(c, id):
    """Purpose and vision can't be deleted."""
    c.execute("DELETE FROM horizons WHERE id = ? AND level NOT IN ('purpose', 'vision')", (id,))


# --- tickler ---------------------------------------------------------------

# H0 someday tasks and someday projects whose tickler date (?1, today) has come: back in the Inbox until dealt with.
TICKLED = f"""SELECT 'task' AS kind, t.id, t.title, t.due_date AS tickle_on FROM tasks t
    WHERE {MAYBE} AND t.due_date <= ?1
  UNION ALL SELECT 'project', p.id, p.title, k.tickle_on
    FROM project_tickles k JOIN projects p ON p.id = k.project_id WHERE p.status = 'someday' AND k.tickle_on <= ?1"""


def tickled(c, today):
    """TICKLED's rows, each with a `kind` ("task" or "project"); soonest date first."""
    return c.execute(f"SELECT * FROM ({TICKLED}) ORDER BY tickle_on, title", (today.isoformat(),)).fetchall()


# --- turning a task into a reference item -----------------------------------

@atomic
def task_to_reference(c, ids, now):
    """Not actionable after all: each task becomes a reference item (title, notes) and goes: deleted, or abandoned
    when it has time on it, so the time is kept."""
    for id in ids:
        t = get_task(c, id)
        add_reference(c, t["title"], notes=t["notes"])
        if task_seconds(c, id, now):
            abandon_task(c, id, now)
        else:
            delete_task(c, id)


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
def complete_review(c, today):
    """Record a finished review and clear the ticks for next week."""
    c.execute("INSERT INTO reviews (completed_on) VALUES (?)", (today.isoformat(),))
    c.execute("DELETE FROM review_checks")


def reviews(c):
    return [r[0] for r in c.execute("SELECT completed_on FROM reviews ORDER BY completed_on DESC, id DESC")]


# --- timer -----------------------------------------------------------------

ENTRY_FIELDS = {"task_id", "start_time", "end_time"}
ENTRY_SELECT = f"""SELECT e.*, t.title, t.state AS task_state, t.ground, t.project_id, p.title AS project_title, ({INBOX}) AS inbox
  FROM time_entries e JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = t.project_id"""
SECONDS = "(julianday(coalesce(e.end_time, ?)) - julianday(e.start_time)) * 86400"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


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
    """[(task title, seconds, task id)] for a project's timed tasks, done ones included, most time first."""
    rows = c.execute(f"""SELECT t.id, t.title, sum({SECONDS}) AS secs FROM time_entries e JOIN tasks t ON t.id = e.task_id
        WHERE t.project_id = ? GROUP BY t.id ORDER BY secs DESC, t.id""", (iso(now), project_id)).fetchall()
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
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' ORDER BY {STEP_ORDER}, lower(t.title)""").fetchall()


# --- autosave --------------------------------------------------------------

TEXT_TABLES = {"tasks": update_task, "projects": update_project, "horizons": update_horizon, "reference": update_reference}


@atomic
def save_text(c, table, id, fields, base):
    """A record page's autosave of its title and notes. A field changed elsewhere since the page last saw it
    (now neither `base` nor the new text) is a conflict: nothing is written and the current text is returned
    for the page to ask about. Otherwise writes and returns None."""
    norm = lambda s: (s or "").replace("\r\n", "\n").strip()
    if not c.in_transaction:  # no other save between the check and the write (inside one, its lock holds already)
        c.execute("BEGIN IMMEDIATE")
    row = c.execute(f"SELECT title, notes FROM {table} WHERE id = ?", (id,)).fetchone()
    if any(norm(row[k]) not in (norm(base.get(k)), norm(v)) for k, v in fields.items()):
        return {k: row[k] for k in ("title", "notes")}  # nothing written; the outermost transaction ends it
    TEXT_TABLES[table](c, id, **fields)
    return None
