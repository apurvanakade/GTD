import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
import import_mindwtr  # noqa: E402
import recur  # noqa: E402
from app import app  # noqa: E402

TODAY = date(2026, 10, 22)


@pytest.fixture
def client(tmp_path):
    app.config.update(TESTING=True, DB_PATH=str(tmp_path / "t.db"), TODAY=TODAY)
    with app.test_client() as c:
        yield c


@pytest.fixture
def conn(client):
    c = db.connect(app.config["DB_PATH"])
    yield c
    c.close()


# --- recur -----------------------------------------------------------------

@pytest.mark.parametrize("rule,expected", [
    ("weekly", (1, "week")), ("every 2 weeks", (2, "week")), ("every 6 months", (6, "month")),
    ("Daily", (1, "day")), ("every year", (1, "year")), ("every 0 days", None), ("fortnightly", None),
])
def test_parse(rule, expected):
    assert recur.parse(rule) == expected


def test_next_due_keeps_anchor_and_skips_past():
    assert recur.next_due("weekly", date(2026, 10, 25), TODAY) == date(2026, 11, 1)
    assert recur.next_due("weekly", date(2026, 10, 1), TODAY) == date(2026, 10, 29)  # overdue: skip ahead
    assert recur.next_due("monthly", date(2026, 1, 31), date(2026, 1, 31)) == date(2026, 2, 28)
    assert recur.next_due("every 6 months", None, TODAY) == date(2027, 4, 22)


# --- inbox processing ------------------------------------------------------

def test_capture_and_process_to_next(client, conn):
    client.post("/capture", data={"title": "call dentist"})
    t = db.tasks_with_status(conn, "inbox")[0]
    # context is required for a next action
    client.post(f"/process/{t['id']}", data={"action": "next", "title": "call dentist", "context": ""})
    assert db.get_task(conn, t["id"])["status"] == "inbox"
    client.post(f"/process/{t['id']}", data={"action": "next", "title": "Call dentist", "context": "@calls",
                                             "project_id": "", "due_date": ""})
    row = db.get_task(conn, t["id"])
    assert (row["status"], row["context"], row["title"]) == ("next", "@calls", "Call dentist")
    assert b"Call dentist" in client.get("/next?ctx=@calls").data


def test_process_other_actions(client, conn):
    ids = [db.add_task(conn, n) for n in ("a", "b", "c", "d", "e")]
    client.post(f"/process/{ids[0]}", data={"action": "trash"})
    client.post(f"/process/{ids[1]}", data={"action": "someday"})
    client.post(f"/process/{ids[2]}", data={"action": "done"})
    client.post(f"/process/{ids[3]}", data={"action": "waiting", "waiting_on": "Bob"})
    r = client.post(f"/process/{ids[4]}", data={"action": "project", "title": "Fix porch"})
    assert db.get_task(conn, ids[0]) is None
    assert db.get_task(conn, ids[1])["status"] == "someday"
    assert db.get_task(conn, ids[2])["status"] == "done"
    assert db.get_task(conn, ids[3])["waiting_on"] == "Bob"
    assert db.get_task(conn, ids[4]) is None
    assert "/projects/" in r.headers["Location"]
    assert [p["title"] for p in db.stalled_projects(conn)] == ["Fix porch"]


def test_all_pages_render(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "t", status="next", context="@home", project_id=pid, due_date="2026-10-01")
    db.add_task(conn, "inboxed")
    for url in ("/inbox", "/process", "/next", "/projects", f"/projects/{pid}", "/waiting",
                "/someday", "/review", f"/tasks/{tid}", "/calendar", "/calendar?m=2027-01",
                "/calendar?m=bogus", "/horizons"):
        assert client.get(url).status_code == 200, url


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


def test_calendar_add(client, conn):
    r = client.post("/calendar", data={"title": "file taxes", "due_date": "2027-04-15", "context": "@computer"})
    assert r.headers["Location"].endswith("m=2027-04")
    (t,) = db.tasks_with_status(conn, "next")
    assert (t["title"], t["due_date"], t["context"]) == ("file taxes", "2027-04-15", "@computer")


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
