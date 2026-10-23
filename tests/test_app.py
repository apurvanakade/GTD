from datetime import date, timedelta

import pytest

import dates
import db
import import_mindwtr
import recur
from app import app, local_input
from conftest import NOW, TODAY


# --- recur -----------------------------------------------------------------

@pytest.mark.parametrize("rule,expected", [
    ("weekly", (1, "week")), ("every 2 weeks", (2, "week")), ("every 6 months", (6, "month")),
    ("Daily", (1, "day")), ("every year", (1, "year")), ("every 0 days", None), ("fortnightly", None),
    ("every 2 weeks after done", (2, "week")), ("weekly after completion", None),
    ("every 12 months", (12, "month")), ("Every  10  Days ", (10, "day")),
])
def test_parse(rule, expected):
    assert recur.parse(rule) == expected


def test_next_due_keeps_anchor_and_skips_past():
    assert recur.next_due("weekly", date(2026, 10, 25), TODAY) == date(2026, 11, 1)
    assert recur.next_due("weekly", date(2026, 10, 1), TODAY) == date(2026, 10, 29)  # overdue: skip ahead
    assert recur.next_due("monthly", date(2026, 1, 31), date(2026, 1, 31)) == date(2026, 2, 28)
    assert recur.next_due("every 6 months", None, TODAY) == date(2027, 4, 22)


def test_after_done_counts_from_completion():
    rule = "every 2 weeks after done"
    assert recur.next_due(rule, date(2026, 10, 1), TODAY) == date(2026, 11, 5)  # from today, not the overdue due date
    assert recur.next_due(rule, date(2026, 12, 1), TODAY) == date(2026, 11, 5)  # even if completed early
    assert list(recur.occurrences(rule, date(2026, 10, 22), date(2027, 3, 1))) == []


# --- inbox processing ------------------------------------------------------

def test_capture_and_process_to_next(client, conn):
    client.post("/capture", data={"title": "call dentist"})
    t = db.tasks_with_status(conn, "inbox")[0]
    # context is required for a next action
    client.post(f"/process/{t['id']}", data={"action": "next", "title": "call dentist", "context": ""})
    assert db.get_task(conn, t["id"])["status"] == "inbox"
    pid = db.add_project(conn, "Teeth")
    client.post(f"/process/{t['id']}", data={"action": "next", "title": "Call dentist", "context": "@calls",
                                             "project_id": str(pid), "due_date": ""})
    row = db.get_task(conn, t["id"])
    assert (row["status"], row["context"], row["title"]) == ("next", "@calls", "Call dentist")
    assert b"Call dentist" in client.get("/next?ctx=@calls").data


def test_capture_box_only_on_inbox(client, conn):
    tid = db.add_task(conn, "x")
    pid = db.add_project(conn, "P")
    assert b'<input name="title" placeholder="Capture' in client.get("/inbox").data
    assert b"<dialog" not in client.get("/inbox").data
    for url in ("/next", f"/tasks/{tid}", f"/projects/{pid}", "/process"):
        page = client.get(url).data
        assert b"<dialog" in page and b"Capture to inbox" not in page, url
    r = client.post("/capture", data={"title": "buy milk"}, headers={"Referer": "/next"}, follow_redirects=True)
    assert b"Added to inbox: buy milk" in r.data


def test_process_other_actions(client, conn):
    ids = [db.add_task(conn, n) for n in ("a", "b", "c", "d", "e")]
    client.post(f"/process/{ids[0]}", data={"action": "trash"})
    client.post(f"/process/{ids[1]}", data={"action": "someday"})  # must say where it goes
    assert db.get_task(conn, ids[1])["status"] == "inbox"
    client.post(f"/process/{ids[1]}", data={"action": "someday", "someday_target": "new"})
    client.post(f"/process/{ids[2]}", data={"action": "done"})
    pid = db.add_project(conn, "Deck")
    client.post(f"/process/{ids[3]}", data={"action": "waiting", "project_id": str(pid)})
    r = client.post(f"/process/{ids[4]}", data={"action": "project", "title": "Fix porch"})
    assert db.get_task(conn, ids[0]) is None
    assert db.get_task(conn, ids[1]) is None
    assert [p["title"] for p in db.projects(conn, "someday")] == ["b"]
    assert db.get_task(conn, ids[2])["status"] == "done"
    assert db.get_task(conn, ids[3])["status"] == "waiting"
    assert db.get_task(conn, ids[4]) is None
    assert "/projects/" in r.headers["Location"]
    assert [p["title"] for p in db.stalled_projects(conn)] == ["Deck", "Fix porch"]


def test_process_someday_into_existing_project(client, conn):
    pid = db.add_project(conn, "Garden", status="someday")
    tid = db.add_task(conn, "build a pond")
    client.post(f"/process/{tid}", data={"action": "someday", "someday_target": str(pid), "title": "Build a pond"})
    t = db.get_task(conn, tid)
    assert (t["title"], t["status"], t["project_id"]) == ("Build a pond", "later", pid)
    assert [p["title"] for p in db.projects(conn, "someday")] == ["Garden"]  # no new project


def test_all_pages_render(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "t", status="next", context="@home", project_id=pid, due_date="2026-10-01")
    db.add_task(conn, "inboxed")
    for url in ("/inbox", "/process", "/next", "/projects", f"/projects/{pid}", "/waiting",
                "/someday", "/review", f"/tasks/{tid}", "/calendar", "/calendar?m=2027-01",
                "/calendar?m=bogus", "/horizons"):
        assert client.get(url).status_code == 200, url


