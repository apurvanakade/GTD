import os
import sqlite3
from datetime import date, datetime, timezone

import fuzzy
import recur

# On the server GTD_DB points to /var/lib/apps/gtd/gtd.db; locally `GTD dev` points it to dev.db.
DB_PATH = os.environ.get("GTD_DB") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtd.db")

# Someday and waiting are project statuses only. A deleted task is first "abandoned" (revivable), like done. An active project has exactly one next action;
# its other open steps are "later".
PROJECTS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'waiting', 'someday', 'done')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
)
"""
TASKS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'inbox' CHECK (status IN ('inbox', 'next', 'waiting', 'later', 'done', 'abandoned')),
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  due_date TEXT,
  recur TEXT,
  ground INTEGER NOT NULL DEFAULT 0,
  position INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
)
"""

SCHEMA = """
{projects};
{tasks};
CREATE INDEX IF NOT EXISTS tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS tasks_project ON tasks(project_id);
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
CREATE UNIQUE INDEX IF NOT EXISTS time_entries_one_running ON time_entries((end_time IS NULL)) WHERE end_time IS NULL;
""".replace("{projects}", PROJECTS_TABLE.format(name="projects")).replace("{tasks}", TASKS_TABLE.format(name="tasks"))

TASK_FIELDS = {"title", "notes", "status", "project_id", "due_date", "recur", "ground"}
PROJECT_FIELDS = {"title", "notes", "status"}
TASK_SELECT = "SELECT t.*, p.title AS project_title, p.status AS project_status FROM tasks t LEFT JOIN projects p ON p.id = t.project_id"
# Next Actions leave out H0 tasks (they live on Horizons), recurring tasks until their due day, and tasks of waiting/someday projects.
# Open tasks need a project unless deliberately marked H0 (ground level); later steps always need one.
# Inbox items aren't processed yet.
ORPHAN = """t.project_id IS NULL AND (t.status = 'later' OR (t.status IN ('next', 'waiting') AND NOT t.ground))"""
READY = """t.status = 'next' AND (t.recur IS NULL OR t.due_date IS NULL OR t.due_date <= ?)"""
VISIBLE_NEXT = f"""{READY} AND NOT t.ground
  AND (t.project_id IS NULL OR t.project_id IN (SELECT id FROM projects WHERE status = 'active'))"""
VISIBLE_H0 = f"{READY} AND t.ground"
STATUS_ORDER = "CASE t.status WHEN 'next' THEN 0 WHEN 'waiting' THEN 1 WHEN 'later' THEN 2 ELSE 3 END"


