import functools
import os
import sqlite3
import zlib
from contextlib import contextmanager
from datetime import date, datetime, timezone

import fuzzy
import recur

# On the server GTD_DB points to /var/lib/apps/gtd/gtd.db; locally `GTD dev` points it to dev.db.
DB_PATH = os.environ.get("GTD_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtd.db")

# Someday and waiting are project statuses only; an abandoned project is given up (on Done, revivable). A task has a state (open, done, or abandoned: deleted but revivable)
# and, only inside a project, a step: an active project has exactly one "next" step, its other open steps are "waiting"
# or "later". H0 tasks (ground level) and inbox items (neither a project nor H0) have no step.
SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'waiting', 'someday', 'done', 'abandoned')),
  position INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT,
  area_id INTEGER REFERENCES horizons(id)
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  state TEXT NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'done', 'abandoned')),
  step TEXT CHECK (step IN ('next', 'waiting', 'later')),
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  due_date TEXT,
  recur TEXT,
  ground INTEGER NOT NULL DEFAULT 0,
  position INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT,
  remind_at TEXT,
  reminded_at TEXT,
  CHECK ((step IS NULL) = (project_id IS NULL)),
  CHECK (NOT (ground AND project_id IS NOT NULL))
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
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  archived_at TEXT
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
-- When each someday project was last looked at in a weekly review (UTC, iso()). A table of its own, not a column,
-- so the live DB needs no ALTER.
CREATE TABLE IF NOT EXISTS project_reviews (
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  reviewed_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS time_entries_one_running ON time_entries((end_time IS NULL)) WHERE end_time IS NULL;
"""
# Stored in the file's user_version once SCHEMA has run on it, so a connection skips it until SCHEMA changes.
SCHEMA_VERSION = zlib.crc32(SCHEMA.encode()) & 0x7FFFFFFF

# State changes via complete/abandon/revive. remind_at is a UTC ISO time; reminded_at is set once it has been sent.
TASK_FIELDS = {"title", "notes", "step", "project_id", "due_date", "recur", "ground", "position", "remind_at"}
PROJECT_FIELDS = {"title", "notes", "status", "area_id"}  # area_id: an H2 area (a horizons row), required
# Open tasks need a project unless deliberately marked H0 (ground level); the ones with neither are the inbox.
INBOX = "t.state = 'open' AND t.project_id IS NULL AND NOT t.ground"
TASK_SELECT = f"""SELECT t.*, p.title AS project_title, p.status AS project_status, ({INBOX}) AS inbox
  FROM tasks t LEFT JOIN projects p ON p.id = t.project_id"""
# Next Actions leave out H0 tasks (they have their own tab), recurring tasks until their due day, and tasks of waiting/someday projects.
ACTIVE = "t.project_id IN (SELECT id FROM projects WHERE status = 'active')"
NEXT = f"t.state = 'open' AND t.step = 'next' AND {ACTIVE}"
READY = "(t.recur IS NULL OR t.due_date IS NULL OR t.due_date <= ?)"
VISIBLE_NEXT = f"{NEXT} AND {READY}"
DUE_NEXT = f"{NEXT} AND t.due_date <= ?"  # the nav count: due today or overdue only
VISIBLE_H0 = "t.state = 'open' AND t.ground AND t.due_date <= ?"  # the tab's count: due today or overdue only
OPEN_PROJECT = "('active', 'waiting', 'someday')"  # project statuses still in play; an area holding one is open
STEP_ORDER = "CASE WHEN t.ground OR t.step = 'next' THEN 0 WHEN t.step = 'waiting' THEN 1 WHEN t.step = 'later' THEN 2 ELSE 3 END"


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
    """`today` dates an H0 task given no due date (_fit_step())."""
    if fields.get("project_id") and "position" not in fields:  # a new step goes to the top, under the next action
        fields["position"] = _top(c, "tasks", "project_id = ?", fields["project_id"])
    id = _insert(c, "tasks", TASK_FIELDS, _fit_step({"title": title, **fields}, today=today))
    _reopen_project(c, id)
    _one_next(c, id, was_next=False)
    return id


@atomic
def update_task(c, id, today=None, **fields):
    """Returns the titles of project tasks demoted to later because this one became next.
    `today` dates an H0 task left without a due date (_fit_step())."""
    current = get_task(c, id)
    if current and fields.get("project_id") and fields["project_id"] != current["project_id"] and "position" not in fields:
        fields["position"] = _top(c, "tasks", "project_id = ?", fields["project_id"])  # filed: top of its project
    _update(c, "tasks", TASK_FIELDS, id, _fit_step(fields, current, today))
    if current and "remind_at" in fields and fields["remind_at"] != current["remind_at"]:
        c.execute("UPDATE tasks SET reminded_at = NULL WHERE id = ?", (id,))  # a new time is sent again
    _reopen_project(c, id)
    return _one_next(c, id, was_next=current is not None and current["step"] == "next")


def _reopen_project(c, id):
    """A done project with an open task isn't done any more: filing into one (pickers list done projects) reopens it.
    An abandoned one (a step revived) comes back as someday, like a revived project."""
    p = c.execute("""SELECT id, status FROM projects WHERE status IN ('done', 'abandoned')
        AND id = (SELECT project_id FROM tasks WHERE id = ? AND state = 'open')""", (id,)).fetchone()
    if p:
        status = "active" if p["status"] == "done" else "someday"
        c.execute("UPDATE projects SET status = ?, completed_at = NULL, position = ? WHERE id = ?",
                  (status, _top(c, "projects", "status = ?", status), p["id"]))
        _reopen_area(c, p["id"])


def _fit_step(fields, current=None, today=None):
    """Only a project's tasks have a step: a task without one (H0, inbox) drops any step it's given, and one
    joining a project leaves H0 and becomes a later step unless told otherwise. An H0 task is always dated:
    left without a due date, it's due today (`today`, else the system's date)."""
    project_id = fields["project_id"] if "project_id" in fields else current and current["project_id"]
    if not project_id:
        fields["step"] = None
        ground = fields["ground"] if "ground" in fields else current and current["ground"]
        due = fields["due_date"] if "due_date" in fields else current and current["due_date"]
        if ground and not due:
            fields["due_date"] = (today or date.today()).isoformat()
        return fields
    if "project_id" in fields:
        fields["ground"] = 0
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
    if not t or t["state"] != "open" or t["step"] != "next":
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
def revive_task(c, id, today=None):
    """An abandoned task comes back where it was: a later step of its project, an H0 task (due today if undated),
    or an inbox item. A done project it belongs to is reopened, since it has an open task again."""
    c.execute("""UPDATE tasks SET state = 'open', step = CASE WHEN project_id IS NULL THEN NULL ELSE 'later' END,
        due_date = CASE WHEN ground AND due_date IS NULL THEN ? ELSE due_date END,
        completed_at = NULL WHERE id = ? AND state = 'abandoned'""", ((today or date.today()).isoformat(), id))
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
    if not c.execute("""UPDATE tasks SET state = 'done', completed_at = datetime('now'), ground = ground OR project_id IS NULL
            WHERE id = ? AND state = 'open'""", (id,)).rowcount:
        return None  # finished meanwhile (a second tap, the phone's Done button): one next instance only
    new_id = None
    if recur.parse(t["recur"]):
        due = date.fromisoformat(t["due_date"]) if t["due_date"] else None
        next_due = recur.next_due(t["recur"], due, today)
        step = "later" if t["step"] == "next" else t["step"]
        new_id = add_task(c, t["title"], today=today, notes=t["notes"], step=step, project_id=t["project_id"],
                          ground=t["ground"], recur=t["recur"], position=t["position"], due_date=next_due.isoformat())
    return new_id


def due_reminders(c, now):
    """Open tasks whose reminder time has come and that haven't been sent yet."""
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.reminded_at IS NULL AND t.remind_at <= ?
        ORDER BY t.remind_at, t.id""", (iso(now),)).fetchall()


@atomic
def mark_reminded(c, id, now):
    c.execute("UPDATE tasks SET reminded_at = ? WHERE id = ?", (iso(now), id))


def tasks_with_step(c, step):
    """Open project tasks at this step."""
    return c.execute(f"{TASK_SELECT} WHERE t.state = 'open' AND t.step = ? ORDER BY t.created_at DESC, t.id DESC", (step,)).fetchall()


def next_actions(c, today):
    """Next actions that count today: next_list() minus repeats not yet due."""
    return c.execute(f"""{TASK_SELECT} WHERE {VISIBLE_NEXT}
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at DESC, t.id DESC""", (today.isoformat(),)).fetchall()


def h0_actions(c):
    """Every H0 next action, repeats not yet due included, soonest due first; undated ones last."""
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.ground
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at DESC, t.id DESC""").fetchall()