def test_crosslinks(client, conn):
    pid = db.add_project(conn, "Garden")
    tid = db.add_task(conn, "dig", status="next", context="@home", project_id=pid)
    gid = db.add_task(conn, "stretch", status="next", ground=1)
    assert f'href="/projects/{pid}"' in client.get(f"/tasks/{tid}").get_data(as_text=True)
    assert 'href="/horizons"' in client.get(f"/tasks/{gid}").get_data(as_text=True)
    assert 'href="/projects"' in client.get(f"/projects/{pid}").get_data(as_text=True)
    db.update_project(conn, pid, status="someday")
    assert 'href="/someday"' in client.get(f"/projects/{pid}").get_data(as_text=True)
    assert 'href="/next?ctx=' in client.get(f"/projects/{pid}").get_data(as_text=True)


def test_move_project_to_someday_from_task(client, conn):
    pid = db.add_project(conn, "Garden")
    db.update_project(conn, pid, notes="seed list")
    tid = db.add_task(conn, "dig", status="next", context="@home", project_id=pid)
    assert "Move project to someday" in client.get(f"/tasks/{tid}").get_data(as_text=True)
    r = client.post(f"/projects/{pid}", data={"status": "someday"})
    assert r.headers["Location"].endswith(f"/projects/{pid}")  # stays on the project
    p = db.get_project(conn, pid)
    assert (p["status"], p["title"], p["notes"]) == ("someday", "Garden", "seed list")  # notes kept
    assert "Move project to someday" not in client.get(f"/tasks/{tid}").get_data(as_text=True)


# --- recurring + projects --------------------------------------------------

def test_recurring_complete_spawns_hidden_next(client, conn):
    tid = db.add_task(conn, "vacuum", status="next", context="@home", recur="weekly", due_date="2026-10-22")
    assert [t["id"] for t in db.next_actions(conn, TODAY)] == [tid]
    client.post(f"/tasks/{tid}/complete", headers={"X-Requested-With": "fetch"})
    assert db.next_actions(conn, TODAY) == []
    (nxt,) = db.upcoming_recurring(conn, TODAY)
    assert (nxt["due_date"], nxt["context"]) == ("2026-10-29", "@home")


def test_project_cannot_complete_with_open_tasks(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "t", status="next", project_id=pid)
    client.post(f"/projects/{pid}", data={"title": "P", "notes": "", "status": "done"})
    assert db.get_project(conn, pid)["status"] == "active"
    db.complete_task(conn, tid, TODAY)
    client.post(f"/projects/{pid}", data={"title": "P", "notes": "n", "status": "done"})
    assert db.get_project(conn, pid)["status"] == "done"