def connect(path=None):
    c = sqlite3.connect(path or DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.execute("PRAGMA busy_timeout = 5000")  # gunicorn threads share the file; wait for a lock instead of failing
    c.execute("PRAGMA journal_mode = WAL")
    _migrate(c)
    c.executescript(SCHEMA)
    return c


def _migrate(c):
    """Rebuild tables whose status CHECK predates the current one (SQLite can't alter a CHECK)."""
    old = dict(c.execute("SELECT name, sql FROM sqlite_master WHERE type = 'table'").fetchall())
    if "tasks" in old and "waiting_on" in [r[1] for r in c.execute("PRAGMA table_info(tasks)")]:
        # The "waiting on" field was dropped; whoever it named now lives in the notes.
        c.executescript("""BEGIN; UPDATE tasks SET notes = CASE WHEN notes = '' THEN '' ELSE notes || char(10) || char(10) END
            || 'Waiting on: ' || trim(waiting_on) WHERE trim(COALESCE(waiting_on, '')) != '';
            ALTER TABLE tasks DROP COLUMN waiting_on; COMMIT;""")
    if "tasks" in old and "context" in [r[1] for r in c.execute("PRAGMA table_info(tasks)")]:
        # Contexts were removed: nobody filtered by them, and picking one was a chore.
        c.execute("ALTER TABLE tasks DROP COLUMN context")
    if "contexts" in old:
        c.execute("DROP TABLE contexts")
    steps = []
    if "projects" in old and "'waiting'" not in old["projects"]:
        steps += [PROJECTS_TABLE.format(name="projects_new"), "INSERT INTO projects_new SELECT * FROM projects",
                  "DROP TABLE projects", "ALTER TABLE projects_new RENAME TO projects"]
    if "tasks" in old and "'someday'" in old["tasks"]:
        # Someday tasks inside a project become later steps; loose ones go back to the inbox.
        steps += [TASKS_TABLE.format(name="tasks_new"), """INSERT INTO tasks_new (id, title, notes, status,
            project_id, due_date, recur, created_at, completed_at) SELECT id, title, notes,
            CASE WHEN status != 'someday' THEN status WHEN project_id IS NULL THEN 'inbox' ELSE 'later' END,
            project_id, due_date, recur, created_at, completed_at FROM tasks""",
                  "DROP TABLE tasks", "ALTER TABLE tasks_new RENAME TO tasks"]
    if steps:
        # Foreign keys off, or dropping projects would null every task's project_id.
        c.execute("PRAGMA foreign_keys = OFF")
        c.executescript("BEGIN; " + "; ".join(steps) + "; COMMIT;")
        c.execute("PRAGMA foreign_keys = ON")
    if "time_entries" in old and "task_id" not in old["time_entries"]:
        # The first timer kept free-text task names; that history was dropped when entries became tasks' time.
        c.execute("DROP TABLE time_entries")
    if "tasks" in old and "ground" not in [r[1] for r in c.execute("PRAGMA table_info(tasks)")]:
        # Before the H0 flag, open repeating tasks without a project were ground level implicitly.
        c.executescript("""BEGIN; ALTER TABLE tasks ADD COLUMN ground INTEGER NOT NULL DEFAULT 0;
            UPDATE tasks SET ground = 1 WHERE recur IS NOT NULL AND project_id IS NULL AND status != 'done'; COMMIT;""")
    if "tasks" in old and "position" not in [r[1] for r in c.execute("PRAGMA table_info(tasks)")]:
        # Project steps became draggable; number them in the order the project page used to show.
        c.executescript(f"""BEGIN; ALTER TABLE tasks ADD COLUMN position INTEGER;
            UPDATE tasks SET position = o.n FROM (SELECT id, row_number() OVER (PARTITION BY project_id ORDER BY
              {STATUS_ORDER}, created_at, id) AS n FROM tasks t WHERE project_id IS NOT NULL) o WHERE o.id = tasks.id;
            COMMIT;""")
    tasks_sql = c.execute("SELECT sql FROM sqlite_master WHERE name = 'tasks'").fetchone()
    if tasks_sql and "'abandoned'" not in tasks_sql[0]:
        # Deleting a task became abandoning it. Foreign keys off, or dropping tasks would delete their time.
        cols = ", ".join(r[1] for r in c.execute("PRAGMA table_info(tasks)"))
        c.execute("PRAGMA foreign_keys = OFF")
        c.executescript(f"""BEGIN; {TASKS_TABLE.format(name="tasks_new")};
            INSERT INTO tasks_new ({cols}) SELECT {cols} FROM tasks;
            DROP TABLE tasks; ALTER TABLE tasks_new RENAME TO tasks; COMMIT;""")
        c.execute("PRAGMA foreign_keys = ON")


def _insert(c, table, allowed, fields):
    fields = {k: v for k, v in fields.items() if k in allowed}
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    cur = c.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(fields.values()))
    c.commit()
    return cur.lastrowid


def _update(c, table, allowed, id, fields):
    unknown = set(fields) - allowed
    if unknown:
        raise ValueError(f"unknown fields: {unknown}")
    if fields:
        sets = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE {table} SET {sets} WHERE id = ?", [*fields.values(), id])
        c.commit()


# --- tasks -----------------------------------------------------------------

def add_task(c, title, **fields):
    id = _insert(c, "tasks", TASK_FIELDS, {"title": title, **fields})
    _one_next(c, id)
    return id


def update_task(c, id, **fields):
    """Returns the titles of project tasks demoted to later because this one became next."""
    _update(c, "tasks", TASK_FIELDS, id, fields)
    return _one_next(c, id)