def inbox_tasks(c):
    return c.execute(f"{TASK_SELECT} WHERE {INBOX} ORDER BY t.created_at DESC, t.id DESC").fetchall()


def next_list(c):
    """The Next tab: every next action outside H0 and waiting/someday projects, repeats not yet due included,
    soonest due first; undated ones last."""
    return c.execute(f"""{TASK_SELECT} WHERE {NEXT}
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at DESC, t.id DESC""").fetchall()


def overdue(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND (t.ground OR t.step IN ('next', 'waiting') AND {ACTIVE}) AND t.due_date < ?
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
        "inbox": one(f"SELECT count(*) FROM tasks t WHERE {INBOX}"),
        "next": one(f"SELECT count(*) FROM tasks t WHERE {DUE_NEXT}", today.isoformat()),
        "h0": one(f"SELECT count(*) FROM tasks t WHERE {VISIBLE_H0}", today.isoformat()),
        "next_overdue": one(f"SELECT count(*) FROM tasks t WHERE {DUE_NEXT} AND t.due_date < ?", today.isoformat(), today.isoformat()),
        "h0_overdue": one(f"SELECT count(*) FROM tasks t WHERE {VISIBLE_H0} AND t.due_date < ?", today.isoformat(), today.isoformat()),
        "waiting": one("SELECT count(*) FROM tasks WHERE state = 'open' AND step = 'waiting'")
                   + one("SELECT count(*) FROM projects WHERE status = 'waiting'"),
        "someday": one("SELECT count(*) FROM projects WHERE status = 'someday'"),
        "stalled": len(stalled_projects(c)),
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
    done = c.execute(f"""{TASK_SELECT} WHERE t.state != 'open' AND {where}
        ORDER BY t.completed_at DESC LIMIT 50""", args).fetchall()
    where, args = _matching(q, ["title", "notes"])
    projects = c.execute(f"""SELECT * FROM projects WHERE {where}
        ORDER BY status IN ('done', 'abandoned'), lower(title)""", args).fetchall()
    horizons = c.execute(f"SELECT * FROM horizons WHERE {where} ORDER BY lower(title)", args).fetchall()
    references = c.execute(f"SELECT * FROM reference WHERE {where} ORDER BY lower(title)", args).fetchall()
    return {"tasks": tasks, "done": done, "projects": projects, "horizons": horizons, "references": references}


def suggest(c, q, limit=10):
    """The best fuzzy title matches among tasks (done ones too), projects, horizons and reference.
    Titles containing every query word come first, so scattered-letter matches can't crowd them out;
    then open items before done ones."""
    rows = [("task", r) for r in c.execute(f"""SELECT t.id, t.title, t.state, t.step, t.ground, p.title AS project_title,
        ({INBOX}) AS inbox FROM tasks t LEFT JOIN projects p ON p.id = t.project_id""")]  # no notes: only titles count
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
    _check_area(c, {"area_id": None, **fields})
    top = _top(c, "projects", "status = ?", fields.get("status", "active"))  # a new project goes to the top
    id = _insert(c, "projects", PROJECT_FIELDS, {"title": title, **fields})
    c.execute("UPDATE projects SET position = ? WHERE id = ?", (top, id))
    _reopen_area(c, id)
    return id


def _check_area(c, fields):
    """Every project is in an area: a new one needs one, and one can only move to another."""
    if "area_id" in fields and not c.execute(
            "SELECT 1 FROM horizons WHERE id = ? AND level = 'area'", (fields["area_id"],)).fetchone():
        raise ValueError(f"not an area: {fields['area_id']}")


def _reopen_area(c, project_id):
    """An archived area with an open project in it isn't archived any more, as a done project with an open task
    isn't done."""
    c.execute(f"""UPDATE horizons SET archived_at = NULL WHERE archived_at IS NOT NULL AND id =
        (SELECT area_id FROM projects WHERE id = ? AND status IN {OPEN_PROJECT})""", (project_id,))


@atomic
def update_project(c, id, **fields):
    _check_area(c, fields)
    if "status" in fields:
        # A project changing lists goes to the top of the new one, rather than to its place in the old one.
        c.execute("UPDATE projects SET completed_at = CASE WHEN ? = 'done' THEN coalesce(completed_at, datetime('now')) END,"
                  " position = CASE WHEN status = ? THEN position"
                  " ELSE (SELECT coalesce(min(position), 0) - 1 FROM projects WHERE status = ?) END WHERE id = ?",
                  (fields["status"], fields["status"], fields["status"], id))
    _update(c, "projects", PROJECT_FIELDS, id, fields)
    _reopen_area(c, id)


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
    _reopen_area(c, id)


def abandoned_projects(c, limit=100):
    """Abandoned projects, newest first."""
    return c.execute("SELECT * FROM projects WHERE status = 'abandoned' ORDER BY completed_at DESC, id DESC LIMIT ?",
                     (limit,)).fetchall()


REVIEWED = "(SELECT r.reviewed_at FROM project_reviews r WHERE r.project_id = p.id) AS reviewed_at"


def get_project(c, id):
    return c.execute(f"SELECT p.*, {REVIEWED} FROM projects p WHERE p.id = ?", (id,)).fetchone()


def projects(c, status="active", area_id=None):
    """Projects with the given status (and area) in the order dragged, new ones on top."""
    return c.execute(f"""SELECT p.*, {REVIEWED},
          (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.state = 'open' AND t.step = 'next') AS next_count
        FROM projects p WHERE p.status = ? AND (? IS NULL OR p.area_id = ?)
        ORDER BY p.position, p.id""", (status, area_id, area_id)).fetchall()


def someday_to_review(c):
    """Someday projects for the weekly review: never reviewed first, then the longest ago."""
    return sorted(projects(c, "someday"), key=lambda p: (p["reviewed_at"] is not None, p["reviewed_at"] or ""))


@atomic
def mark_reviewed(c, id, now):
    """A someday project looked at in a weekly review, at `now`."""
    c.execute("INSERT OR REPLACE INTO project_reviews (project_id, reviewed_at) VALUES (?, ?)", (id, iso(now)))


def by_area(c, projects):
    """[(area, its projects)] in area order, areas without any left out."""
    groups = [(a, [p for p in projects if p["area_id"] == a["id"]]) for a in areas(c, archived=None)]
    return [(a, ps) for a, ps in groups if ps]


@atomic
def reorder_projects(c, status, ids):
    """Number the projects with this status in the given order; other projects' ids are ignored."""
    c.executemany("UPDATE projects SET position = ? WHERE id = ? AND status = ?",
                  [(n, id, status) for n, id in enumerate(ids)])


def open_steps(c, status="active", area_id=None):
    """{project id: its open steps in project-page order}, for every project with the given status (None: any),
    optionally only in one area."""
    steps = {}
    for t in c.execute(f"""{TASK_SELECT} WHERE (? IS NULL OR p.status = ?) AND (? IS NULL OR p.area_id = ?)
            AND t.project_id IS NOT NULL AND t.state = 'open' ORDER BY t.position, t.id DESC""",
            (status, status, area_id, area_id)):
        steps.setdefault(t["project_id"], []).append(t)
    return steps


def done_tasks(c, limit=100, project_id=None, ground=None, state="done"):
    """Finished (or, with state="abandoned", abandoned) tasks, newest first; optionally only one project's,
    or only H0 (ground=1) / non-H0 (ground=0). Without a project, an abandoned project's steps are left out:
    Done lists the project instead."""
    where, args = "t.state = ?", [state]
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


def project_groups(c):
    """Every project, for pickers (the task dropdown, Inbox's drop panel): (label, projects, area id) for each open
    area, even an empty one (a new project can start there), then done ones.
    Within an area active projects come first, then waiting, then someday, each in its list's own order."""
    rows = c.execute("""SELECT * FROM projects WHERE status != 'abandoned'
        ORDER BY position, id""").fetchall()
    rank = {"active": 0, "waiting": 1, "someday": 2}
    live = sorted((p for p in rows if p["status"] in rank), key=lambda p: rank[p["status"]])  # stable: keeps the drag order
    groups = [(a["title"], [p for p in live if p["area_id"] == a["id"]], a["id"]) for a in areas(c)]
    groups.append(("Done", [p for p in rows if p["status"] == "done"], None))
    return [g for g in groups if g[1] or g[2]]


def stalled_projects(c):
    """Active projects with no next action -- the weekly review's main target."""
    return [p for p in projects(c) if p["next_count"] == 0]


# --- calendar --------------------------------------------------------------

def dated_tasks(c, start, end):
    return c.execute(f"""{TASK_SELECT} WHERE t.state = 'open' AND t.due_date BETWEEN ? AND ?
        ORDER BY t.due_date, t.created_at DESC, t.id DESC""", (start.isoformat(), end.isoformat())).fetchall()


# --- horizons (H5 purpose .. H2 areas) -------------------------------------

HORIZON_FIELDS = {"level", "title", "notes"}
SINGLE_HORIZONS = ("purpose", "vision")  # exactly one note each; can't be deleted


def horizons(c):
    """{level: rows}, each alphabetical; an area row counts its open projects. Archived areas are left out."""
    rows = c.execute(f"""SELECT h.*, (SELECT count(*) FROM projects p WHERE p.area_id = h.id AND p.status IN {OPEN_PROJECT})
        AS project_count FROM horizons h WHERE h.archived_at IS NULL ORDER BY lower(h.title)""").fetchall()
    out = {level: [] for level in ("purpose", "vision", "goal", "area")}
    for h in rows:
        out[h["level"]].append(h)
    return out


def areas(c, archived=False):
    """Areas, alphabetical: open ones, archived ones (archived=True, newest first), or all (None)."""
    where = {False: "AND archived_at IS NULL", True: "AND archived_at IS NOT NULL", None: ""}[archived]
    order = "archived_at DESC, id DESC" if archived else "lower(title), id"
    return c.execute(f"SELECT * FROM horizons WHERE level = 'area' {where} ORDER BY {order}").fetchall()


@atomic
def archive_area(c, id):
    """An area is over (a term's course, say) once none of its projects is open; done ones stay in it."""
    if c.execute(f"SELECT 1 FROM projects WHERE area_id = ? AND status IN {OPEN_PROJECT}", (id,)).fetchone():
        return False
    c.execute("UPDATE horizons SET archived_at = datetime('now') WHERE id = ? AND level = 'area'", (id,))
    return True


@atomic
def unarchive_area(c, id):
    c.execute("UPDATE horizons SET archived_at = NULL WHERE id = ?", (id,))


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
    """Purpose and vision can't be deleted, nor an area any project (done ones too) is in."""
    if c.execute("SELECT 1 FROM projects WHERE area_id = ?", (id,)).fetchone():
        return False
    c.execute("DELETE FROM horizons WHERE id = ? AND level NOT IN ('purpose', 'vision')", (id,))
    return True


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
    """[(area title, or H0 / inbox, seconds)] for entries started since `since`, most time first."""
    rows = c.execute(f"""SELECT CASE WHEN p.id IS NOT NULL THEN a.title
          WHEN t.ground THEN 'H0 · ground level' WHEN t.state = 'open' THEN 'Inbox (unprocessed)' ELSE '(no project)' END
          AS label, sum({SECONDS}) AS secs
        FROM time_entries e JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = t.project_id
          LEFT JOIN horizons a ON a.id = p.area_id
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
    c.execute("BEGIN IMMEDIATE")  # no other save between the check and the write
    row = c.execute(f"SELECT title, notes FROM {table} WHERE id = ?", (id,)).fetchone()
    if any(norm(row[k]) not in (norm(base.get(k)), norm(v)) for k, v in fields.items()):
        c.rollback()
        return {k: row[k] for k in ("title", "notes")}
    TEXT_TABLES[table](c, id, **fields)
    return None
