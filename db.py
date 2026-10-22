import os
import sqlite3
from datetime import date

import recur

DB_PATH = os.environ.get("GTD_DB", os.path.join(os.path.dirname(os.path.abspath(__file__)), "gtd.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'someday', 'done')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
CREATE TABLE IF NOT EXISTS tasks (
  id INTEGER PRIMARY KEY,
  title TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'inbox' CHECK (status IN ('inbox', 'next', 'waiting', 'someday', 'done')),
  context TEXT,
  project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL,
  due_date TEXT,
  recur TEXT,
  waiting_on TEXT,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  completed_at TEXT
);
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
CREATE TABLE IF NOT EXISTS contexts (name TEXT PRIMARY KEY);
-- Any context an open task uses is always on the list.
INSERT OR IGNORE INTO contexts SELECT DISTINCT context FROM tasks
  WHERE context IS NOT NULL AND context != '' AND status != 'done';
"""

TASK_FIELDS = {"title", "notes", "status", "context", "project_id", "due_date", "recur", "waiting_on"}
PROJECT_FIELDS = {"title", "notes", "status"}
TASK_SELECT = "SELECT t.*, p.title AS project_title FROM tasks t LEFT JOIN projects p ON p.id = t.project_id"
# Recurring tasks stay out of Next Actions until their due day.
VISIBLE_NEXT = "t.status = 'next' AND (t.recur IS NULL OR t.due_date IS NULL OR t.due_date <= ?)"


def connect(path=None):
    c = sqlite3.connect(path or DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    c.executescript(SCHEMA)
    return c


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
    return _insert(c, "tasks", TASK_FIELDS, {"title": title, **fields})


def update_task(c, id, **fields):
    _update(c, "tasks", TASK_FIELDS, id, fields)


def get_task(c, id):
    return c.execute(f"{TASK_SELECT} WHERE t.id = ?", (id,)).fetchone()


def delete_task(c, id):
    c.execute("DELETE FROM tasks WHERE id = ?", (id,))
    c.commit()


def complete_task(c, id, today):
    """Mark done; a recurring task spawns its next instance. Returns the new task id or None."""
    t = get_task(c, id)
    if t is None or t["status"] == "done":
        return None
    c.execute("UPDATE tasks SET status = 'done', completed_at = datetime('now') WHERE id = ?", (id,))
    new_id = None
    if recur.parse(t["recur"]):
        due = date.fromisoformat(t["due_date"]) if t["due_date"] else None
        new_id = add_task(c, t["title"], notes=t["notes"], status=t["status"], context=t["context"],
                          project_id=t["project_id"], recur=t["recur"],
                          due_date=recur.next_due(t["recur"], due, today).isoformat())
    c.commit()
    return new_id


def tasks_with_status(c, status):
    return c.execute(f"{TASK_SELECT} WHERE t.status = ? ORDER BY t.created_at, t.id", (status,)).fetchall()


def next_actions(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE {VISIBLE_NEXT}
        ORDER BY t.due_date IS NULL, t.due_date, t.created_at, t.id""", (today.isoformat(),)).fetchall()


def upcoming_recurring(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE t.status = 'next' AND t.recur IS NOT NULL
        AND t.due_date > ? ORDER BY t.due_date""", (today.isoformat(),)).fetchall()


def overdue(c, today):
    return c.execute(f"""{TASK_SELECT} WHERE t.status IN ('next', 'waiting') AND t.due_date < ?
        ORDER BY t.due_date""", (today.isoformat(),)).fetchall()


def project_tasks(c, project_id):
    return c.execute(f"""{TASK_SELECT} WHERE t.project_id = ? AND t.status != 'done'
        ORDER BY CASE t.status WHEN 'next' THEN 0 WHEN 'waiting' THEN 1 WHEN 'inbox' THEN 2 ELSE 3 END,
        t.created_at, t.id""", (project_id,)).fetchall()


def counts(c, today):
    one = lambda sql, *a: c.execute(sql, a).fetchone()[0]
    return {
        "inbox": one("SELECT count(*) FROM tasks WHERE status = 'inbox'"),
        "next": one(f"SELECT count(*) FROM tasks t WHERE {VISIBLE_NEXT}", today.isoformat()),
        "waiting": one("SELECT count(*) FROM tasks WHERE status = 'waiting'"),
        "someday": one("SELECT count(*) FROM tasks WHERE status = 'someday'")
                   + one("SELECT count(*) FROM projects WHERE status = 'someday'"),
        "stalled": len(stalled_projects(c)),
    }


# --- projects --------------------------------------------------------------

def add_project(c, title, **fields):
    return _insert(c, "projects", PROJECT_FIELDS, {"title": title, **fields})


def update_project(c, id, **fields):
    if fields.get("status") == "done":
        c.execute("UPDATE projects SET completed_at = datetime('now') WHERE id = ?", (id,))
    _update(c, "projects", PROJECT_FIELDS, id, fields)


def get_project(c, id):
    return c.execute("SELECT * FROM projects WHERE id = ?", (id,)).fetchone()


def projects(c, status="active"):
    return c.execute("""SELECT p.*,
          (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'next') AS next_count,
          (SELECT count(*) FROM tasks t WHERE t.project_id = p.id AND t.status != 'done') AS open_count
        FROM projects p WHERE p.status = ? ORDER BY lower(p.title)""", (status,)).fetchall()


def stalled_projects(c):
    """Active projects with no next action -- the weekly review's main target."""
    return [p for p in projects(c) if p["next_count"] == 0]


# --- calendar --------------------------------------------------------------

def dated_tasks(c, start, end):
    return c.execute(f"""{TASK_SELECT} WHERE t.status != 'done' AND t.due_date BETWEEN ? AND ?
        ORDER BY t.due_date, t.created_at""", (start.isoformat(), end.isoformat())).fetchall()


def recurring_tasks(c):
    return c.execute(f"""{TASK_SELECT} WHERE t.status != 'done' AND t.recur IS NOT NULL
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


# --- contexts --------------------------------------------------------------

def contexts(c):
    return [r[0] for r in c.execute("SELECT name FROM contexts ORDER BY name")]


def context_name(raw):
    """'deep work' -> '@deep-work'."""
    name = "-".join((raw or "").strip().lstrip("@").split())
    return f"@{name}" if name else None


def add_context(c, name):
    c.execute("INSERT OR IGNORE INTO contexts (name) VALUES (?)", (name,))
    c.commit()


def context_in_use(c, name):
    return c.execute("SELECT count(*) FROM tasks WHERE context = ? AND status != 'done'", (name,)).fetchone()[0]


def delete_context(c, name):
    """Remove a context unless open tasks still use it; returns how many do."""
    used = context_in_use(c, name)
    if not used:
        c.execute("DELETE FROM contexts WHERE name = ?", (name,))
        c.commit()
    return used