def _one_next(c, id):
    """A project has at most one next action: if this task is it, the project's other next tasks become later.
    Returns the titles it demoted."""
    t = get_task(c, id)
    if not t or t["status"] != "next" or not t["project_id"]:
        return []
    others = c.execute("SELECT id, title FROM tasks WHERE project_id = ? AND status = 'next' AND id != ?",
                       (t["project_id"], id)).fetchall()
    c.executemany("UPDATE tasks SET status = 'later' WHERE id = ?", [(o["id"],) for o in others])
    c.commit()
    return [o["title"] for o in others]


def project_next(c, project_id):
    return c.execute("SELECT * FROM tasks WHERE project_id = ? AND status = 'next'", (project_id,)).fetchone()


def get_task(c, id):
    return c.execute(f"{TASK_SELECT} WHERE t.id = ?", (id,)).fetchone()


def delete_task(c, id):
    """Hard delete, time included. The UI abandons a task first; only an abandoned one is deleted for good."""
    c.execute("DELETE FROM tasks WHERE id = ?", (id,))
    c.commit()


def abandon_task(c, id, now=None):
    """A soft delete (stopping its timer): the task leaves every list but stays on Done, revivable."""
    running = timer_running(c)
    if running and running["task_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    c.execute("UPDATE tasks SET status = 'abandoned', completed_at = datetime('now') WHERE id = ?", (id,))
    c.commit()


def revive_task(c, id):
    """An abandoned task goes back to the inbox (keeping its project) to be processed again."""
    c.execute("UPDATE tasks SET status = 'inbox', completed_at = NULL WHERE id = ? AND status = 'abandoned'", (id,))
    c.commit()


def complete_task(c, id, today, now=None):
    """Mark done (stopping its timer); a recurring task spawns its next instance. Returns the new task id or None."""
    t = get_task(c, id)
    if t is None or t["status"] in ("done", "abandoned"):
        return None
    running = timer_running(c)
    if running and running["task_id"] == id:
        timer_stop(c, now or datetime.now(timezone.utc))
    c.execute("UPDATE tasks SET status = 'done', completed_at = datetime('now') WHERE id = ?", (id,))
    new_id = None
    if recur.parse(t["recur"]):
        due = date.fromisoformat(t["due_date"]) if t["due_date"] else None
        new_id = add_task(c, t["title"], notes=t["notes"], status=t["status"], project_id=t["project_id"],
                          ground=t["ground"], recur=t["recur"],
                          due_date=recur.next_due(t["recur"], due, today).isoformat())
    c.commit()
    return new_id


def tasks_with_status(c, status):
    return c.execute(f"{TASK_SELECT} WHERE t.status = ? ORDER BY t.created_at, t.id", (status,)).fetchall()


def next_actions(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE {VISIBLE_NEXT}
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at, t.id""", (today.isoformat(),)).fetchall()


def h0_actions(c):
    """Every H0 next action, repeats not yet due included, soonest due first; undated ones last."""
    return c.execute(f"""{TASK_SELECT} WHERE t.status = 'next' AND t.ground
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at, t.id""").fetchall()


def next_list(c):
    """The Next tab: every next action outside H0 and waiting/someday projects, repeats not yet due included,
    soonest due first; undated ones last."""
    return c.execute(f"""{TASK_SELECT} WHERE t.status = 'next' AND NOT t.ground
        AND (t.project_id IS NULL OR t.project_id IN (SELECT id FROM projects WHERE status = 'active'))
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at, t.id""").fetchall()


def overdue(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE t.status IN ('next', 'waiting') AND t.due_date < ?
        ORDER BY t.due_date""", (today.isoformat(),)).fetchall()


def orphans(c):
    return c.execute(f"{TASK_SELECT} WHERE {ORPHAN} ORDER BY t.created_at, t.id").fetchall()


def ground_level(c):
    """H0: open tasks deliberately kept outside any project."""
    return c.execute(f"""{TASK_SELECT} WHERE t.status NOT IN ('done', 'abandoned') AND t.ground
        ORDER BY t.due_date IS NULL, t.due_date, lower(t.title)""").fetchall()


def project_tasks(c, project_id):
    """Open steps in the order dragged on the project page; steps never dragged go last, oldest first."""
    return c.execute(f"""{TASK_SELECT} WHERE t.project_id = ? AND t.status NOT IN ('done', 'abandoned')
        ORDER BY t.position IS NULL, t.position, t.created_at, t.id""", (project_id,)).fetchall()


def reorder_project(c, project_id, ids):
    """Number the project's tasks in the given order; ids from other projects are ignored."""
    c.executemany("UPDATE tasks SET position = ? WHERE id = ? AND project_id = ?",
                  [(n, id, project_id) for n, id in enumerate(ids)])
    c.commit()


def counts(c, today):
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    return {
        "inbox": one("SELECT count(*) FROM tasks WHERE status = 'inbox'"),
        "next": one(f"SELECT count(*) FROM tasks t WHERE {VISIBLE_NEXT}", today.isoformat()),
        "h0": one(f"SELECT count(*) FROM tasks t WHERE {VISIBLE_H0}", today.isoformat()),
        "waiting": one("SELECT count(*) FROM tasks WHERE status = 'waiting'")
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
    tasks = c.execute(f"""{TASK_SELECT} WHERE t.status NOT IN ('done', 'abandoned') AND {where} ORDER BY
        CASE t.status WHEN 'inbox' THEN 0 WHEN 'next' THEN 1 WHEN 'waiting' THEN 2 ELSE 3 END,
        lower(t.title)""", args).fetchall()
    done = c.execute(f"""{TASK_SELECT} WHERE t.status IN ('done', 'abandoned') AND {where}
        ORDER BY t.completed_at DESC LIMIT 50""", args).fetchall()
    where, args = _matching(q, ["title", "notes"])
    projects = c.execute(f"""SELECT * FROM projects WHERE {where}
        ORDER BY status = 'done', lower(title)""", args).fetchall()
    horizons = c.execute(f"SELECT * FROM horizons WHERE {where} ORDER BY lower(title)", args).fetchall()
    references = c.execute(f"SELECT * FROM reference WHERE {where} ORDER BY lower(title)", args).fetchall()
    return {"tasks": tasks, "done": done, "projects": projects, "horizons": horizons, "references": references}


def suggest(c, q, limit=10):
    """The best fuzzy title matches among tasks, projects, horizons and reference; open items before done ones."""
    rows = [("task", r) for r in c.execute(f"{TASK_SELECT} WHERE t.status NOT IN ('done', 'abandoned')")]
    rows += [("task", r) for r in c.execute(f"{TASK_SELECT} WHERE t.status IN ('done', 'abandoned') ORDER BY t.completed_at DESC LIMIT 200")]
    rows += [("project", r) for r in c.execute("SELECT * FROM projects")]
    rows += [("horizon", r) for r in c.execute("SELECT * FROM horizons")]
    rows += [("reference", r) for r in c.execute("SELECT * FROM reference")]
    hits = [(fuzzy.score(q, r["title"]), kind, r) for kind, r in rows]
    hits = [h for h in hits if h[0] is not None]
    closed = lambda kind, r: kind in ("task", "project") and r["status"] in ("done", "abandoned")
    hits.sort(key=lambda h: (closed(h[1], h[2]), -h[0], h[2]["title"].lower()))
    return [(kind, r) for _, kind, r in hits[:limit]]


# --- projects --------------------------------------------------------------

def add_project(c, title, **fields):
    return _insert(c, "projects", PROJECT_FIELDS, {"title": title, **fields})


def update_project(c, id, **fields):
    if fields.get("status") == "done":
        c.execute("UPDATE projects SET completed_at = datetime('now') WHERE id = ?", (id,))
    _update(c, "projects", PROJECT_FIELDS, id, fields)


def delete_project(c, id):
    """Only an empty project (no tasks at all, done ones included) can be deleted."""
    if c.execute("SELECT 1 FROM tasks WHERE project_id = ?", (id,)).fetchone():
        return False
    c.execute("DELETE FROM projects WHERE id = ?", (id,))
    c.commit()
    return True


def get_project(c, id):
    return c.execute("SELECT * FROM projects WHERE id = ?", (id,)).fetchone()


def projects(c, status="active"):
    return c.execute("""SELECT p.*,
          (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'next') AS next_count,
          (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status NOT IN ('done', 'abandoned')) AS open_count,
          (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'done') AS done_count
        FROM projects p WHERE p.status = ? ORDER BY lower(p.title)""", (status,)).fetchall()


def open_steps(c, status="active"):
    """{project id: its open steps in project-page order}, for every project with the given status."""
    steps = {}
    for t in c.execute(f"""{TASK_SELECT} WHERE p.status = ? AND t.status NOT IN ('done', 'abandoned')
            ORDER BY t.position IS NULL, t.position, t.created_at, t.id""", (status,)):
        steps.setdefault(t["project_id"], []).append(t)
    return steps


def done_tasks(c, limit=100, project_id=None, ground=None, status="done"):
    """Finished (or, with status="abandoned", abandoned) tasks, newest first; optionally only one project's,
    or only H0 (ground=1) / non-H0 (ground=0)."""
    where, args = "t.status = ?", [status]
    if project_id is not None:
        where += " AND t.project_id = ?"
        args.append(project_id)
    if ground is not None:
        where += " AND t.ground = ?"
        args.append(ground)
    return c.execute(f"{TASK_SELECT} WHERE {where} ORDER BY t.completed_at DESC, t.id DESC LIMIT ?",
                     (*args, limit)).fetchall()


def open_projects(c):
    """Active then someday projects, for pickers."""
    return c.execute("""SELECT * FROM projects WHERE status != 'done'
        ORDER BY status, lower(title)""").fetchall()


def stalled_projects(c):
    """Active projects with no next action -- the weekly review's main target."""
    return [p for p in projects(c) if p["next_count"] == 0]


# --- calendar --------------------------------------------------------------

def dated_tasks(c, start, end):
    return c.execute(f"""{TASK_SELECT} WHERE t.status NOT IN ('done', 'abandoned') AND t.due_date BETWEEN ? AND ?
        ORDER BY t.due_date, t.created_at""", (start.isoformat(), end.isoformat())).fetchall()


def recurring_tasks(c):
    return c.execute(f"""{TASK_SELECT} WHERE t.status NOT IN ('done', 'abandoned') AND t.recur IS NOT NULL
        AND t.due_date IS NOT NULL""").fetchall()


# --- horizons (H5 purpose .. H2 areas) -------------------------------------

HORIZON_FIELDS = {"level", "title", "notes"}
SINGLE_HORIZONS = ("purpose", "vision")  # exactly one note each; can't be deleted


def horizons(c):
    rows = c.execute("SELECT * FROM horizons ORDER BY lower(title)").fetchall()
    out = {level: [] for level in ("purpose", "vision", "goal", "area")}
    for h in rows:
        out[h["level"]].append(h)
    return out


def add_horizon(c, level, title):
    return _insert(c, "horizons", HORIZON_FIELDS, {"level": level, "title": title})


def update_horizon(c, id, **fields):
    _update(c, "horizons", HORIZON_FIELDS - {"level"}, id, fields)


def get_horizon(c, id):
    return c.execute("SELECT * FROM horizons WHERE id = ?", (id,)).fetchone()


def delete_horizon(c, id):
    c.execute("DELETE FROM horizons WHERE id = ? AND level NOT IN ('purpose', 'vision')", (id,))
    c.commit()


# --- reference -------------------------------------------------------------

REFERENCE_FIELDS = {"title", "notes"}


def references(c):
    return c.execute("SELECT * FROM reference ORDER BY lower(title)").fetchall()


def add_reference(c, title, **fields):
    return _insert(c, "reference", REFERENCE_FIELDS, {"title": title, **fields})


def update_reference(c, id, **fields):
    _update(c, "reference", REFERENCE_FIELDS, id, fields)


def get_reference(c, id):
    return c.execute("SELECT * FROM reference WHERE id = ?", (id,)).fetchone()


def delete_reference(c, id):
    c.execute("DELETE FROM reference WHERE id = ?", (id,))
    c.commit()


# --- weekly review ---------------------------------------------------------

def review_checked(c):
    return {r[0] for r in c.execute("SELECT step FROM review_checks")}


def toggle_review_step(c, step):
    if not c.execute("DELETE FROM review_checks WHERE step = ?", (step,)).rowcount:
        c.execute("INSERT INTO review_checks (step) VALUES (?)", (step,))
    c.commit()


def complete_review(c, today):
    """Record a finished review and clear the ticks for next week."""
    c.execute("INSERT INTO reviews (completed_on) VALUES (?)", (today.isoformat(),))
    c.execute("DELETE FROM review_checks")
    c.commit()


def reviews(c):
    return [r[0] for r in c.execute("SELECT completed_on FROM reviews ORDER BY completed_on DESC, id DESC")]


# --- timer -----------------------------------------------------------------

ENTRY_FIELDS = {"task_id", "start_time", "end_time"}
ENTRY_SELECT = """SELECT e.*, t.title, t.status AS task_status, t.ground, t.project_id, p.title AS project_title
  FROM time_entries e JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = t.project_id"""
SECONDS = "(julianday(coalesce(e.end_time, ?)) - julianday(e.start_time)) * 86400"


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def timer_running(c):
    return c.execute(f"{ENTRY_SELECT} WHERE e.end_time IS NULL").fetchone()


def time_entries(c, limit=200):
    """The running entry first, then the rest, newest first."""
    return c.execute(f"{ENTRY_SELECT} ORDER BY e.end_time IS NOT NULL, e.start_time DESC LIMIT ?", (limit,)).fetchall()


def get_entry(c, id):
    return c.execute("SELECT * FROM time_entries WHERE id = ?", (id,)).fetchone()


def timer_start(c, task_id, now):
    """Start timing a task, stopping whatever was running. Returns the new entry's id."""
    timer_stop(c, now)
    return _insert(c, "time_entries", ENTRY_FIELDS, {"task_id": task_id, "start_time": iso(now)})


def timer_stop(c, now):
    c.execute("UPDATE time_entries SET end_time = ? WHERE end_time IS NULL", (iso(now),))
    c.commit()


def update_entry(c, id, **fields):
    _update(c, "time_entries", ENTRY_FIELDS, id, fields)


def delete_entry(c, id):
    c.execute("DELETE FROM time_entries WHERE id = ?", (id,))
    c.commit()


def task_seconds(c, task_id, now):
    return c.execute(f"SELECT coalesce(sum({SECONDS}), 0) FROM time_entries e WHERE e.task_id = ?",
                     (iso(now), task_id)).fetchone()[0]


def project_seconds(c, project_id, now):
    """Time on all of a project's tasks, done ones included."""
    return c.execute(f"""SELECT coalesce(sum({SECONDS}), 0) FROM time_entries e JOIN tasks t ON t.id = e.task_id
        WHERE t.project_id = ?""", (iso(now), project_id)).fetchone()[0]


def week_totals(c, since, now):
    """[(project title, or H0 / inbox / no project, seconds)] for entries started since `since`, most time first."""
    rows = c.execute(f"""SELECT CASE WHEN p.title IS NOT NULL THEN p.title WHEN t.ground THEN 'H0 · ground level'
          WHEN t.status = 'inbox' THEN 'Inbox (unprocessed)' ELSE '(no project)' END AS label, sum({SECONDS}) AS secs
        FROM time_entries e JOIN tasks t ON t.id = e.task_id LEFT JOIN projects p ON p.id = t.project_id
        WHERE e.start_time >= ? GROUP BY label ORDER BY secs DESC""", (iso(now), iso(since))).fetchall()
    return [(r["label"], round(r["secs"])) for r in rows]


def open_tasks(c):
    """Every open task, for the timer's picker: next actions first."""
    return c.execute(f"""{TASK_SELECT} WHERE t.status NOT IN ('done', 'abandoned') ORDER BY {STATUS_ORDER}, lower(t.title)""").fetchall()