def test_one_next_action_per_project(client, conn):
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", status="next", context="@home", project_id=pid)
    b = db.add_task(conn, "b", status="next", context="@home", project_id=pid)  # b takes over
    assert [db.get_task(conn, i)["status"] for i in (a, b)] == ["later", "next"]
    client.post(f"/projects/{pid}/tasks", data={"title": "c"})  # project has a next, so c is later
    c = db.get_task(conn, b + 1)
    assert (c["title"], c["status"]) == ("c", "later")
    client.post(f"/tasks/{a}/make-next")
    assert [db.get_task(conn, i)["status"] for i in (a, b)] == ["next", "later"]
    # finishing the next action asks for the next one, even via fetch
    r = client.post(f"/tasks/{a}/complete", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/projects/{pid}")
    assert db.project_next(conn, pid) is None and [p["title"] for p in db.stalled_projects(conn)] == ["P"]
    assert b"Make next" in client.get(f"/projects/{pid}").data


def test_process_next_into_project_with_next_becomes_later(client, conn):
    pid = db.add_project(conn, "P")
    db.add_task(conn, "a", status="next", context="@home", project_id=pid)
    tid = db.add_task(conn, "b")
    client.post(f"/process/{tid}", data={"action": "next", "context": "@home", "project_id": str(pid)})
    assert db.get_task(conn, tid)["status"] == "later"


def test_waiting_and_someday_projects_hide_their_tasks(client, conn):
    pid = db.add_project(conn, "P")
    db.add_task(conn, "a", status="next", context="@home", project_id=pid)
    for status, page in (("waiting", "/waiting"), ("someday", "/someday")):
        client.post(f"/projects/{pid}", data={"title": "P", "status": status})
        assert db.next_actions(conn, TODAY) == [] and db.stalled_projects(conn) == []
        assert b"P</a>" in client.get(page).data
    client.post(f"/projects/{pid}", data={"title": "P", "status": "active"})
    assert [t["title"] for t in db.next_actions(conn, TODAY)] == ["a"]


def test_tasks_need_a_project_or_h0(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "loose", status="next", context="@home", project_id=pid)
    for status in ("next", "waiting", "later"):
        db.update_task(conn, tid, status=status, project_id=pid)
        client.post(f"/tasks/{tid}", data={"status": status, "project_id": "", "recur": "weekly"})
        assert db.get_task(conn, tid)["status"] == "inbox"  # repeating doesn't exempt it
    client.post(f"/tasks/{tid}", data={"status": "later", "project_id": "h0"})
    assert db.get_task(conn, tid)["status"] == "inbox"  # later steps always need a project
    client.post(f"/tasks/{tid}", data={"status": "next", "project_id": "h0"})
    t = db.get_task(conn, tid)
    assert (t["status"], t["ground"], t["project_id"]) == ("next", 1, None)
    assert b">H0</a>" in client.get("/horizons").data
    client.post(f"/tasks/{tid}", data={"status": "next", "project_id": str(pid)})
    assert db.get_task(conn, tid)["ground"] == 0  # picking a project clears H0

    item = db.add_task(conn, "inbox item")
    for action in ("next", "waiting"):
        client.post(f"/process/{item}", data={"action": action, "context": "@home",
                                              "project_id": "", "recur": "monthly"})
        assert db.get_task(conn, item)["status"] == "inbox"
    client.post(f"/process/{item}", data={"action": "next", "context": "@home", "project_id": "h0"})
    assert db.get_task(conn, item)["status"] == "next"
    assert [t["id"] for t in db.ground_level(conn)] == [item]
    html = client.get("/horizons").data.decode()
    assert "inbox item" in html and f'href="/projects/{pid}">P</a>' in html  # H0 tasks and H1 projects

    orphan = db.add_task(conn, "legacy", status="next")  # e.g. from before the rule
    stray = db.add_task(conn, "stray wait", status="waiting")
    db.add_task(conn, "h0 wait", status="waiting", ground=1)
    assert [t["id"] for t in db.orphans(conn)] == [orphan, stray]
    assert b"legacy" in client.get("/review").data


def test_migrates_someday_tasks(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript("""
      CREATE TABLE projects (id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'someday', 'done')),
        created_at TEXT NOT NULL DEFAULT (datetime('now')), completed_at TEXT);
      CREATE TABLE tasks (id INTEGER PRIMARY KEY, title TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'inbox' CHECK (status IN ('inbox', 'next', 'waiting', 'someday', 'done')),
        context TEXT, project_id INTEGER REFERENCES projects(id) ON DELETE SET NULL, due_date TEXT, recur TEXT,
        waiting_on TEXT, created_at TEXT NOT NULL DEFAULT (datetime('now')), completed_at TEXT);
      INSERT INTO projects (id, title) VALUES (1, 'P');
      INSERT INTO tasks (title, status, project_id) VALUES ('in project', 'someday', 1), ('loose', 'someday', NULL),
        ('n', 'next', 1);""")
    old.commit()
    old.close()
    c = db.connect(path)
    got = {r["title"]: (r["status"], r["project_id"]) for r in c.execute("SELECT * FROM tasks")}
    assert got == {"in project": ("later", 1), "loose": ("inbox", None), "n": ("next", 1)}
    db.update_project(c, 1, status="waiting")  # new CHECK is in place
    c.close()
    db.connect(path).close()  # second connect is a no-op


def test_migration_folds_waiting_on_into_notes(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(db.PROJECTS_TABLE.format(name="projects") + ";" + db.TASKS_TABLE.format(name="tasks")
                      .replace("ground INTEGER", "waiting_on TEXT, ground INTEGER") + """;
      INSERT INTO tasks (title, notes, status, waiting_on) VALUES ('bare', '', 'waiting', 'Bob'),
        ('noted', 'call back', 'waiting', 'Ann'), ('blank', 'keep', 'waiting', ''), ('none', '', 'next', NULL);""")
    old.commit()
    old.close()
    c = db.connect(path)
    assert "waiting_on" not in [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
    assert {r["title"]: r["notes"] for r in c.execute("SELECT * FROM tasks")} == {
        "bare": "Waiting on: Bob", "noted": "call back\n\nWaiting on: Ann", "blank": "keep", "none": ""}
    c.close()
    db.connect(path).close()  # second connect is a no-op


def test_migration_marks_projectless_repeats_h0(tmp_path):
    import sqlite3
    path = str(tmp_path / "old.db")
    old = sqlite3.connect(path)
    old.executescript(db.PROJECTS_TABLE.format(name="projects") + ";" + db.TASKS_TABLE.format(name="tasks")
                      .replace("ground INTEGER NOT NULL DEFAULT 0,", "") + """;
      INSERT INTO projects (id, title) VALUES (1, 'P');
      INSERT INTO tasks (title, status, recur, project_id) VALUES ('bins', 'next', 'weekly', NULL),
        ('in project', 'next', 'weekly', 1), ('one-off', 'next', NULL, NULL), ('old', 'done', 'weekly', NULL);""")
    old.commit()
    old.close()
    c = db.connect(path)
    assert {r["title"]: r["ground"] for r in c.execute("SELECT * FROM tasks")} == {
        "bins": 1, "in project": 0, "one-off": 0, "old": 0}
    c.close()


# --- calendar + horizons -------------------------------------------------

def test_occurrences_do_not_drift():
    assert list(recur.occurrences("monthly", date(2026, 1, 31), date(2026, 4, 30))) == [
        date(2026, 2, 28), date(2026, 3, 31), date(2026, 4, 30)]


def test_calendar_shows_dated_and_projected_repeats(client, conn):
    db.add_task(conn, "dentist", status="next", due_date="2026-10-28")  # in Nov grid (starts Mon Oct 26)
    db.add_task(conn, "haircut", status="next", due_date="2026-10-20")  # before the grid
    db.add_task(conn, "plants", status="next", recur="weekly", due_date="2026-10-25")
    db.add_task(conn, "undated", status="next")
    html = client.get("/calendar?m=2026-11").data.decode()
    assert "dentist" in html and "haircut" not in html
    assert html.count(">↻ plants<") == 6  # Oct 25 is before the grid; Nov 1..Dec 6 projected
    assert "undated" not in html


def test_calendar_week_view(client, conn):
    db.add_task(conn, "in week", status="next", due_date="2026-10-25")  # Sunday of the week of Thu Oct 22
    db.add_task(conn, "next week", status="next", due_date="2026-10-26")
    db.add_task(conn, "last week", status="next", due_date="2026-10-18")
    db.add_task(conn, "plants", status="next", recur="weekly", due_date="2026-10-11")
    html = client.get("/calendar?w=2026-10-22").data.decode()
    assert "in week" in html and "next week" not in html and "last week" not in html
    assert html.count(">↻ plants<") == 1  # Oct 11 weekly: Oct 25 is the only one in the week
    assert "19 Oct" in html and "25 Oct" in html
    assert "w=2026-10-26" in html and "w=2026-10-12" in html  # next / previous week
    assert client.get("/calendar?w=bogus").status_code == 200
    assert "w=2026-10-19" in client.get("/calendar").data.decode()  # month view links to this week


def test_calendar_add(client, conn):
    pid = db.add_project(conn, "Taxes")
    r = client.post("/calendar", data={"title": "file taxes", "due_date": "2027-04-15", "context": "@computer",
                                       "project_id": str(pid)})
    assert r.headers["Location"].endswith("m=2027-04")
    (t,) = db.tasks_with_status(conn, "next")
    assert (t["title"], t["due_date"], t["context"]) == ("file taxes", "2027-04-15", "@computer")
    client.post("/calendar", data={"title": "send forms", "due_date": "2027-04-01", "project_id": str(pid)})
    assert db.get_task(conn, t["id"])["status"] == "next"  # existing next action is kept
    assert [x["title"] for x in db.tasks_with_status(conn, "later")] == ["send forms"]
    client.post("/calendar", data={"title": "loose", "due_date": "2027-04-02"})
    assert [x["title"] for x in db.tasks_with_status(conn, "inbox")] == ["loose"]
    client.post("/calendar", data={"title": "bins", "due_date": "2027-04-02", "recur": "weekly", "project_id": "h0"})
    assert db.tasks_with_status(conn, "next")[-1]["title"] == "bins"


def test_horizons(client, conn):
    items = db.horizons(conn)
    assert len(items["purpose"]) == 1 and len(items["vision"]) == 1  # seeded, not duplicated
    db.connect(app.config["DB_PATH"]).close()
    assert len(db.horizons(conn)["purpose"]) == 1
    r = client.post("/horizons", data={"level": "goal", "title": "Get promoted"})
    gid = int(r.headers["Location"].rsplit("/", 1)[1])
    client.post(f"/horizons/{gid}", data={"title": "Get promoted", "notes": "Dossier by Sept"})
    assert db.get_horizon(conn, gid)["notes"] == "Dossier by Sept"
    assert client.post("/horizons", data={"level": "purpose", "title": "x"}).status_code == 400
    pid = items["purpose"][0]["id"]
    client.post(f"/horizons/{pid}/delete")
    assert db.get_horizon(conn, pid) is not None  # purpose can't be deleted
    client.post(f"/horizons/{gid}/delete")
    assert db.get_horizon(conn, gid) is None


def test_contexts(client, conn):
    assert db.contexts(conn) == []
    client.post("/contexts", data={"name": " deep work "})
    client.post("/contexts", data={"name": "@calls"})
    client.post("/contexts", data={"name": "@calls"})  # duplicate ignored
    assert db.contexts(conn) == ["@calls", "@deep-work"]
    assert b"@deep-work" in client.get("/horizons").data
    tid = db.add_task(conn, "ring Bob", status="next", context="@calls")
    client.post("/contexts/delete", data={"name": "@calls"})
    assert "@calls" in db.contexts(conn)  # still used by an open task
    db.complete_task(conn, tid, TODAY)
    client.post("/contexts/delete", data={"name": "@calls"})
    assert db.contexts(conn) == ["@deep-work"]
    db.connect(app.config["DB_PATH"]).close()
    assert db.contexts(conn) == ["@deep-work"]  # done tasks don't re-seed it


def test_contexts_seeded_from_open_tasks(tmp_path):
    c = db.connect(str(tmp_path / "s.db"))
    db.add_task(c, "a", status="next", context="@home")
    db.add_task(c, "b", status="done", context="@old")
    c.close()
    assert db.contexts(db.connect(str(tmp_path / "s.db"))) == ["@home"]


# --- import ----------------------------------------------------------------

def test_import_mapping():
    projects = [{"id": "p1", "title": "Basement", "status": "active"},
                {"id": "p2", "title": "Old", "status": "archived"}]
    tasks = [
        {"id": "1", "title": "measure", "status": "next", "contexts": ["@home", "@errands"], "projectId": "p1"},
        {"id": "2", "title": "plants", "status": "next", "contexts": ["@home"], "dueDate": "2026-10-25",
         "recurrence": {"rule": "weekly", "byDay": ["SU"], "rrule": "FREQ=WEEKLY;BYDAY=SU"}},
        {"id": "3", "title": "oil", "status": "next", "recurrence": {"rule": "monthly", "rrule": "FREQ=MONTHLY;INTERVAL=6"}},
        {"id": "4", "title": "odd", "status": "next", "recurrence": {"rule": "weekly", "rrule": "FREQ=WEEKLY;BYDAY=MO,WE"}},
        {"id": "5", "title": "ref", "status": "reference"},
        {"id": "6", "title": "gone", "status": "next", "deletedAt": "x"},
        {"id": "7", "title": "done", "status": "done"},
        {"id": "8", "title": "orphan", "status": "someday", "projectId": "p2"},
    ]
    out_p, out_t, ref, warnings = import_mindwtr.plan(tasks, projects)
    assert [p["title"] for p in out_p] == ["Basement"]
    by = {t["title"].lstrip("⚠ "): t for t in out_t}
    assert set(by) == {"measure", "plants", "oil", "odd", "orphan"}
    assert by["measure"]["context"] == "@home" and "@errands" in by["measure"]["notes"]
    assert by["measure"]["project_src"] == "p1" and by["orphan"]["project_src"] is None
    assert by["plants"]["recur"] == "every 1 week"
    assert by["oil"]["recur"] == "every 6 months"
    assert by["odd"]["recur"] is None and by["odd"]["title"].startswith("⚠")
    assert [r["title"] for r in ref] == ["ref"]
    assert len(warnings) == 2


def test_search(client, conn):
    pid = db.add_project(conn, "Kitchen remodel", notes="call the plumber")
    db.add_task(conn, "Buy tiles", status="next", context="@errands", project_id=pid)
    db.add_task(conn, "Pick tile grout", status="inbox")
    done = db.add_task(conn, "Measure kitchen tiles", status="next", project_id=pid)
    db.complete_task(conn, done, TODAY)
    db.add_task(conn, "100% sure", status="inbox")
    r = db.search(conn, "TILE")
    assert [t["title"] for t in r["tasks"]] == ["Pick tile grout", "Buy tiles"]
    assert [t["title"] for t in r["done"]] == ["Measure kitchen tiles"]
    assert [t["title"] for t in db.search(conn, "tiles kitchen")["done"]] == ["Measure kitchen tiles"]
    assert [p["title"] for p in db.search(conn, "plumber")["projects"]] == ["Kitchen remodel"]
    assert [t["title"] for t in db.search(conn, "errands")["tasks"]] == ["Buy tiles"]
    assert db.search(conn, "0%")["tasks"][0]["title"] == "100% sure"
    assert db.search(conn, "_")["tasks"] == []
    page = client.get("/search?q=tile").data
    assert b"Pick tile grout" in page and b"Measure kitchen tiles" in page
    assert b"Nothing matches" in client.get("/search?q=zzz").data
    assert client.get("/search").status_code == 200


# --- edges -----------------------------------------------------------------

def test_delete_from_task_page_after_saving(client, conn):
    tid = db.add_task(conn, "x", status="next", context="@home", ground=1)
    # After a save the task page is its own referrer, so its back link points at itself.
    r = client.post(f"/tasks/{tid}/delete", data={"next": f"http://localhost/tasks/{tid}"})
    assert db.get_task(conn, tid) is None and r.headers["Location"] == "/next"
    r = client.post(f"/tasks/{tid + 1}/delete", data={"next": "/waiting"})
    assert r.headers["Location"] == "/waiting"


def test_delete_empty_project_only(client, conn):
    empty = db.add_project(conn, "Empty")
    full = db.add_project(conn, "Full")
    db.add_task(conn, "step", status="next", context="@home", project_id=full)
    assert "Delete project" in client.get(f"/projects/{empty}").get_data(as_text=True)
    assert "Delete project" not in client.get(f"/projects/{full}").get_data(as_text=True)
    client.post(f"/projects/{full}/delete")
    assert db.get_project(conn, full) is not None
    r = client.post(f"/projects/{empty}/delete")
    assert db.get_project(conn, empty) is None and r.headers["Location"] == "/projects"


def test_review_orphan_wording(client, conn):
    html = client.get("/review").get_data(as_text=True)
    assert "Every open task has a project or is H0" in html and "repeat" not in html
    db.add_task(conn, "legacy", status="next")
    assert "mark it H0" in client.get("/review").get_data(as_text=True)


def test_review_next_actions_skip_scheduled(client, conn):
    pid = db.add_project(conn, "Trip")
    db.add_task(conn, "book flights", status="next", context="@home", project_id=pid, due_date="2026-12-01")
    db.add_task(conn, "pack bags", status="later", context="@home", project_id=pid)
    db.add_task(conn, "renew visa", status="next", context="@home", project_id=db.add_project(conn, "Visa"))
    html = client.get("/review").get_data(as_text=True)
    assert "renew visa" in html and "book flights" not in html  # listed like orphans
    assert "1 scheduled for later is left out" in html


def test_task_form_edges(client, conn):
    tid = db.add_task(conn, "t", status="next", context="@home", ground=1, notes="old", recur="weekly",
                      due_date="2026-10-22")
    r = client.post(f"/tasks/{tid}", data={"title": "", "notes": "", "recur": "fortnightly"}, follow_redirects=True)
    t = db.get_task(conn, tid)
    assert (t["title"], t["notes"], t["recur"]) == ("t", "", "weekly")  # blank title kept, bad rule ignored
    assert "understand repeat rule “fortnightly”" in r.get_data(as_text=True)
    client.post(f"/tasks/{tid}", data={"recur": "every 3 days", "due_date": ""})
    assert db.get_task(conn, tid)["due_date"] == "2026-10-22"  # a repeat needs a due date: today
    assert client.get("/tasks/999").status_code == 404
    assert client.post("/tasks/999/complete").status_code == 404


def test_make_next_without_context_asks_for_one(client, conn):
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", status="later", project_id=pid)
    r = client.post(f"/tasks/{a}/make-next", headers={"Referer": f"/projects/{pid}"})
    assert db.get_task(conn, a)["status"] == "next" and r.headers["Location"] == f"/tasks/{a}"
    b = db.add_task(conn, "b", status="later", context="@home", project_id=pid)
    r = client.post(f"/tasks/{b}/make-next", headers={"Referer": f"/projects/{pid}"})
    assert r.headers["Location"] == f"/projects/{pid}"
    assert [db.get_task(conn, i)["status"] for i in (a, b)] == ["later", "next"]


def test_complete_twice_is_harmless(client, conn):
    tid = db.add_task(conn, "bins", status="next", context="@home", ground=1, recur="weekly", due_date="2026-10-22")
    client.post(f"/tasks/{tid}/complete")
    client.post(f"/tasks/{tid}/complete")  # e.g. a double click
    assert len(db.tasks_with_status(conn, "next")) == 1  # only one new instance


def test_process_edges(client, conn):
    r = client.get("/process", follow_redirects=True)
    assert r.request.path == "/inbox" and b"Inbox zero." in r.data
    tid = db.add_task(conn, "x")
    assert client.post(f"/process/{tid}", data={"action": "bogus"}).status_code == 400
    assert client.post(f"/process/{tid}", data={}).status_code == 400
    assert client.post("/process/999", data={"action": "trash"}).status_code == 404
    client.post(f"/process/{tid}", data={"action": "someday", "someday_target": "999"})
    assert db.get_task(conn, tid)["status"] == "inbox"  # unknown project: asks again


def test_projects_create_and_status(client, conn):
    assert client.post("/projects", data={"title": "  "}).headers["Location"] == "/projects"
    r = client.post("/projects", data={"title": "Paint fence"})
    pid = int(r.headers["Location"].rsplit("/", 1)[1])
    assert db.get_project(conn, pid)["title"] == "Paint fence"
    client.post(f"/projects/{pid}", data={"title": "", "notes": "", "status": "bogus"})
    p = db.get_project(conn, pid)
    assert (p["title"], p["status"]) == ("Paint fence", "active")  # blank title and unknown status ignored
    client.post(f"/projects/{pid}", data={"status": "done"})
    assert db.get_project(conn, pid)["completed_at"]
    assert client.get("/projects/999").status_code == 404
    assert client.post("/projects/999/tasks", data={"title": "x"}).status_code == 404


def test_project_step_without_title_is_ignored(client, conn):
    pid = db.add_project(conn, "P")
    client.post(f"/projects/{pid}/tasks", data={"title": " "})
    assert db.project_tasks(conn, pid) == []
    client.post(f"/projects/{pid}/tasks", data={"title": "first"})
    assert db.project_tasks(conn, pid)[0]["status"] == "next"  # no next yet, so the first step is it


def test_capture_on_inbox_has_no_flash_and_blank_is_ignored(client, conn):
    r = client.post("/capture", data={"title": "milk"}, headers={"Referer": "http://localhost/inbox"},
                    follow_redirects=True)
    assert b"Added to inbox" not in r.data and b"milk" in r.data
    client.post("/capture", data={"title": "   "})
    assert [t["title"] for t in db.tasks_with_status(conn, "inbox")] == ["milk"]


def test_home_and_horizon_page(client, conn):
    assert client.get("/").headers["Location"] == "/next"
    gid = db.add_horizon(conn, "goal", "Run a marathon")
    page = client.get(f"/horizons/{gid}").get_data(as_text=True)
    assert "Run a marathon" in page and "H3 · Goals" in page and "Delete" in page
    vision = db.horizons(conn)["vision"][0]["id"]
    assert "Delete" not in client.get(f"/horizons/{vision}").get_data(as_text=True)
    client.post(f"/horizons/{vision}", data={"notes": "big"})  # from the Horizons page: notes only
    assert (db.get_horizon(conn, vision)["title"], db.get_horizon(conn, vision)["notes"]) == ("Vision", "big")
    assert client.get("/horizons/999").status_code == 404
    assert client.post("/horizons", data={"level": "goal", "title": " "}).status_code == 400
    assert client.post("/contexts", data={"name": " @ "}).status_code == 400
    assert client.post("/contexts/delete", data={}).status_code == 400


def test_db_rejects_unknown_fields(conn):
    tid = db.add_task(conn, "x")
    with pytest.raises(ValueError):
        db.update_task(conn, tid, colour="red")
    assert db.search(conn, "   ") == {"tasks": [], "done": [], "projects": [], "horizons": []}


def test_next_due_skipping_ahead_does_not_drift():
    # Overdue month-end and leap-day tasks keep their day when several periods are skipped.
    assert recur.next_due("monthly", date(2026, 1, 31), TODAY) == date(2026, 10, 31)
    assert recur.next_due("every 2 years", date(2024, 2, 29), TODAY) == date(2028, 2, 29)
    assert recur.next_due("yearly", date(2024, 2, 29), TODAY) == date(2027, 2, 28)



# --- timer -----------------------------------------------------------------

def iso(dt):
    return dt.isoformat(timespec="seconds")


def test_timer_starts_from_a_task_and_starting_another_stops_it(client, conn):
    a = db.add_task(conn, "write", status="next", context="@home", ground=1)
    b = db.add_task(conn, "read", status="next", context="@home", ground=1)
    assert b'action="/tasks/%d/start"' % a in client.get("/horizons").data  # ▶ on task rows
    client.post(f"/tasks/{a}/start", headers={"Referer": "/next"})
    assert db.timer_running(conn)["task_id"] == a
    page = client.get("/inbox").get_data(as_text=True)  # the running timer shows on every page
    assert 'class="running-timer"' in page and "write" in page and "0:00" in page
    client.post(f"/tasks/{b}/start")
    entries = db.time_entries(conn)
    assert [(e["task_id"], e["end_time"] is None) for e in entries] == [(b, True), (a, False)]
    client.post("/timer/stop")
    assert db.timer_running(conn) is None and 'class="running-timer"' not in client.get("/inbox").get_data(as_text=True)
    assert client.post("/tasks/999/start").status_code == 404


def test_timer_page_start_uses_a_task_or_captures_to_inbox(client, conn):
    t = db.add_task(conn, "Write report", status="next", context="@home", ground=1)
    client.post("/timer/start", data={"title": "write REPORT "})
    assert db.timer_running(conn)["task_id"] == t
    r = client.post("/timer/start", data={"title": "Yoga"}, follow_redirects=True)
    (yoga,) = db.tasks_with_status(conn, "inbox")
    assert yoga["title"] == "Yoga" and db.timer_running(conn)["task_id"] == yoga["id"]
    assert "Captured to inbox: Yoga" in r.get_data(as_text=True)
    client.post("/timer/start", data={"title": " "})
    assert db.timer_running(conn)["task_id"] == yoga["id"]  # blank: nothing changes


def test_completing_the_running_task_stops_its_timer(client, conn):
    t = db.add_task(conn, "bins", status="next", context="@home", ground=1, recur="weekly", due_date="2026-10-22")
    other = db.add_task(conn, "x", status="next", context="@home", ground=1)
    client.post(f"/tasks/{t}/start")
    client.post(f"/tasks/{other}/complete")
    assert db.timer_running(conn)["task_id"] == t  # another task: timer keeps running
    client.post(f"/tasks/{t}/complete")
    assert db.timer_running(conn) is None
    assert db.get_entry(conn, db.time_entries(conn)[0]["id"])["end_time"] == iso(NOW)


def test_deleting_a_task_deletes_its_time(client, conn):
    t = db.add_task(conn, "x", status="next", context="@home", ground=1)
    client.post(f"/tasks/{t}/start")
    client.post(f"/tasks/{t}/delete")
    assert db.time_entries(conn) == []


def test_edit_and_delete_time_entries(client, conn):
    t = db.add_task(conn, "x", status="next", context="@home", ground=1)
    eid = db.timer_start(conn, t, NOW - timedelta(hours=2, seconds=-17))
    db.timer_stop(conn, NOW - timedelta(hours=1, seconds=-5))
    before = dict(db.get_entry(conn, eid))
    start, end = (local_input(before[k]) for k in ("start_time", "end_time"))
    client.post(f"/timer/{eid}", data={"start": start, "end": end})
    assert dict(db.get_entry(conn, eid)) == before  # an unchanged save keeps the seconds
    for bad in ({"start": start, "end": local_input(iso(NOW - timedelta(hours=3)))},  # end before start
                {"start": local_input(iso(NOW + timedelta(hours=1))), "end": end},   # in the future
                {"start": "nonsense", "end": end}):
        r = client.post(f"/timer/{eid}", data=bad, follow_redirects=True)
        assert r.status_code == 200 and dict(db.get_entry(conn, eid)) == before, bad
    new_start = local_input(iso(NOW - timedelta(hours=4)))
    client.post(f"/timer/{eid}", data={"start": new_start, "end": end})
    assert local_input(db.get_entry(conn, eid)["start_time"]) == new_start
    running = db.timer_start(conn, t, NOW - timedelta(minutes=5))
    client.post(f"/timer/{running}", data={"start": local_input(iso(NOW - timedelta(minutes=5))),
                                           "end": local_input(iso(NOW - timedelta(minutes=1)))})
    assert db.timer_running(conn) is None  # an end time stops it
    client.post(f"/timer/{eid}/delete")
    assert db.get_entry(conn, eid) is None
    assert client.post("/timer/999", data={}).status_code == 404


def test_time_totals_on_task_project_and_timer_pages(client, conn):
    pid = db.add_project(conn, "Kitchen")
    a = db.add_task(conn, "measure", status="next", context="@home", project_id=pid)
    b = db.add_task(conn, "tile", status="later", project_id=pid)
    h0 = db.add_task(conn, "stretch", status="next", context="@home", ground=1)
    db.timer_start(conn, h0, NOW - timedelta(days=30))  # last month: not this week
    db.timer_stop(conn, NOW - timedelta(days=30) + timedelta(minutes=5))
    db.timer_start(conn, a, NOW - timedelta(minutes=90))
    db.timer_start(conn, b, NOW - timedelta(minutes=30))  # stops a after 60 min
    db.complete_task(conn, a, TODAY, NOW)  # done tasks still count toward the project
    db.timer_start(conn, h0, NOW - timedelta(minutes=10))  # stops b after 20 min
    assert "1h 00m" in client.get(f"/tasks/{a}").get_data(as_text=True)
    assert "1h 20m" in client.get(f"/projects/{pid}").get_data(as_text=True)
    page = client.get("/timer").get_data(as_text=True)
    assert "Kitchen" in page and "1h 20m" in page and "H0 · ground level" in page and "0h 10m" in page
    assert db.week_totals(conn, NOW - timedelta(days=1), NOW) == [("Kitchen", 4800), ("H0 · ground level", 600)]


def test_old_time_entries_table_is_replaced(tmp_path):
    import sqlite3
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE time_entries (id INTEGER PRIMARY KEY, task TEXT, area TEXT, start_time TEXT, end_time TEXT)")
    old.execute("INSERT INTO time_entries (task, area, start_time) VALUES ('x', '', '2026-10-22T10:00:00+00:00')")
    old.commit()
    old.close()
    c = db.connect(str(path))
    assert "task_id" in [r[1] for r in c.execute("PRAGMA table_info(time_entries)")]
    assert db.time_entries(c) == []


def test_new_context_from_task_form(client, conn):
    tid = db.add_task(conn, "ring Bob", status="next", project_id=db.add_project(conn, "P"))
    r = client.post(f"/tasks/{tid}", data={"context": "", "new_context": " deep work "})
    assert r.status_code in (200, 302)
    assert db.get_task(conn, tid)["context"] == "@deep-work"
    assert "@deep-work" in db.contexts(conn)
    assert b'name="new_context"' in client.get(f"/tasks/{tid}").data


def test_new_project_from_task_form(client, conn):
    tid = db.add_task(conn, "ring Bob", status="inbox")
    r = client.post(f"/tasks/{tid}", data={"project_id": "h0", "new_project": " Garden ", "status": "next",
                                           "context": "@home"})
    assert r.status_code in (200, 302)
    t = db.get_task(conn, tid)
    p = conn.execute("SELECT * FROM projects WHERE title = 'Garden'").fetchone()
    assert p and p["status"] == "active"
    assert t["project_id"] == p["id"] and not t["ground"] and t["status"] == "next"
    assert b'name="new_project"' in client.get(f"/tasks/{tid}").data


# --- natural-language dates --------------------------------------------------

@pytest.mark.parametrize("text,expected", [  # TODAY is Thursday 2026-10-22
    ("tomorrow", date(2026, 10, 23)), ("Today", TODAY), ("yesterday", date(2026, 10, 21)),
    ("saturday", date(2026, 10, 24)), ("sat", date(2026, 10, 24)), ("next saturday", date(2026, 10, 24)),
    ("thursday", date(2026, 10, 29)),  # same weekday means next week
    ("monday", date(2026, 10, 26)), ("next week", date(2026, 10, 26)), ("next month", date(2026, 11, 22)),
    ("in 3 days", date(2026, 10, 25)), ("in 2 weeks", date(2026, 11, 5)), ("in 1 month", date(2026, 11, 22)),
    ("oct 30", date(2026, 10, 30)), ("30 Oct", date(2026, 10, 30)), ("jan 5", date(2027, 1, 5)),
    ("jan 5 2028", date(2028, 1, 5)), ("2026-12-01", date(2026, 12, 1)),
    ("feb 30", None), ("blah", None), ("", None),
])
def test_natural_dates(text, expected):
    assert dates.parse(text, TODAY) == expected


def test_task_form_accepts_words_for_due_date(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "call", status="next", project_id=pid)
    client.post(f"/tasks/{tid}", data={"title": "call", "due_date": "saturday"})
    assert db.get_task(conn, tid)["due_date"] == "2026-10-24"
    client.post(f"/tasks/{tid}", data={"title": "call", "due_date": "gibberish"})  # keeps the old date
    assert db.get_task(conn, tid)["due_date"] == "2026-10-24"


def test_due_date_box_has_a_calendar_picker(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "call", status="next", project_id=pid)
    html = client.get(f"/tasks/{tid}").get_data(as_text=True)
    assert 'name="due_date"' in html and 'class="cal"' in html and 'type="date" class="picker"' in html


def test_h0_tasks_stay_off_next_actions(client, conn):
    gid = db.add_task(conn, "stretch", status="next", context="@home", ground=1)
    pid = db.add_project(conn, "Proj")
    pt = db.add_task(conn, "real step", status="next", context="@home", project_id=pid)
    page = client.get("/next").data
    assert b"real step" in page and b"stretch" not in page
    assert db.counts(conn, date(2026, 10, 22))["next"] == 1
    assert gid in [t["id"] for t in db.ground_level(conn)]


def test_h0_tab_lists_ground_next_actions(client, conn):
    db.add_task(conn, "stretch", status="next", context="@home", ground=1)
    db.add_task(conn, "later bins", status="next", context="@home", ground=1, recur="weekly", due_date="2026-11-30")
    page = client.get("/h0").data
    main, _, upcoming = page.partition(b"Upcoming repeating")  # like Next: not-yet-due repeats are folded away
    assert b"stretch" in main and b"later bins" not in main and b"later bins" in upcoming
    assert b"later bins" not in client.get("/next").data
    assert db.counts(conn, date(2026, 10, 22))["h0"] == 1
