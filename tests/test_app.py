import os
import re
import sqlite3
from datetime import date, datetime, timedelta

import pytest

import dates
import db
import recur
from app import HORIZON_STEPS, REVIEW_STEPS, app, local_input
from conftest import NOW, TODAY, seed

# --- recur -----------------------------------------------------------------

@pytest.mark.parametrize("rule,expected", [
    ("weekly", (1, "week")), ("every 2 weeks", (2, "week")), ("every 6 months", (6, "month")),
    ("Daily", (1, "day")), ("every year", (1, "year")), ("every 0 days", None), ("fortnightly", None),
    ("every 2 weeks after done", (2, "week")), ("weekly after completion", (1, "week")),
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


# --- inbox processing ------------------------------------------------------

def test_no_process_page(client, conn):
    """The Inbox page is where items get dealt with: no second page doing the same."""
    db.add_task(conn, "x")
    assert client.get("/process").status_code == 404
    assert "/process" not in client.get("/inbox").get_data(as_text=True)

def test_capture_box_only_on_inbox(client, conn):
    tid = db.add_task(conn, "x")
    pid = db.add_project(conn, "P")
    inbox = client.get("/inbox").data.decode()
    assert '<input name="title" placeholder="Capture' in inbox.split("<main>")[1]
    for url in ("/next", f"/tasks/{tid}", f"/projects/{pid}"):
        page = client.get(url).data
        assert b"<dialog" in page and b"Capture to inbox" not in page, url
    r = client.post("/capture", data={"title": "buy milk"}, headers={"Referer": "/next"}, follow_redirects=True)
    assert b"Added to inbox: buy milk" in r.data


def test_crosslinks(client, conn):
    pid = db.add_project(conn, "Garden")
    tid = db.add_task(conn, "dig", step="next", project_id=pid)
    gid = db.add_task(conn, "stretch", step="next", ground=1)
    assert f'href="/projects/{pid}"' in client.get(f"/tasks/{tid}").get_data(as_text=True)
    gpage = client.get(f"/tasks/{gid}").get_data(as_text=True)
    assert 'title="Open the project"' not in gpage  # an H0 task has no project to open
    assert 'class="crumb"' not in client.get(f"/projects/{pid}").get_data(as_text=True)  # its selects say where it is


# --- recurring + projects --------------------------------------------------

def test_recurring_next_step_comes_back_as_later(client, conn):
    """Picking the next action is a conscious decision: a repeating next step's new instance is a later step."""
    pid = db.add_project(conn, "Home")
    tid = db.add_task(conn, "vacuum", step="next", project_id=pid, recur="weekly", due_date="2026-10-22")
    assert [t["id"] for t in db.next_actions(conn, TODAY)] == [tid]
    client.post(f"/tasks/{tid}/complete", headers={"X-Requested-With": "fetch"})
    assert db.next_list(conn) == []
    (new,) = db.project_tasks(conn, pid)
    assert (new["step"], new["due_date"]) == ("later", "2026-10-29")
    assert db.project_next(conn, pid) is None  # stalled until you pick one


def test_recurring_waiting_step_and_h0_task_keep_their_place(client, conn):
    pid = db.add_project(conn, "Home")
    w = db.add_task(conn, "chase", step="waiting", project_id=pid, recur="weekly", due_date="2026-10-22")
    h = db.add_task(conn, "stretch", ground=1, recur="daily", due_date="2026-10-22")
    assert db.get_task(conn, db.complete_task(conn, w, TODAY))["step"] == "waiting"
    new = db.get_task(conn, db.complete_task(conn, h, TODAY))
    assert (new["ground"], new["step"]) == (1, "next")
    hw = db.add_task(conn, "chase Ann", ground=1, step="waiting", recur="weekly", due_date="2026-10-22")
    assert db.get_task(conn, db.complete_task(conn, hw, TODAY))["step"] == "waiting"


def test_project_cannot_complete_with_open_tasks(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "t", step="next", project_id=pid)
    client.post(f"/projects/{pid}", data={"title": "P", "notes": "", "status": "done"})
    assert db.get_project(conn, pid)["status"] == "active"
    db.complete_task(conn, tid, TODAY)
    client.post(f"/projects/{pid}", data={"title": "P", "notes": "n", "status": "done"})
    assert db.get_project(conn, pid)["status"] == "done"


def test_one_next_action_per_project(client, conn):
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", step="next", project_id=pid)
    b = db.add_task(conn, "b", step="next", project_id=pid)  # b takes over
    assert [db.get_task(conn, i)["step"] for i in (a, b)] == ["later", "next"]
    client.post(f"/projects/{pid}/tasks", data={"title": "c"})  # project has a next, so c is later
    c = db.get_task(conn, b + 1)
    assert (c["title"], c["step"]) == ("c", "later")
    client.post(f"/tasks/{a}/make-next")
    assert [db.get_task(conn, i)["step"] for i in (a, b)] == ["next", "later"]
    # finishing the next action leaves the project stalled (⚠), for you to pick the next one
    r = client.post(f"/tasks/{a}/complete", headers={"X-Requested-With": "fetch"})
    assert r.status_code == 204
    assert db.project_next(conn, pid) is None and [p["title"] for p in db.stalled_projects(conn)] == ["P"]
    assert b"Make next" in client.get(f"/projects/{pid}").data


def test_done_then_pick_next(client, conn):
    """✓→ completes a project step and opens its project, to choose the next action there."""
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", step="next", project_id=pid)
    db.add_task(conn, "b", step="later", project_id=pid)
    h = db.add_task(conn, "h", step="next", ground=1)
    assert 'done-then-project' not in client.get("/next").get_data(as_text=True)  # only on the task page
    assert f'/tasks/{a}/done-then-project' in client.get(f"/tasks/{a}").get_data(as_text=True)
    assert f'/tasks/{h}/done-then-project' not in client.get(f"/tasks/{h}").get_data(as_text=True)  # no project
    r = client.post(f"/tasks/{a}/done-then-project", headers={"Referer": "http://localhost/next"})
    assert r.status_code == 302 and r.headers["Location"].endswith(f"/projects/{pid}")
    assert db.get_task(conn, a)["state"] == "done"
    r = client.post(f"/tasks/{h}/done-then-project", headers={"Referer": "http://localhost/h0"})
    assert r.headers["Location"].endswith("/h0") and db.get_task(conn, h)["state"] == "done"


def test_done_stays_on_the_page(client, conn):
    tid = db.add_task(conn, "y", step="next", ground=1)
    r = client.post(f"/tasks/{tid}/complete", headers={"Referer": f"http://localhost/tasks/{tid}"})
    assert r.headers["Location"] == f"http://localhost/tasks/{tid}" and db.get_task(conn, tid)["state"] == "done"


def test_waiting_and_someday_projects_hide_their_tasks(client, conn):
    pid = db.add_project(conn, "P")
    db.add_task(conn, "a", step="next", project_id=pid)
    for status, page in (("waiting", "/waiting"), ("someday", "/someday")):
        client.post(f"/projects/{pid}", data={"title": "P", "status": status})
        assert db.next_actions(conn, TODAY) == [] and db.stalled_projects(conn) == []
        assert b"P</a>" in client.get(page).data
    client.post(f"/projects/{pid}", data={"title": "P", "status": "active"})
    assert [t["title"] for t in db.next_actions(conn, TODAY)] == ["a"]


def test_tasks_need_a_project_or_h0(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "loose", step="next", project_id=pid)
    for status in ("next", "waiting", "later"):
        db.update_task(conn, tid, step=status, project_id=pid)
        client.post(f"/tasks/{tid}", data={"step": status, "project_id": "", "recur": "weekly"})
        assert db.get_task(conn, tid)["inbox"]  # repeating doesn't exempt it
    client.post(f"/tasks/{tid}", data={"step": "next", "project_id": "h0"})
    t = db.get_task(conn, tid)
    assert (t["step"], t["ground"], t["project_id"]) == ("next", 1, None)
    assert t["title"] in client.get("/h0").get_data(as_text=True)
    client.post(f"/tasks/{tid}", data={"step": "next", "project_id": str(pid)})
    assert db.get_task(conn, tid)["ground"] == 0  # picking a project clears H0

    item = db.add_task(conn, "inbox item")
    client.post(f"/tasks/{item}", data={"title": "inbox item", "project_id": "h0"})
    assert db.get_task(conn, item)["ground"] and db.get_task(conn, item)["step"] == "next"
    assert [t["id"] for t in db.h0_actions(conn)] == [item]
    assert "inbox item" in client.get("/horizons?level=ground").data.decode()  # H0 tasks
    assert f'href="/projects/{pid}"' in client.get("/horizons?level=projects").data.decode()  # H1 projects

    stray = db.add_task(conn, "stray wait", step="waiting")  # no project, not H0: it's an inbox item
    assert db.get_task(conn, stray)["inbox"] and stray not in [t["id"] for t in db.next_list(conn)]


def test_inbox_is_not_a_status(client, conn):
    """The inbox is every open task with neither a project nor H0; filing one into a project makes it a later step."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO tasks (title, state) VALUES ('x', 'inbox')")
    conn.rollback()
    client.post("/capture", data={"title": "call dentist"})
    (t,) = db.inbox_tasks(conn)
    assert t["step"] is None and client.get("/inbox").status_code == 200
    page = client.get(f"/tasks/{t['id']}").get_data(as_text=True)
    assert 'name="step"' not in page  # nothing to pick until it's in a project
    pid = db.add_project(conn, "Teeth")
    db.add_task(conn, "find number", step="later", project_id=pid)  # project has no next action: still later
    client.post(f"/tasks/{t['id']}", data={"title": "call dentist", "project_id": str(pid)})
    t = db.get_task(conn, t["id"])
    assert (t["step"], t["project_id"], t["inbox"]) == ("later", pid, 0) and db.inbox_tasks(conn) == []
    assert 'name="step"' in client.get(f"/tasks/{t['id']}").get_data(as_text=True)
    # Taking the project away puts it back in the inbox, whatever its status was.
    db.update_task(conn, t["id"], step="waiting")
    r = client.post(f"/tasks/{t['id']}", data={"project_id": "", "step": "waiting"}, follow_redirects=True)
    assert db.get_task(conn, t["id"])["step"] is None and b"went to the inbox" in r.data
    assert [x["id"] for x in db.inbox_tasks(conn)] == [t["id"]] and db.counts(conn, TODAY)["inbox"] == 1


def test_h0_tasks_are_next_or_waiting(client, conn):
    """An H0 task is a next action or waiting; never later. Only H0's next actions are on the H0 tab."""
    steps = {s: db.get_task(conn, db.add_task(conn, str(s), step=s, ground=1))["step"] for s in (None, "next", "waiting", "later")}
    assert steps == {None: "next", "next": "next", "waiting": "waiting", "later": "next"}
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "w", step="waiting", project_id=pid)
    client.post(f"/tasks/{tid}", data={"project_id": "h0", "step": "waiting"})
    t = db.get_task(conn, tid)
    assert (t["ground"], t["step"], t["project_id"]) == (1, "waiting", None)  # moved: still waiting
    page = client.get(f"/tasks/{tid}").get_data(as_text=True)
    assert '<option selected>waiting</option>' in page and "<option>later</option>" not in page
    client.post(f"/tasks/{tid}", data={"project_id": "h0", "step": "next"})
    assert db.get_task(conn, tid)["step"] == "next"
    client.post("/waiting/add", data={"title": "parcel", "project_id": "h0", "due_date": ""})
    parcel = conn.execute("SELECT * FROM tasks WHERE title = 'parcel'").fetchone()
    assert (parcel["ground"], parcel["step"]) == (1, "waiting")
    h0 = client.get("/h0").get_data(as_text=True)
    assert ">later<" in h0 and ">waiting<" not in h0 and ">parcel<" not in h0
    assert len(db.h0_actions(conn)) == 4 and db.counts(conn, TODAY)["waiting"] == 2
    assert ">parcel<" in client.get("/waiting").get_data(as_text=True)


def test_h0_task_moving_into_a_project(client, conn):
    """Out of H0, a next action becomes a later step (next stays a conscious choice); a waiting one stays waiting."""
    pid = db.add_project(conn, "P")
    nxt = db.add_task(conn, "n", ground=1)
    wait = db.add_task(conn, "w", ground=1, step="waiting")
    first = db.add_task(conn, "first", project_id=pid, step="next")
    client.post(f"/tasks/{nxt}", data={"project_id": str(pid), "step": "next"})  # the step select untouched
    client.post("/tasks/move", data={"id": wait, "project_id": str(pid)}, headers=FETCH)
    assert [db.get_task(conn, i)["step"] for i in (nxt, wait, first)] == ["later", "waiting", "next"]
    back = db.add_task(conn, "back", project_id=pid, step="later")
    client.post("/tasks/move", data={"id": back, "project_id": "h0"}, headers=FETCH)
    assert db.get_task(conn, back)["step"] == "next"


def test_waiting_lists_projects_then_tasks(client, conn):
    wp = db.add_project(conn, "Visa", status="waiting")
    kitchen = db.add_project(conn, "Kitchen")
    db.add_task(conn, "quote", project_id=kitchen, step="waiting")
    db.add_task(conn, "tiles", project_id=kitchen, step="later")
    db.add_task(conn, "parcel", ground=1, step="waiting")
    html = client.get("/waiting").get_data(as_text=True).split('class="drop-panel"')[0]
    assert html.index("Visa") < html.index("parcel") < html.index("quote") and ">tiles<" not in html  # H0's tasks first
    assert 'data-reorder="/projects/order/waiting"' in html
    other = db.add_project(conn, "Passport", status="waiting")
    client.post("/projects/order/waiting", data={"ids": f"{other},{wp}"})
    assert [p["title"] for p in db.projects(conn, "waiting")] == ["Passport", "Visa"]


def test_steps_only_exist_in_projects_and_h0(conn):
    """The database refuses a step on an inbox item, a project or H0 task without one, a later H0 task, and an H0 task
    in a project."""
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "x", project_id=pid)
    for sql in ("UPDATE tasks SET project_id = NULL WHERE id = ?", "UPDATE tasks SET step = NULL WHERE id = ?",
                "UPDATE tasks SET ground = 1 WHERE id = ?"):
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(sql, (tid,))
    loose = db.add_task(conn, "loose", step="next")  # no project: the step is dropped
    assert db.get_task(conn, loose)["step"] is None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE tasks SET step = 'next' WHERE id = ?", (loose,))
    h0 = db.add_task(conn, "h0", ground=1)
    for sql in ("UPDATE tasks SET step = NULL WHERE id = ?", "UPDATE tasks SET step = 'later' WHERE id = ?"):
        with pytest.raises(sqlite3.IntegrityError):  # an H0 task is next or waiting
            conn.execute(sql, (h0,))


# --- calendar + horizons -------------------------------------------------

def test_calendar_shows_each_task_once_on_its_due_date(client, conn):
    db.add_task(conn, "dentist", step="next", due_date="2026-12-03")  # in Nov grid (Sun Nov 1 .. Sat Dec 5)
    db.add_task(conn, "haircut", step="next", due_date="2026-10-28")  # before the grid
    db.add_task(conn, "plants", step="next", recur="weekly", due_date="2026-10-25")
    db.add_task(conn, "undated", step="next")
    html = client.get("/calendar?m=2026-11").data.decode()
    assert "dentist" in html and "haircut" not in html
    assert "plants" not in html  # Oct 25 is before the grid; no future copies are projected
    assert client.get("/calendar?m=2026-10").data.decode().count(">↻ plants<") == 1
    assert "undated" not in html


def test_calendar_week_view(client, conn):
    db.add_task(conn, "in week", step="next", due_date="2026-10-18")  # Sunday of the week of Thu Oct 22
    db.add_task(conn, "next week", step="next", due_date="2026-10-25")
    db.add_task(conn, "last week", step="next", due_date="2026-10-17")
    db.add_task(conn, "plants", step="next", recur="weekly", due_date="2026-10-20")
    html = client.get("/calendar?w=2026-10-22").data.decode()
    assert "in week" in html and "next week" not in html and "last week" not in html
    assert html.count(">↻ plants<") == 1
    assert html.index(">Sun<") < html.index(">Mon<")  # weeks start on Sunday
    assert "18 Oct" in html and "24 Oct" in html
    assert "w=2026-10-25" in html and "w=2026-10-11" in html  # next / previous week
    assert client.get("/calendar?w=bogus").status_code == 200
    assert client.get("/calendar?m=bogus").status_code == 200
    assert "w=2026-10-18" in client.get("/calendar").data.decode()  # month view links to this week


def test_calendar_remembers_the_view(client):
    assert "Oct 2026" not in client.get("/calendar").data.decode()  # month view by default
    client.get("/calendar?w=2026-11-04")
    html = client.get("/calendar").data.decode()
    assert "18 Oct – 24 Oct 2026" in html  # back to week view, on this week
    html = client.get("/calendar?m=2026-11").data.decode()
    assert "November 2026" in html
    assert "October 2026" in client.get("/calendar").data.decode()  # month view again, on this month
    client.get("/calendar?w=2026-10-22")
    r = client.post("/calendar", data={"title": "x", "due_date": "2026-11-04", "project_id": "h0"},
                    headers={"Referer": "http://localhost/calendar?w=2026-10-22"})
    assert r.headers["Location"].endswith("w=2026-10-22")  # adding stays on the week you're looking at


def nav_badges(html):
    """{tab: (classes, tooltip)} for the nav's count dots."""
    nav = html.split("<nav", 1)[1].split("</nav>", 1)[0]
    return {tab: (cls, title) for tab, cls, title in
            re.findall(r'>(\w+) <b class="badge ([^"]*)" title="([^"]*)" aria-label="\d+"></b></a>', nav)}


def test_waiting_steps_can_be_made_next(client, conn):
    pid = db.add_project(conn, "Deck")
    db.add_task(conn, "sand", step="next", project_id=pid)
    w = db.add_task(conn, "stain arrives", step="waiting", project_id=pid)
    html = client.get(f"/projects/{pid}").get_data(as_text=True)
    assert f'action="/tasks/{w}/make-next"' in html
    client.post(f"/tasks/{w}/make-next")
    assert db.project_next(conn, pid)["id"] == w


def test_completed_steps_dont_repeat_their_project_or_step(client, conn):
    pid = db.add_project(conn, "Garden")
    db.complete_task(conn, db.add_task(conn, "buy seeds", step="later", project_id=pid), TODAY)
    done = client.get(f"/projects/{pid}?done=1").get_data(as_text=True).split("<h2>Completed</h2>", 1)[1]
    assert "buy seeds" in done and 'class="proj"' not in done and ">later<" not in done
    done = client.get("/next?done=1").get_data(as_text=True).split("<h2>Completed</h2>", 1)[1]
    assert 'class="proj"' in done and ">later<" not in done  # elsewhere the project still says where it was


def test_no_stripe_colors(client, conn):
    """Colored stripes meant different things on different pages (due soon here, waiting there): only the calendar,
    with its legend, colors tasks (by kind)."""
    seed(conn)
    for url in ("/next", "/h0", "/projects/1", "/projects", "/waiting", "/inbox", "/horizons", "/search?q=tiles"):
        html = client.get(url).get_data(as_text=True)
        assert not re.search(r'class="[^"]*\b[sk]-(overdue|today|week|month|later|next|h0|waiting|someday|inbox)\b', html), url
        assert 'class="legend"' not in html, url
    css = client.get("/static/style.css").get_data(as_text=True)
    assert "inset 3px" not in css


def test_nav_counts_are_dots(client, conn):
    html = client.get("/inbox").get_data(as_text=True)
    assert nav_badges(html) == {}  # nothing to count: no dots, not even Next's 0
    for i in range(4):
        db.add_task(conn, f"inbox {i}")
    pid = db.add_project(conn, "P")  # stalled
    db.add_task(conn, "due", step="next", project_id=db.add_project(conn, "Q"), due_date="2026-10-22")
    db.add_task(conn, "late", step="next", ground=1, due_date="2026-10-01")
    for i in range(6):
        db.add_task(conn, f"h0 {i}", step="next", ground=1, due_date="2026-10-22")
    db.add_task(conn, "wait", step="waiting", project_id=pid)
    assert nav_badges(client.get("/inbox").get_data(as_text=True)) == {
        "Next": ("s1", "1 due today"), "H0": ("s3 hot", "7 due today or overdue"), "Waiting": ("s1", "1 waiting"),
        "Inbox": ("s2", "4 in the inbox"), "Projects": ("s1 warn", "1 stalled")}


def bands(html):
    """[(band, open, [titles])] in page order."""
    out = []
    for key, attrs, body in re.findall(r'<details class="band" data-band="(\w+)"([^>]*)>(.*?)</details>', html, re.S):
        out.append((key, "open" in attrs, re.findall(r'class="title"[^>]*>([^<]*)<', body)))
    return out


def test_next_and_h0_fold_into_due_bands(client, conn):
    for title, due in [("od", "2026-10-20"), ("td", "2026-10-22"), ("wk", "2026-10-25"),
                       ("mo", "2026-11-10"), ("lt", "2027-03-01")]:
        db.add_task(conn, title, step="next", ground=1, due_date=due)
    html = client.get("/h0").get_data(as_text=True)
    assert bands(html) == [("now", True, ["od", "td"]), ("week", False, ["wk"]), ("later", False, ["mo", "lt"])]
    assert re.search(r'data-band="week"[^>]*>\s*<summary>This week <b>1</b></summary>', html)
    assert 'title="Ground level' not in html  # no H0 tag on the H0 page
    assert 'class="legend"' not in html
    pid = db.add_project(conn, "P")
    db.add_task(conn, "loose end", step="next", project_id=pid)
    html = client.get("/next").get_data(as_text=True)
    assert bands(html) == [("nodate", False, ["loose end"])]  # empty bands aren't shown
    assert 'class="legend"' not in html


def test_calendar_colors_tasks_by_kind(client, conn):
    some = db.add_project(conn, "Dreams", status="someday")
    act = db.add_project(conn, "Act")
    db.add_task(conn, "nexty", step="next", project_id=act, due_date="2026-10-23")
    db.add_task(conn, "stepy", step="later", project_id=act, due_date="2026-10-23")
    db.add_task(conn, "groundy", step="next", ground=1, due_date="2026-10-23")
    db.add_task(conn, "dreamy", step="later", project_id=some, due_date="2026-10-23")
    db.add_task(conn, "waity", step="waiting", project_id=act, due_date="2026-10-23")
    html = client.get("/calendar").data.decode()
    for kind, title in (("next", "nexty"), ("later", "stepy"), ("h0", "groundy"), ("someday", "dreamy"),
                        ("waiting", "waity")):
        assert re.search(rf'class="ev k-{kind}[^"]*">[^<]*{title}<', html), kind
    assert 'class="legend"' in html


def test_calendar_drop_moves_the_due_date(client, conn):
    tid = db.add_task(conn, "dentist", step="next", ground=1, due_date="2026-10-28")
    fetch = {"X-Requested-With": "fetch"}
    assert client.post(f"/tasks/{tid}/due", data={"due_date": "2026-11-03"}, headers=fetch).status_code == 204
    assert db.get_task(conn, tid)["due_date"] == "2026-11-03"
    assert client.post(f"/tasks/{tid}/due", data={"due_date": "bogus"}, headers=fetch).status_code == 400
    db.complete_task(conn, tid, TODAY)  # a done task can't be moved
    assert client.post(f"/tasks/{tid}/due", data={"due_date": "2026-11-05"}, headers=fetch).status_code == 400
    assert db.get_task(conn, tid)["due_date"] == "2026-11-03"


def test_calendar_month_greys_days_outside_the_month(client):
    html = client.get("/calendar?m=2026-11").data.decode()
    assert 'class="out"' in html  # Dec 1..5 trail the November grid
    assert ">26 Oct<" not in html  # month view labels days by number only


def test_calendar_add(client, conn):
    pid = db.add_project(conn, "Taxes")
    r = client.post("/calendar", data={"title": "file taxes", "due_date": "2027-04-15",
                                       "project_id": str(pid)})
    assert r.headers["Location"] == "/calendar"
    (t,) = db.tasks_with_step(conn, "next")
    assert (t["title"], t["due_date"]) == ("file taxes", "2027-04-15")
    client.post("/calendar", data={"title": "send forms", "due_date": "2027-04-01", "project_id": str(pid)})
    assert db.get_task(conn, t["id"])["step"] == "next"  # existing next action is kept
    assert [x["title"] for x in db.tasks_with_step(conn, "later")] == ["send forms"]
    client.post("/calendar", data={"title": "loose", "due_date": "2027-04-02"})
    assert [x["title"] for x in db.inbox_tasks(conn)] == ["loose"]
    client.post("/calendar", data={"title": "bins", "due_date": "2027-04-02", "recur": "weekly", "project_id": "h0"})
    assert [x["title"] for x in db.h0_actions(conn)] == ["bins"]


def test_horizons(client, conn):
    items = db.horizons(conn)
    assert len(items["purpose"]) == 1 and len(items["vision"]) == 1  # seeded, not duplicated
    db.connect(app.config["DB_PATH"]).close()
    assert len(db.horizons(conn)["purpose"]) == 1
    client.post("/horizons", data={"level": "goal", "title": "Get promoted"})
    gid = db.horizons(conn)["goal"][-1]["id"]
    client.post(f"/horizons/{gid}", data={"title": "Get promoted", "notes": "Dossier by Sept"})
    assert db.get_horizon(conn, gid)["notes"] == "Dossier by Sept"
    assert client.post("/horizons", data={"level": "purpose", "title": "x"}).status_code == 400
    pid = items["purpose"][0]["id"]
    client.post(f"/horizons/{pid}/delete")
    assert db.get_horizon(conn, pid) is not None  # purpose can't be deleted
    r = client.post(f"/horizons/{gid}/delete")
    assert db.get_horizon(conn, gid) is None and r.headers["Location"] == "/horizons?level=goal"  # back to its card


ADD_LINE = re.compile(r'<form[^>]*class="add')


def horizon_card(html):
    """The one Horizons card shown: its key and its HTML."""
    m = re.search(r'<li id="(\w+)" class="step on">(.*?)</li>\s*</ol>', html, re.S)
    return m and m.group(1), m and m.group(2)


def test_horizons_show_one_level_at_a_time(client, conn):
    """Like the weekly review: one card per horizon, H5 down to H0, and a rail of dots to move between them."""
    seed(conn)
    html = client.get("/horizons").get_data(as_text=True)
    assert html.count('class="dot') == len(HORIZON_STEPS) == 6
    assert horizon_card(html)[0] == "purpose"  # opens at the top
    top = client.get("/horizons?level=purpose").get_data(as_text=True)
    assert horizon_card(top)[0] == "purpose" and not ADD_LINE.search(top)  # no add line there
    assert 'rel="prev"' not in top and 'href="/horizons?level=vision" rel="next"' in top
    goal = client.get("/horizons?level=goal").get_data(as_text=True)
    key, card = horizon_card(goal)
    assert key == "goal" and "Ship the kitchen" in card and "Home" not in card
    assert '<input type="hidden" name="level" value="goal">' in goal  # the add line adds to the card shown
    assert '<span class="count">1</span>' in card
    area = client.get("/horizons?level=area").get_data(as_text=True)
    assert '<input type="hidden" name="level" value="area">' in area and "Clean and calm" in horizon_card(area)[1]
    assert horizon_card(client.get("/horizons?level=bogus").get_data(as_text=True))[0] == "purpose"
    assert not ADD_LINE.search(client.get("/horizons?level=ground").get_data(as_text=True))
    vision = db.horizons(conn)["vision"][0]["id"]
    assert f'data-autosave="/autosave/horizons/{vision}"' in client.get("/horizons?level=vision").get_data(as_text=True)


def test_reference(client, conn):
    """A flat list of reference items (title + notes) so reference needn't live in another app."""
    assert "Nothing filed yet" in client.get("/reference").get_data(as_text=True)
    client.post("/reference/add", data={"title": "  "}, headers={"Referer": "/reference"})
    assert db.references(conn) == []
    r = client.post("/reference/add", data={"title": "Wifi password"}, headers={"Referer": "/reference"})
    rid = db.references(conn)[0]["id"]
    assert r.headers["Location"].endswith(f"/reference/{rid}")  # a new item opens, to add its details
    r = client.post(f"/reference/{rid}", data={"title": "Wifi", "notes": "hunter2\nsee https://example.com/a?b=1 and obsidian://open?vault=j"},
                    headers={"Referer": f"/reference/{rid}"})
    assert r.headers["Location"].endswith(f"/reference/{rid}")  # Save stays on the page
    assert db.get_reference(conn, rid)["notes"].startswith("hunter2")
    listing = client.get("/reference").get_data(as_text=True)
    assert "Wifi" in listing and "hunter2" in listing  # first line of the notes
    client.post(f"/reference/{rid}", data={"title": "", "notes": ""})
    assert db.get_reference(conn, rid)["title"] == "Wifi"  # a blank title keeps the old one
    assert client.post(f"/reference/{rid}/delete").headers["Location"].endswith("/reference")
    assert db.get_reference(conn, rid) is None
    assert client.get(f"/reference/{rid}").status_code == 404


def test_search_finds_reference(client, conn):
    db.add_reference(conn, "Boiler manual", notes="model XJ-9 service code")
    assert [r["title"] for r in db.search(conn, "xj-9")["references"]] == ["Boiler manual"]
    assert "Boiler manual" in client.get("/search?q=service").get_data(as_text=True)
    hit = client.get("/search/suggest?q=boilr").get_json()[0]
    assert hit["title"] == "Boiler manual" and hit["kind"] == "reference" and hit["url"].startswith("/reference/")


# --- import ----------------------------------------------------------------

def test_search(client, conn):
    pid = db.add_project(conn, "Kitchen remodel", notes="call the plumber")
    db.add_task(conn, "Buy tiles", step="next", project_id=pid)
    db.add_task(conn, "Pick tile grout")
    done = db.add_task(conn, "Measure kitchen tiles", step="next", project_id=pid)
    db.complete_task(conn, done, TODAY)
    db.add_task(conn, "100% sure")
    r = db.search(conn, "TILE")
    assert [t["title"] for t in r["tasks"]] == ["Pick tile grout", "Buy tiles"]
    assert [t["title"] for t in r["done"]] == ["Measure kitchen tiles"]
    assert [t["title"] for t in db.search(conn, "tiles kitchen")["done"]] == ["Measure kitchen tiles"]
    assert [p["title"] for p in db.search(conn, "plumber")["projects"]] == ["Kitchen remodel"]
    assert db.search(conn, "0%")["tasks"][0]["title"] == "100% sure"
    assert db.search(conn, "_")["tasks"] == []
    page = client.get("/search?q=tile").data
    assert b"Pick tile grout" in page and b"Measure kitchen tiles" in page
    assert b"Nothing matches" in client.get("/search?q=zzz").data
    assert client.get("/search").status_code == 200


# --- edges -----------------------------------------------------------------

def test_saving_a_task_stays_on_its_page(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "call", step="next", project_id=pid)
    # A field saves as soon as it's changed; the page stays put, with no buttons to save or leave.
    r = client.post(f"/tasks/{tid}", data={"step": "waiting"})
    assert r.headers["Location"] == f"/tasks/{tid}" and db.get_task(conn, tid)["step"] == "waiting"
    page = client.get(r.headers["Location"]).get_data(as_text=True)
    assert "Saved." not in page and "Save &amp; exit" not in page and "data-save-on-change" in page


def test_deleting_an_abandoned_task_goes_to_done(client, conn):
    tid = db.add_task(conn, "x", step="next", ground=1)
    r = client.post(f"/tasks/{tid}/delete", headers={"Referer": f"http://localhost/tasks/{tid}"})
    assert db.get_task(conn, tid)["state"] == "abandoned" and r.headers["Location"] == f"http://localhost/tasks/{tid}"
    r = client.post(f"/tasks/{tid}/delete")
    assert db.get_task(conn, tid) is None and r.headers["Location"] == "/done"


def test_delete_empty_project_only(client, conn):
    empty = db.add_project(conn, "Empty")
    full = db.add_project(conn, "Full")
    db.add_task(conn, "step", step="next", project_id=full)
    assert "Delete project" in client.get(f"/projects/{empty}").get_data(as_text=True)
    assert "Delete project" not in client.get(f"/projects/{full}").get_data(as_text=True)
    finished = db.add_project(conn, "Finished")  # only done steps, no time: still not deletable, so no ✕
    db.complete_task(conn, db.add_task(conn, "step", step="next", project_id=finished), date(2026, 10, 22))
    assert "Delete project" not in client.get(f"/projects/{finished}").get_data(as_text=True)
    client.post(f"/projects/{full}/delete")
    assert db.get_project(conn, full) is not None
    r = client.post(f"/projects/{empty}/delete")
    assert db.get_project(conn, empty) is None and r.headers["Location"] == "/projects"


def test_review_next_actions_skip_scheduled(client, conn):
    pid = db.add_project(conn, "Trip")
    db.add_task(conn, "book flights", step="next", project_id=pid, due_date="2026-12-01")
    db.add_task(conn, "pack bags", step="later", project_id=pid)
    db.add_task(conn, "renew visa", step="next", project_id=db.add_project(conn, "Visa"))
    db.add_task(conn, "call hotel", step="next", project_id=db.add_project(conn, "Hotel"),
                due_date="2026-10-22")  # due today: still left out, or the review never ends
    db.add_task(conn, "pay deposit", step="next", project_id=db.add_project(conn, "Deposit"),
                due_date="2026-10-21")  # overdue: kept
    nexts = card(client.get("/review").get_data(as_text=True), "nexts")
    assert "renew visa" in nexts and "pay deposit" in nexts  # listed like orphans
    assert "book flights" not in nexts and "call hotel" not in nexts


def test_task_form_edges(client, conn):
    tid = db.add_task(conn, "t", step="next", ground=1, notes="old", recur="weekly",
                      due_date="2026-10-22")
    r = client.post(f"/tasks/{tid}", data={"title": "", "notes": "", "recur": "fortnightly"}, follow_redirects=True)
    t = db.get_task(conn, tid)
    assert (t["title"], t["notes"], t["recur"]) == ("t", "old", "weekly")  # title and notes are autosave's; bad rule ignored
    assert "understand repeat rule “fortnightly”" in r.get_data(as_text=True)
    client.post(f"/tasks/{tid}", data={"recur": "every 3 days", "due_date": ""})
    assert db.get_task(conn, tid)["due_date"] == "2026-10-22"  # a repeat needs a due date: today
    assert client.get("/tasks/999").status_code == 404
    assert client.post("/tasks/999/complete").status_code == 404


def test_make_next_returns_to_the_project(client, conn):
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", step="later", project_id=pid)
    r = client.post(f"/tasks/{a}/make-next", headers={"Referer": f"/projects/{pid}"})
    assert db.get_task(conn, a)["step"] == "next" and r.headers["Location"] == f"/projects/{pid}"
    b = db.add_task(conn, "b", step="later", project_id=pid)
    r = client.post(f"/tasks/{b}/make-next", headers={"Referer": f"/projects/{pid}"})
    assert r.headers["Location"] == f"/projects/{pid}"
    assert [db.get_task(conn, i)["step"] for i in (a, b)] == ["later", "next"]


def test_complete_twice_is_harmless(client, conn):
    tid = db.add_task(conn, "bins", step="next", ground=1, recur="weekly", due_date="2026-10-22")
    client.post(f"/tasks/{tid}/complete")
    client.post(f"/tasks/{tid}/complete")  # e.g. a double click
    assert len(db.h0_actions(conn)) == 1  # only one new instance


def test_projects_create_and_status(client, conn):
    assert client.post("/projects", data={"title": "  "}).headers["Location"] == "/projects"
    client.post("/projects", data={"title": "Paint fence"})
    pid = db.projects(conn)[-1]["id"]
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
    assert db.project_tasks(conn, pid)[0]["step"] == "later"  # "next" is always chosen, even with no next yet
    assert b'<option selected>later</option>' in client.get(f"/projects/{pid}").data


def test_capture_on_inbox_has_no_flash_and_blank_is_ignored(client, conn):
    r = client.post("/capture", data={"title": "milk"}, headers={"Referer": "http://localhost/inbox"},
                    follow_redirects=True)
    assert b"Added to inbox" not in r.data and b"milk" in r.data
    client.post("/capture", data={"title": "   "})
    assert [t["title"] for t in db.inbox_tasks(conn)] == ["milk"]


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


def test_db_rejects_unknown_fields(conn):
    tid = db.add_task(conn, "x")
    with pytest.raises(ValueError):
        db.update_task(conn, tid, colour="red")
    assert db.search(conn, "   ") == {"tasks": [], "done": [], "projects": [], "horizons": [], "references": []}
    with pytest.raises(ValueError):
        db.update_reference(conn, 1, colour="red")


def test_next_due_skipping_ahead_does_not_drift():
    # Overdue month-end and leap-day tasks keep their day when several periods are skipped.
    assert recur.next_due("monthly", date(2026, 1, 31), TODAY) == date(2026, 10, 31)
    assert recur.next_due("every 2 years", date(2024, 2, 29), TODAY) == date(2028, 2, 29)
    assert recur.next_due("yearly", date(2024, 2, 29), TODAY) == date(2027, 2, 28)


# --- timer -----------------------------------------------------------------

def iso(dt):
    return dt.isoformat(timespec="seconds")


def test_timer_starts_from_a_task_and_starting_another_stops_it(client, conn):
    a = db.add_task(conn, "write", step="next", ground=1)
    b = db.add_task(conn, "read", step="next", ground=1)
    assert b'action="/tasks/%d/start"' % a in client.get("/horizons?level=ground").data  # ▶ on task rows
    client.post(f"/tasks/{a}/start", headers={"Referer": "/next"})
    assert db.timer_running(conn)["task_id"] == a
    page = client.get("/inbox").get_data(as_text=True)  # the running timer shows on every page
    assert 'class="running-timer"' in page and "write" in page and "0:00" in page
    client.post(f"/tasks/{b}/start")
    entries = db.time_entries(conn)
    assert [(e["task_id"], e["end_time"] is None) for e in entries] == [(b, True), (a, False)]
    client.post("/timer/stop")
    page = client.get("/inbox").get_data(as_text=True)
    assert db.timer_running(conn) is None and 'class="running-timer"' not in page
    assert 'class="running-timer stopped"' in page  # it stays a while, to finish the task (below)
    assert client.post("/tasks/999/start").status_code == 404


def pill(client):
    m = re.search(r'<div class="running-timer stopped">(.*?)</div>', client.get("/inbox").get_data(as_text=True), re.S)
    return m and m.group(1)


def test_a_stopped_timer_stays_as_a_pill_to_finish_resume_or_dismiss(client, conn):
    tid = db.add_task(conn, "write", step="next", ground=1, today=TODAY)
    db.timer_start(conn, tid, NOW - timedelta(minutes=25))
    client.post("/timer/stop")
    p = pill(client)
    assert f'href="/tasks/{tid}"' in p and "0:25:00" in p
    assert f'action="/tasks/{tid}/complete"' in p and f'action="/tasks/{tid}/start"' in p and 'action="/timer/dismiss"' in p
    client.post("/timer/dismiss")
    assert pill(client) is None
    db.timer_start(conn, tid, NOW - timedelta(minutes=5))  # a new stretch, stopped: shown again
    client.post("/timer/stop")
    assert "0:05:00" in pill(client)
    client.post(f"/tasks/{tid}/complete")
    assert pill(client) is None  # finished: nothing left to do with it


def test_a_stopped_timer_pill_goes_after_ten_minutes(client, conn):
    tid = db.add_task(conn, "write", step="next", ground=1, today=TODAY)
    db.timer_start(conn, tid, NOW - timedelta(minutes=30))
    db.timer_stop(conn, NOW - timedelta(minutes=9))
    assert pill(client)
    db.update_entry(conn, db.time_entries(conn)[0]["id"], end_time=db.iso(NOW - timedelta(minutes=11)))
    assert pill(client) is None


def test_timer_page_start_uses_a_task_or_captures_to_inbox(client, conn):
    t = db.add_task(conn, "Write report", step="next", ground=1)
    client.post("/timer/start", data={"title": "write REPORT "})
    assert db.timer_running(conn)["task_id"] == t
    r = client.post("/timer/start", data={"title": "Yoga"}, follow_redirects=True)
    (yoga,) = db.inbox_tasks(conn)
    assert yoga["title"] == "Yoga" and db.timer_running(conn)["task_id"] == yoga["id"]
    assert "Captured to inbox: Yoga" in r.get_data(as_text=True)
    client.post("/timer/start", data={"title": " "})
    assert db.timer_running(conn)["task_id"] == yoga["id"]  # blank: nothing changes


def test_completing_the_running_task_stops_its_timer(client, conn):
    t = db.add_task(conn, "bins", step="next", ground=1, recur="weekly", due_date="2026-10-22")
    other = db.add_task(conn, "x", step="next", ground=1)
    client.post(f"/tasks/{t}/start")
    client.post(f"/tasks/{other}/complete")
    assert db.timer_running(conn)["task_id"] == t  # another task: timer keeps running
    client.post(f"/tasks/{t}/complete")
    assert db.timer_running(conn) is None
    assert db.get_entry(conn, db.time_entries(conn)[0]["id"])["end_time"] == iso(NOW)


def test_deleting_a_task_abandons_it_then_deletes_it_for_good(client, conn):
    t = db.add_task(conn, "x", step="next", ground=1)
    client.post(f"/tasks/{t}/start")
    client.post(f"/tasks/{t}/delete")
    assert db.get_task(conn, t)["state"] == "abandoned" and db.get_task(conn, t)["completed_at"]
    assert db.timer_running(conn) is None and len(db.time_entries(conn)) == 1  # stopped, time kept
    client.post(f"/tasks/{t}/delete")  # deleting an abandoned task is a hard delete
    assert db.get_task(conn, t) is None and db.time_entries(conn) == []


def test_abandoned_tasks_leave_every_list_and_show_on_done(client, conn):
    pid = db.add_project(conn, "P")
    t = db.add_task(conn, "forgotten idea", step="next", project_id=pid)
    client.post(f"/tasks/{t}/delete")
    assert db.project_tasks(conn, pid) == [] and db.open_tasks(conn) == []
    assert "forgotten idea" not in client.get("/next").get_data(as_text=True)
    page = client.get(f"/projects/{pid}?done=1").get_data(as_text=True)
    assert "forgotten idea" not in page.split("<h2>Abandoned")[0] and "forgotten idea" in page.split("<h2>Abandoned")[1]
    done = client.get("/done").get_data(as_text=True)
    assert "Abandoned" in done and "forgotten idea" in done and f"/tasks/{t}/revive" in done
    assert "forgotten idea" in client.get("/search?q=forgotten").get_data(as_text=True)
    # Completing an abandoned task does nothing.
    client.post(f"/tasks/{t}/complete")
    assert db.get_task(conn, t)["state"] == "abandoned"


def test_revive_brings_an_abandoned_task_back_where_it_was(client, conn):
    pid = db.add_project(conn, "P")
    t = db.add_task(conn, "x", step="next", project_id=pid)
    h0, loose = db.add_task(conn, "h", step="next", ground=1), db.add_task(conn, "loose")
    for i in (t, h0, loose):
        client.post(f"/tasks/{i}/delete")
    r = client.post(f"/tasks/{t}/revive")
    task = db.get_task(conn, t)
    assert task["step"] == "later" and task["completed_at"] is None and task["project_id"] == pid
    assert r.headers["Location"] == "/next"  # no Referer: the default
    client.post(f"/tasks/{h0}/revive")
    client.post(f"/tasks/{loose}/revive")
    assert db.get_task(conn, h0)["ground"] and db.get_task(conn, loose)["inbox"]


def test_edit_and_delete_time_entries(client, conn):
    t = db.add_task(conn, "x", step="next", ground=1)
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
    a = db.add_task(conn, "measure", step="next", project_id=pid)
    b = db.add_task(conn, "tile", step="later", project_id=pid)
    h0 = db.add_task(conn, "stretch", step="next", ground=1)
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
    assert db.week_totals(conn, NOW - timedelta(days=1), NOW) == [("Kitchen", 4800), ("H0 · ground level", 600)]  # by project


def test_task_page_timer_sits_on_the_title_line(client, conn):
    """▶ and the time spent sit on the title line, with the other icons."""
    pid = db.add_project(conn, "Kitchen")
    t = db.add_task(conn, "measure", step="next", project_id=pid)
    h0 = db.add_task(conn, "stretch", ground=1)
    db.timer_start(conn, t, NOW - timedelta(minutes=90))
    db.timer_stop(conn, NOW - timedelta(minutes=30))
    line = lambda tid: (lambda p: p[p.index('class="title-line"'):p.index('<textarea')])(client.get(f"/tasks/{tid}").get_data(as_text=True))
    for tid in (t, h0):
        assert 'class="task-timer"' in line(tid) and "Start the timer" in line(tid)
    assert "1h 00m" in line(t)
    client.post(f"/tasks/{t}/start")
    assert "Stop the timer" in line(t) and 'data-seconds="3600"' in line(t)
    db.complete_task(conn, t, TODAY, NOW)
    assert "1h 00m" in line(t) and "Start the timer" not in line(t)  # done: time shown, no ▶


def test_new_project_from_task_form(client, conn):
    tid = db.add_task(conn, "ring Bob")
    r = client.post(f"/tasks/{tid}", data={"project_id": "__new__", "new_project": " Garden ", "step": "next"})
    assert r.status_code in (200, 302)
    t = db.get_task(conn, tid)
    p = conn.execute("SELECT * FROM projects WHERE title = 'Garden'").fetchone()
    assert p and p["status"] == "active"
    assert t["project_id"] == p["id"] and not t["ground"] and t["step"] == "next"
    assert b'name="new_project"' in client.get(f"/tasks/{tid}").data


def test_new_option_without_a_name_picks_nothing(client, conn):
    """The pickers' "＋ New…" option only reveals the name box; picked with the box left empty, it's no choice."""
    tid = db.add_task(conn, "ring Bob")
    html = client.get(f"/tasks/{tid}").data.decode()
    assert html.count('value="__new__"') == 1
    client.post(f"/tasks/{tid}", data={"project_id": "__new__", "new_project": ""})
    t = db.get_task(conn, tid)
    assert t["project_id"] is None and not t["ground"]
    assert not conn.execute("SELECT 1 FROM projects WHERE title = '__new__'").fetchone()


def test_project_cards_are_plain(client, conn):
    """A project card is its title (⚠ when stalled): no progress fill, step count or idle fade."""
    pid = db.add_project(conn, "Garden")
    db.complete_task(conn, db.add_task(conn, "buy seeds", step="later", project_id=pid), TODAY)
    html = client.get("/projects").data.decode()
    row = re.search(r'<li class="task[^"]*" data-id="%d"(?:(?!</li>).)*' % pid, html, re.S).group(0)
    assert "⚠ Garden" in row and "--done" not in row and "open</span>" not in row and "idle" not in row

def test_project_lists_carry_each_projects_open_steps(client, conn):
    """Projects and Someday rows fold out their open steps, each linking to its task page; no add line."""
    pid = db.add_project(conn, "Garden")
    dig = db.add_task(conn, "dig", step="next", project_id=pid)
    db.complete_task(conn, db.add_task(conn, "buy seeds", step="later", project_id=pid), TODAY)
    sid = db.add_project(conn, "Boat", status="someday")
    sail = db.add_task(conn, "learn to sail", step="later", project_id=sid)
    conn.commit()
    for url, tid, title, done in (("/projects", dig, "dig", "buy seeds"), ("/someday", sail, "learn to sail", None)):
        html = client.get(url).data.decode()
        steps = re.search(r'<ul class="steps">(.*?)</ul>', html, re.S).group(1)
        assert f'href="/tasks/{tid}"' in steps and title in steps, url
        assert "<form" not in steps and (done is None or done not in steps), url
        assert 'class="peek"' in html and 'aria-expanded="false"' in html, url


def test_timer_week_totals_have_bars(client, conn):
    for title, hours in (("dig", 2), ("rake", 1)):
        db.timer_start(conn, db.add_task(conn, title, step="next", project_id=db.add_project(conn, title)),
                       NOW - timedelta(hours=hours))
        db.timer_stop(conn, NOW - timedelta(hours=hours) + timedelta(minutes=30 * hours))
    conn.commit()
    html = client.get("/timer").data.decode()
    widths = [int(w) for w in re.findall(r'<span class="bar"><i style="width:\s*(\d+)%', html)]
    assert widths and widths[0] == 100 and min(widths) < 100, widths


def test_timer_today_tally_beside_week(client, conn):
    """Today counts only entries started since local midnight; This week counts both."""
    for title, start in (("dig", NOW - timedelta(hours=1)), ("rake", NOW - timedelta(days=1))):
        db.timer_start(conn, db.add_task(conn, title, step="next", project_id=db.add_project(conn, title)), start)
        db.timer_stop(conn, start + timedelta(minutes=30))
    conn.commit()
    html = client.get("/timer").data.decode()
    today_col = re.search(r'<section class="tally">\s*<h2>Today\s*<b>0h 30m</b></h2>(.*?)</section>', html, re.S).group(1)
    week_col = re.search(r'<section class="tally">\s*<h2>This week\s*<b>1h 00m</b></h2>(.*?)</section>', html, re.S).group(1)
    assert "dig" in today_col and "rake" not in today_col
    assert "dig" in week_col and "rake" in week_col
    assert "<table" not in today_col + week_col  # bars, not a table


def test_project_page_times_each_step(client, conn):
    """A project page tallies time like the Timer page: the total, then one bar per step, longest first,
    done steps included; untimed steps aren't listed. Each label opens its task."""
    pid = db.add_project(conn, "Kitchen")
    for title, mins in (("measure", 30), ("order", 90), ("idle", 0)):
        t = db.add_task(conn, title, step="later", project_id=pid)
        if mins:
            db.timer_start(conn, t, NOW - timedelta(minutes=mins))
            db.timer_stop(conn, NOW)
    done = db.add_task(conn, "sketch", step="later", project_id=pid)
    db.timer_start(conn, done, NOW - timedelta(minutes=60))
    db.timer_stop(conn, NOW)
    db.complete_task(conn, done, TODAY, NOW)
    html = client.get(f"/projects/{pid}").data.decode()
    # Folded: only the total shows until its "Show split" is clicked.
    tally = re.search(r'<details class="tally wide">\s*<summary><h2>Time\s*<b>3h 00m</b>.*?</summary>(.*?)</details>', html, re.S).group(1)
    assert re.findall(r'class="who"[^>]*>([^<]+)<', tally) == ["order", "sketch", "measure"]
    assert re.findall(r'width:\s*(\d+)%', tally) == ["100", "67", "33"]
    assert f'href="/tasks/{done}"' in tally and "idle" not in tally
    assert "spent" not in html  # the total lives in the tally now
    empty = db.add_project(conn, "Untimed")
    assert 'class="tally"' not in client.get(f"/projects/{empty}").data.decode()


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
    tid = db.add_task(conn, "call", step="next", project_id=pid)
    client.post(f"/tasks/{tid}", data={"title": "call", "due_date": "saturday"})
    assert db.get_task(conn, tid)["due_date"] == "2026-10-24"
    client.post(f"/tasks/{tid}", data={"title": "call", "due_date": "gibberish"})  # keeps the old date
    assert db.get_task(conn, tid)["due_date"] == "2026-10-24"


def test_due_date_box_has_a_calendar_picker(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "call", step="next", project_id=pid)
    html = client.get(f"/tasks/{tid}").get_data(as_text=True)
    assert 'name="due_date"' in html and 'class="pick"' in html and 'type="date" class="picker"' in html


def test_h0_tasks_stay_off_next_actions(client, conn):
    gid = db.add_task(conn, "stretch", step="next", ground=1)
    pid = db.add_project(conn, "Proj")
    db.add_task(conn, "real step", step="next", project_id=pid)
    page = client.get("/next").data
    assert b"real step" in page and b"stretch" not in page
    assert db.counts(conn, date(2026, 10, 22))["next"] == 0  # undated: not in the count
    assert gid in [t["id"] for t in db.h0_actions(conn)]


def test_h0_tab_lists_every_ground_task_by_due_date(client, conn):
    db.add_task(conn, "stretch", step="next", ground=1, due_date="2026-12-01")
    db.add_task(conn, "later bins", step="next", ground=1, recur="weekly", due_date="2026-11-30")
    db.add_task(conn, "soon", step="next", ground=1, due_date="2026-10-25")
    page = client.get("/h0").get_data(as_text=True)
    assert "Upcoming" not in page  # nothing folded away: future repeats are listed too
    assert page.index("soon") < page.index("later bins") < page.index("stretch")
    assert "later bins" not in client.get("/next").get_data(as_text=True)
    # the count is only what is due today or overdue: not undated, not future
    assert db.counts(conn, date(2026, 10, 24))["h0"] == 0
    assert db.counts(conn, date(2026, 10, 25))["h0"] == 1
    assert db.counts(conn, date(2026, 10, 26))["h0"] == 1


def test_next_tab_lists_every_next_action_by_due_date_and_colors_them(client, conn):
    pid, sid = db.add_project(conn, "P"), db.add_project(conn, "S", status="someday")
    db.add_task(conn, "undated", step="next", project_id=pid)
    db.add_task(conn, "weekly far", step="next", recur="weekly", due_date="2026-11-30", project_id=db.add_project(conn, "Q"))
    db.add_task(conn, "overdue one", step="next", due_date="2026-10-20", project_id=db.add_project(conn, "R"))
    db.add_task(conn, "someday step", step="next", project_id=sid)
    db.add_task(conn, "ground", step="next", ground=1)
    html = client.get("/next").get_data(as_text=True)
    assert "Upcoming" not in html and "someday step" not in html and ">ground<" not in html
    assert html.index("overdue one") < html.index("weekly far") < html.index("undated")
    assert db.counts(conn, TODAY)["next"] == 1  # only the overdue one: not undated, not a far repeat


def test_add_h0_task_straight_from_h0_tab(client, conn):
    page = client.get("/h0").data
    assert b'action="/h0/add"' in page
    r = client.post("/h0/add", data={"title": "stretch", "due_date": "", "recur": "daily"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/h0")
    t = conn.execute("SELECT * FROM tasks WHERE title='stretch'").fetchone()
    assert (t["step"], t["ground"], t["project_id"]) == ("next", 1, None)
    assert b"stretch" in client.get("/h0").data
    client.post("/h0/add", data={"title": "  "})  # blank title: nothing added
    client.post("/h0/add", data={"title": "taxes", "due_date": "tomorrow", "recur": ""})
    assert conn.execute("SELECT due_date, ground FROM tasks WHERE title='taxes'").fetchone()[:] == ("2026-10-23", 1)
    assert conn.execute("SELECT COUNT(*) FROM tasks WHERE title=''").fetchone()[0] == 0


def test_default_db_path_sits_next_to_the_code(monkeypatch):
    import importlib
    monkeypatch.delenv("GTD_DB", raising=False)
    try:
        assert importlib.reload(db).DB_PATH == os.path.join(os.path.dirname(os.path.abspath(db.__file__)), "gtd.db")
        monkeypatch.setenv("GTD_DB", "/var/lib/apps/gtd/gtd.db")
        assert importlib.reload(db).DB_PATH == "/var/lib/apps/gtd/gtd.db"
    finally:
        monkeypatch.undo()
        importlib.reload(db)


def test_connect_uses_wal_and_waits_for_locks(tmp_path):
    c = db.connect(str(tmp_path / "w.db"))
    assert c.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert c.execute("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_reorder_project_tasks(client, conn):
    ids = seed(conn)
    titles = lambda: [t["title"] for t in db.project_tasks(conn, ids["active"])]
    assert titles() == ["buy tiles", "quote from Bob", "lay tiles"]  # never dragged: newest first
    r = client.post(f"/projects/{ids['active']}/order", data={"ids": "2,3,4"}, headers={"X-Requested-With": "fetch"})
    assert r.status_code == 204
    assert titles() == ["buy tiles", "lay tiles", "quote from Bob"]
    assert db.get_task(conn, 2)["step"] == "next"  # order only, statuses untouched
    db.add_task(conn, "grout", step="later", project_id=ids["active"])
    assert titles()[1] == "grout"  # new steps go to the top, under the next action
    # Ids from another project are ignored.
    before = db.get_task(conn, 8)["position"]
    client.post(f"/projects/{ids['active']}/order", data={"ids": "8,5,4,3,2"})
    assert titles() == ["buy tiles", "grout", "quote from Bob", "lay tiles"] and db.get_task(conn, 8)["position"] == before


def test_becoming_next_moves_a_step_to_the_top(client, conn):
    pid = db.add_project(conn, "P")
    a, b, c, d = (db.add_task(conn, t, step="later", project_id=pid) for t in "abcd")
    titles = lambda: [t["title"] for t in db.project_tasks(conn, pid)]
    db.reorder_project(conn, pid, [a, b, c, d])
    client.post(f"/tasks/{c}/make-next")
    assert titles() == ["c", "a", "b", "d"]  # the rest shift down in order, not swapped
    client.post(f"/tasks/{d}", data={"title": "d", "step": "next", "project_id": pid})  # the task page's step picker
    assert titles() == ["d", "c", "a", "b"]
    db.add_task(conn, "e", step="next", project_id=pid)
    assert titles()[0] == "e"
    db.reorder_project(conn, pid, [b, a, db.project_next(conn, pid)["id"], c, d])
    assert titles() == ["e", "b", "a", "c", "d"]  # the next action shows first even if dragged down
    db.update_task(conn, a, title="a!")  # saving without touching the step keeps the dragged order
    db.update_task(conn, db.project_next(conn, pid)["id"], step="next")  # already next: nothing renumbered
    assert titles() == ["e", "b", "a!", "c", "d"]


def test_project_next_action_is_pinned_but_has_a_grip_to_move_it_out(client, conn):
    ids = seed(conn)
    page = client.get(f"/projects/{ids['active']}").get_data(as_text=True)
    rows = re.findall(r'<li class="task[^"]*" data-id="(\d+)"( data-pinned)?>\s*<span class="grip" title="([^"]*)"', page)
    assert sorted(rows) == [("2", " data-pinned", "Drag to another project"), ("3", "", "Drag to reorder"), ("4", "", "Drag to reorder")]


def test_done_page_lists_finished_tasks_and_projects(client, conn):
    seed(conn)
    page = client.get("/done").get_data(as_text=True)
    assert "old tiles" in page and "Done proj" in page
    finished = page.split("<h2>Abandoned")[0]
    assert "buy tiles" not in page and "Kitchen" not in finished.split("Projects")[-1]
    assert "dropped idea" in page.split("<h2>Abandoned")[1]
    assert 'href="/done"' in client.get("/next").get_data(as_text=True)


def test_done_toggle_on_lists(client, conn):
    ids = seed(conn)
    older = db.add_task(conn, "older step", step="next", project_id=ids["active"])
    db.complete_task(conn, older, TODAY - timedelta(days=3))
    conn.execute("UPDATE tasks SET completed_at = '2026-10-19 09:00:00' WHERE id = ?", (older,))
    newer = db.add_task(conn, "newer step", step="next", project_id=ids["active"])
    db.complete_task(conn, newer, TODAY)
    conn.execute("UPDATE tasks SET completed_at = '2026-10-22 09:00:00' WHERE id = ?", (newer,))
    conn.commit()
    grounded = db.add_task(conn, "swept porch", step="next", ground=1)
    db.complete_task(conn, grounded, TODAY)

    def page(url):
        return client.get(url).get_data(as_text=True)

    project = f"/projects/{ids['active']}"
    assert "older step" not in page(project) and "Show completed" in page(project)
    shown = page(project + "?done=1")
    assert shown.index("newer step") < shown.index("older step")  # newest completion first
    assert "old tiles" not in shown and "Hide completed" in shown
    assert f'href="{project}">Hide completed' in shown  # the toggle really hides
    assert f'href="{project}?done=1">Show completed' in page(project)
    assert 'href="/next?done=1">Show completed' in page("/next")
    assert 'href="/next">Hide completed' in page("/next?done=1")
    assert "2026-10-22" in shown and "2026-10-19" in shown  # completion dates are shown
    assert "2026-10-22" in page("/done")
    assert "swept porch" in page("/h0?done=1") and "swept porch" not in page("/h0")
    assert "swept porch" not in page("/next?done=1") and "newer step" in page("/next?done=1")
    assert "Done proj" in page("/projects?done=1") and "Done proj" not in page("/projects")
    # a done row can't be ticked off again (that would re-spawn a repeating task)
    assert 'class="done"' not in page("/h0?done=1").split("Completed")[-1]


def test_top_bar_is_the_same_on_every_page(client, conn):
    """Switching tabs must not move the search box or capture button around."""
    ids = seed(conn)
    conn.commit()
    bar = lambda url: re.search(r'<div class="bar">.*?</div>', client.get(url).data.decode(), re.S).group(0)
    first = bar("/inbox")
    for url in ("/next", "/h0", "/projects", f"/projects/{ids['active']}", "/waiting", "/calendar", "/timer", "/review"):
        assert bar(url) == first, url


def test_add_project_step_with_due_repeat_and_notes(client, conn):
    pid = db.add_project(conn, "Garden")
    conn.commit()
    page = client.get(f"/projects/{pid}").data
    for field in (b'name="due_date"', b'name="recur"', b'name="notes"'):
        assert field in page
    client.post(f"/projects/{pid}/tasks", data={"title": "water plants", "step": "next",
                                             "due_date": "tomorrow", "recur": "every 3 days", "notes": "the ferns"})
    t = conn.execute("SELECT * FROM tasks WHERE title='water plants'").fetchone()
    assert (t["project_id"], t["due_date"], t["recur"], t["notes"]) == (pid, "2026-10-23", "every 3 days", "the ferns")


def test_add_waiting_for_on_the_waiting_page(client, conn):
    pid = db.add_project(conn, "Kitchen")
    conn.commit()
    client.post("/waiting/add", data={"title": "quote from Bob", "project_id": str(pid), "due_date": "fri"})
    t = db.tasks_with_step(conn, "waiting")[0]
    assert (t["title"], t["project_id"], t["due_date"]) == ("quote from Bob", pid, "2026-10-23")
    # Neither a project nor H0: it is not dropped, it goes to the inbox to be processed.
    r = client.post("/waiting/add", data={"title": "mystery", "project_id": "", "due_date": ""}, follow_redirects=True)
    assert [t["title"] for t in db.inbox_tasks(conn)] == ["mystery"]
    assert b"went to the inbox" in r.data


def test_add_someday_project_on_the_someday_page(client, conn):
    r = client.post("/someday/add", data={"title": "learn the cello", "kind": "project"})
    assert r.headers["Location"].endswith("/someday")
    assert [p["title"] for p in db.projects(conn, "someday")] == ["learn the cello"]
    client.post("/someday/add", data={"title": "  ", "kind": "project"})
    assert len(db.projects(conn, "someday")) == 1


def test_fuzzy_score():
    import fuzzy
    assert fuzzy.score("bt", "buy tiles") is not None          # letters in order, gaps allowed
    assert fuzzy.score("tb", "buy tiles") is None              # out of order
    assert fuzzy.score("TIL", "buy tiles") is not None         # case-insensitive
    assert fuzzy.score("tiles buy", "buy tiles") is not None   # each word on its own
    assert fuzzy.score("", "anything") is None
    # substring beats scattered letters; word start beats mid-word; shorter title wins ties
    assert fuzzy.score("til", "buy tiles") > fuzzy.score("til", "the itinerary list")
    assert fuzzy.score("ti", "tiles") > fuzzy.score("ti", "the ice")
    assert fuzzy.score("vac", "vacuum") > fuzzy.score("vac", "vacuum the whole house")


def test_search_suggest(client, conn):
    seed(conn)
    r = client.get("/search/suggest?q=tils").get_json()
    titles = [s["title"] for s in r]
    assert titles[:2] == ["buy tiles", "lay tiles"]           # open tasks before the done one
    assert "old tiles" in titles
    by = {s["title"]: s for s in r}
    assert by["buy tiles"]["url"] == "/tasks/2" and by["buy tiles"]["kind"] == "next"
    assert by["old tiles"]["kind"] == "done"
    kitchen = next(s for s in client.get("/search/suggest?q=kitchn").get_json() if s["title"] == "Kitchen")
    assert kitchen["url"].startswith("/projects/") and kitchen["kind"] == "project"
    assert client.get("/search/suggest?q=").get_json() == []
    assert client.get("/search/suggest?q=zzzz").get_json() == []
    assert len(client.get("/search/suggest?q=e").get_json()) <= 10


def test_suggest_puts_a_done_substring_match_above_open_scattered_ones(client, conn):
    """Open titles with t-i-m-e-r scattered in them mustn't crowd out a done task that says "timer"."""
    for i in range(12):
        db.add_task(conn, f"make third midterm {i}")
    t = db.add_task(conn, "move the timer arrow")
    db.complete_task(conn, t, date(2026, 10, 22))
    for i in range(250):  # older done tasks than the cap used to reach
        db.complete_task(conn, db.add_task(conn, f"filler {i}"), date(2026, 10, 22))
    conn.execute("UPDATE tasks SET completed_at = '2020-01-01 00:00:00' WHERE id = ?", (t,))
    conn.commit()
    titles = [s["title"] for s in client.get("/search/suggest?q=timer").get_json()]
    assert titles[0] == "move the timer arrow"


def test_add_a_next_action_on_the_next_page(client, conn):
    """Next has its own add line, so a known next action needn't go through the inbox."""
    garden, busy = db.add_project(conn, "Garden"), db.add_project(conn, "Busy")
    db.add_task(conn, "first", step="next", project_id=busy)
    conn.commit()
    assert 'action="/next/add"' in client.get("/next").data.decode()
    for title, project in (("dig", str(garden)), ("second", str(busy)), ("loose", ""), ("sweep", "h0")):
        client.post("/next/add", data={"title": title, "project_id": project})
    status = {t["title"]: t for t in conn.execute("SELECT * FROM tasks").fetchall()}
    assert status["dig"]["step"] == "next"
    assert status["second"]["step"] == "later"  # Busy already has its one next action
    assert status["first"]["step"] == "next"
    assert status["loose"]["project_id"] is None and not status["loose"]["ground"]  # an inbox item, to be processed
    assert status["sweep"]["step"] == "next" and status["sweep"]["ground"]


def test_nav_groups_tabs_by_kind_of_list(client):
    """Working minute by minute, task lists, lists of projects; then the weekly-review pages."""
    nav = client.get("/next").data.decode().split("<nav>")[1].split("</nav>")[0]
    groups = [re.findall(r'<a [^>]*>([A-Za-z0-9]+)', g) for g in re.findall(r'<span class="(?:group|aux)">(.*?)</span>', nav, re.S)]
    assert groups == [["Timer", "Calendar"], ["Next", "H0", "Waiting", "Inbox"], ["Projects", "Someday", "Reference"],
                      ["Review", "Horizons", "Done"]]


def test_review_is_a_checklist_you_finish(client, conn):
    steps = REVIEW_STEPS
    html = client.get("/review").get_data(as_text=True)
    assert html.count('class="dot') == len(steps) and 'class="dot done' not in html
    assert "No weekly reviews yet." in html
    client.post(f"/review/{steps[0]}")
    assert db.review_checked(conn) == {steps[0]}
    r = client.post(f"/review/{steps[0]}")  # ticking again unticks, and stays on that step
    assert db.review_checked(conn) == set() and r.headers["Location"] == f"/review?step={steps[0]}"
    for s in steps[:-1]:
        client.post(f"/review/{s}")
    assert client.get("/review").get_data(as_text=True).count('class="dot done') == len(steps) - 1
    assert db.reviews(conn) == []
    r = client.post(f"/review/{steps[-1]}", follow_redirects=True)
    html = r.get_data(as_text=True)
    assert "Weekly review completed on Thu 22 Oct 2026." in html
    assert db.reviews(conn) == ["2026-10-22"] and db.review_checked(conn) == set()  # fresh for next week
    assert "Past reviews" in html and "Thu 22 Oct 2026" in html.split("Past reviews", 1)[1]
    assert client.post("/review/bogus").status_code == 404


def step_card(html, step):
    """The one review step shown, and its key."""
    m = re.search(r'<li id="(\w+)" class="step on[^"]*">(.*?)</li>\s*(?=<li id=|</ol>)', html, re.S)
    return m and m.group(1), m and m.group(2)


def test_review_shows_one_step_at_a_time(client, conn):
    seed(conn)  # sweep is ticked
    client.post("/review/collect")
    html = client.get("/review").get_data(as_text=True)
    assert html.count('class="step on') == 1
    assert step_card(html, "inbox")[0] == "inbox"  # the first unticked step
    assert 'href="/review?step=collect" class="dot done"' in html  # rail: ticked, then current
    assert 'href="/review?step=inbox" class="dot on"' in html
    key, card = step_card(client.get("/review?step=overdue").get_data(as_text=True), "overdue")
    assert key == "overdue" and '<span class="count">1</span>' in card and "buy tiles" in card
    key, card = step_card(client.get("/review?step=projects").get_data(as_text=True), "projects")
    assert "⚠ Stalled" in card  # no next action
    key, card = step_card(client.get("/review?step=inbox").get_data(as_text=True), "inbox")
    assert 'href="/inbox"' in card and "/process" not in card  # deal with it on the Inbox page
    r = client.post("/review/inbox")  # ticking moves on to the next unticked step (sweep is ticked)
    assert r.headers["Location"] == "/review"
    assert step_card(client.get("/review").get_data(as_text=True), "overdue")[0] == "overdue"
    for t in db.inbox_tasks(conn):
        db.abandon_task(conn, t["id"])
    assert db.counts(conn, TODAY)["inbox"] == 1  # a tickled someday task is back in it too
    for x in db.tickled(conn, TODAY):
        db.update_task(conn, x["id"], due_date=None)
    key, card = step_card(client.get("/review?step=inbox").get_data(as_text=True), "inbox")
    assert '<span class="count clear">✓</span>' in card  # nothing to do
    assert client.get("/review?step=bogus").status_code == 200  # falls back to the first unticked


def test_review_prev_next_links(client, conn):
    html = client.get("/review?step=overdue").get_data(as_text=True)
    assert 'href="/review?step=sweep" rel="prev"' in html and 'href="/review?step=nexts" rel="next"' in html
    first = client.get("/review?step=collect").get_data(as_text=True)
    assert 'rel="prev"' not in first and 'href="/review?step=inbox" rel="next"' in first
    last = client.get("/review?step=horizons").get_data(as_text=True)
    assert 'rel="next"' not in last and 'href="/review?step=creative" rel="prev"' in last


def test_review_past_reviews_as_a_streak(client, conn):
    for days in (7, 21):
        db.complete_review(conn, TODAY - timedelta(days=days))
    html = client.get("/review").get_data(as_text=True).split("Past reviews", 1)[1]
    weeks = re.findall(r'<i class="wk( hit)?"', html)
    assert len(weeks) == 26
    assert [bool(h) for h in weeks][-4:] == [True, False, True, False]  # oldest first; this week not done yet
    assert 'title="Thu 15 Oct 2026"' in html and 'title="Thu 1 Oct 2026"' in html


def test_actions_stay_on_the_page_they_were_made_on(client, conn):
    """Nothing jumps elsewhere unless you follow a link or press "Save & exit"."""
    def loc(r):
        return r.headers["Location"].removeprefix("http://localhost")
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", step="next", project_id=pid)
    b = db.add_task(conn, "b", step="later", project_id=pid)
    task_page = f"http://localhost/tasks/{a}?back=/someday"
    # Finishing a project's next action: the row just fades, from a list or the task page.
    assert client.post(f"/tasks/{b}/make-next").status_code == 302
    assert client.post(f"/tasks/{b}/complete", headers={"X-Requested-With": "fetch"}).status_code == 204
    r = client.post(f"/tasks/{a}/complete", data={"next": "/someday"}, headers={"Referer": task_page})
    assert loc(r) == f"/tasks/{a}?back=/someday"
    # Abandon and revive from the task page stay on it.
    c = db.add_task(conn, "c", step="next", project_id=pid)
    page = f"http://localhost/tasks/{c}"
    assert loc(client.post(f"/tasks/{c}/delete", data={"next": "/next"}, headers={"Referer": page})) == f"/tasks/{c}"
    assert loc(client.post(f"/tasks/{c}/revive", headers={"Referer": "http://localhost/done"})) == "/done"
    # Moving the project to someday from a task page stays on the task page; done/waiting stay on the project.
    r = client.post(f"/projects/{pid}", data={"status": "someday"}, headers={"Referer": page})
    assert loc(r) == f"/tasks/{c}"
    r = client.post(f"/projects/{pid}", data={"status": "waiting"}, headers={"Referer": f"http://localhost/projects/{pid}"})
    assert loc(r) == f"/projects/{pid}"
    # Creating a project or a horizon stays on the list.
    assert loc(client.post("/projects", data={"title": "New"}, headers={"Referer": "http://localhost/projects"})) == "/projects"
    r = client.post("/horizons", data={"level": "goal", "title": "G"}, headers={"Referer": "http://localhost/horizons"})
    assert loc(r) == "/horizons"
    # The calendar stays on the month you're looking at, whatever the new task's date.
    r = client.post("/calendar", data={"title": "x", "due_date": "2027-04-15", "project_id": "h0"},
                    headers={"Referer": "http://localhost/calendar?m=2026-10"})
    assert loc(r) == "/calendar?m=2026-10"


def test_favicon(client):
    html = client.get("/next").get_data(as_text=True)
    assert 'rel="icon"' in html
    r = client.get("/static/favicon.svg")
    assert r.status_code == 200 and b"<svg" in r.data


def test_reference_page_shows_a_url_once(client, conn):
    rid = db.add_reference(conn, title="Site", notes="https://example.com")
    html = client.get(f"/reference/{rid}").get_data(as_text=True)
    assert html.count("https://example.com") == 1  # the notes editor shows and opens it; no second list


# --- review fixes (2026-10-24) ----------------------------------------------

def test_reviving_a_step_reopens_its_done_project(client, conn):
    pid = db.add_project(conn, "Q")
    t = db.add_task(conn, "q1", project_id=pid, step="next")
    client.post(f"/tasks/{t}/delete")
    client.post(f"/projects/{pid}", data={"status": "done"})
    assert db.get_project(conn, pid)["completed_at"]
    client.post(f"/tasks/{t}/revive")
    p = db.get_project(conn, pid)
    assert (p["status"], p["completed_at"]) == ("active", None)


def test_reactivating_a_project_clears_its_completion(client, conn):
    pid = db.add_project(conn, "P")
    db.update_project(conn, pid, status="done")
    db.update_project(conn, pid, status="active")
    assert db.get_project(conn, pid)["completed_at"] is None


def test_completion_dates_show_in_local_time(client, conn, monkeypatch):
    import time
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    try:
        t = db.add_task(conn, "late night", ground=1)
        db.complete_task(conn, t, TODAY)
        conn.execute("UPDATE tasks SET completed_at = '2026-10-23 01:30:00' WHERE id = ?", (t,))  # 21:30 on the 22nd
        conn.commit()
        html = client.get("/h0?done=1").get_data(as_text=True)
        assert "2026-10-22" in html and "2026-10-23" not in html
    finally:
        monkeypatch.undo()
        time.tzset()


def test_calendar_shows_an_overdue_repeat_only_on_its_due_date(client, conn):
    db.add_task(conn, "overdue daily", ground=1, recur="daily", due_date="2026-10-15")
    assert client.get("/calendar?w=2026-10-15").get_data(as_text=True).count(">↻ overdue daily<") == 1
    assert "overdue daily" not in client.get("/calendar?w=2026-10-22").get_data(as_text=True)


@pytest.mark.parametrize("rule,after", [
    ("every 2 weeks after  done", True), ("weekly after done", True), ("Daily after completion", True),
    ("every 2 weeks", False), ("monthly", False),
])
def test_after_done_matches_the_parser(rule, after):
    assert recur.parse(rule) and recur.after_done(rule) == after


def test_toggle_url_survives_a_clashing_query_arg(client, conn):
    pid = db.add_project(conn, "P")
    assert client.get(f"/projects/{pid}?id=99").status_code == 200


def test_new_project_is_not_created_without_a_task(client, conn):
    for url in ("/next/add", "/waiting/add", "/calendar"):
        client.post(url, data={"title": " ", "project_id": "__new__", "new_project": "Ghost"})
    pid = db.add_project(conn, "P")
    t = db.add_task(conn, "t", project_id=pid, step="next")
    client.post(f"/tasks/{t}", data={"title": "t", "due_date": "bogus date", "project_id": "__new__", "new_project": "Real"})
    assert [p["title"] for _, ps, _ in db.project_groups(conn) for p in ps] == ["Real", "P"]  # the task page still files it


def test_h0_task_can_join_a_project_directly(client, conn):
    pid = db.add_project(conn, "P")
    g = db.add_task(conn, "h", ground=1)
    db.update_task(conn, g, project_id=pid)
    t = db.get_task(conn, g)
    assert (t["ground"], t["step"], t["project_id"]) == (0, "later", pid)


def test_review_overdue_skips_parked_projects(client, conn):
    ids = seed(conn)
    parked = db.add_task(conn, "parked overdue", project_id=ids["someday"], step="waiting", due_date="2026-10-01")
    assert parked not in [t["id"] for t in db.overdue(conn, TODAY)]
    assert "buy tiles" in [t["title"] for t in db.overdue(conn, TODAY)]


def test_next_repeat_keeps_its_place_in_the_project(client, conn):
    pid = db.add_project(conn, "P")
    a = db.add_task(conn, "a", project_id=pid, step="next", recur="weekly", due_date="2026-10-22")
    b = db.add_task(conn, "b", project_id=pid, step="later")
    db.reorder_project(conn, pid, [a, b])
    new = db.complete_task(conn, a, TODAY)
    assert [t["id"] for t in db.project_tasks(conn, pid)] == [new, b]
    assert db.get_task(conn, new)["step"] == "later"


def test_project_page_rows_line_up(client, conn):
    """Every open step shows its step, and the step, date and action slots exist on every row, so columns line up."""
    pid = db.add_project(conn, "Columns")
    db.add_task(conn, "n", step="next", project_id=pid, due_date="2026-10-30")
    db.add_task(conn, "w", step="waiting", project_id=pid)
    db.add_task(conn, "l", step="later", project_id=pid)
    html = client.get(f"/projects/{pid}").get_data(as_text=True)
    rows = re.findall(r'<li class="task.*?</li>', html, re.S)
    assert len(rows) == 3
    for row, step in zip(rows, ("next", "later", "waiting")):
        assert f'<span class="step">{step}</span>' in row
        assert row.count('class="date"') == 1
        assert row.count('class="make-next') == 1


@pytest.mark.parametrize("status, page", [("someday", "/someday"), ("active", "/projects")])
def test_projects_reorder(client, conn, status, page):
    """Project rows keep the order they're dragged to; new ones go first. Other lists are ignored."""
    a, b, c = (db.add_project(conn, t, status=status) for t in ("a", "b", "c"))
    other = db.add_project(conn, "other", status="waiting")
    assert [p["title"] for p in db.projects(conn, status)] == ["c", "b", "a"]
    before = db.get_project(conn, other)["position"]
    assert client.post(f"/projects/order/{status}", data={"ids": f"{c},{other},{a},{b}"},
                       headers={"X-Requested-With": "fetch"}).status_code == 204
    assert [p["title"] for p in db.projects(conn, status)] == ["c", "a", "b"]
    assert db.get_project(conn, other)["position"] == before
    html = client.get(page).get_data(as_text=True)
    assert f'data-reorder="/projects/order/{status}"' in html and html.count('class="grip"') == 3


def test_moved_project_goes_to_the_top_of_its_new_list(client, conn):
    a, b = db.add_project(conn, "a"), db.add_project(conn, "b")
    s = db.add_project(conn, "s", status="someday")
    db.reorder_projects(conn, "active", [b, a])
    db.reorder_projects(conn, "someday", [s])
    db.update_project(conn, s, status="active")
    assert [p["title"] for p in db.projects(conn)] == ["s", "b", "a"]


# --- autosave --------------------------------------------------------------

@pytest.mark.parametrize("kind,add,get", [
    ("tasks", lambda c: db.add_task(c, "Old", notes="a"), db.get_task),
    ("projects", lambda c: db.add_project(c, "Old", notes="a"), db.get_project),
    ("horizons", lambda c: db.add_horizon(c, "goal", "Old"), db.get_horizon),
    ("reference", lambda c: db.add_reference(c, "Old", notes="a"), db.get_reference),
])
def test_autosave_writes_only_the_fields_sent(client, conn, kind, add, get):
    id = add(conn)
    before = get(conn, id)
    r = client.post(f"/autosave/{kind}/{id}", data={"notes": "new notes\n", "base_notes": before["notes"]})
    assert r.status_code == 200
    assert get(conn, id)["notes"] == "new notes" and get(conn, id)["title"] == "Old"
    assert client.get(f"/autosave/{kind}/{id}").json == {"title": "Old", "notes": "new notes"}


def test_autosave_refuses_a_field_changed_elsewhere(client, conn):
    id = db.add_reference(conn, "Mine", notes="base")
    db.update_reference(conn, id, notes="theirs")
    r = client.post(f"/autosave/reference/{id}", data={"notes": "mine", "base_notes": "base"})
    assert r.status_code == 409
    assert r.json == {"title": "Mine", "notes": "theirs"}
    assert db.get_reference(conn, id)["notes"] == "theirs"
    # The same text on both sides isn't a conflict, and neither is another field changing.
    assert client.post(f"/autosave/reference/{id}", data={"notes": "theirs", "base_notes": "base"}).status_code == 200
    assert client.post(f"/autosave/reference/{id}", data={"title": "T", "base_title": "Mine"}).status_code == 200
    assert db.get_reference(conn, id)["title"] == "T"


def test_autosave_ignores_a_blank_title_and_a_missing_record(client, conn):
    id = db.add_reference(conn, "Keep")
    assert client.post(f"/autosave/reference/{id}", data={"title": " ", "base_title": "Keep"}).status_code == 200
    assert db.get_reference(conn, id)["title"] == "Keep"
    assert client.post("/autosave/reference/999", data={"notes": "x", "base_notes": ""}).status_code == 404


def test_project_page_hides_completed_by_default(client, conn):
    pid = db.add_project(conn, "P")
    t = db.add_task(conn, "Finished step", step="next", project_id=pid)
    db.complete_task(conn, t, TODAY)
    assert "Finished step" not in client.get(f"/projects/{pid}").get_data(as_text=True)
    assert "Finished step" in client.get(f"/projects/{pid}?done=1").get_data(as_text=True)


FETCH = {"X-Requested-With": "fetch"}


def test_inbox_drag_moves_an_item_into_a_project(client, conn):
    a, b = db.add_task(conn, "a"), db.add_task(conn, "b")
    pid = db.add_project(conn, "P")
    db.add_task(conn, "p next", project_id=pid, step="next")
    assert client.post("/tasks/move", data={"id": a, "project_id": str(pid)}, headers=FETCH).status_code == 204
    assert (db.get_task(conn, a)["project_id"], db.get_task(conn, a)["step"]) == (pid, "later")
    assert db.get_task(conn, b)["inbox"]
    assert b"Moved" not in client.get("/inbox").data  # no flash left over for the next page
    # Dropped on "＋ New…" with a name: a new project.
    assert client.post("/tasks/move", data={"id": b, "new_project": "Fresh"}, headers=FETCH).status_code == 204
    fresh = [p for _, ps, _ in db.project_groups(conn) for p in ps if p["title"] == "Fresh"]
    assert len(fresh) == 1 and db.get_task(conn, b)["project_id"] == fresh[0]["id"]


def test_inbox_drag_edges(client, conn):
    a = db.add_task(conn, "a")
    pid = db.add_project(conn, "P")
    filed = db.add_task(conn, "filed", project_id=pid, step="next")
    other = db.add_project(conn, "Other")
    # No project: it stays in the inbox.
    assert client.post("/tasks/move", data={"id": a, "project_id": ""}, headers=FETCH).status_code == 400
    assert db.get_task(conn, a)["inbox"]
    # H0 is a choice too.
    assert client.post("/tasks/move", data={"id": a, "project_id": "h0"}, headers=FETCH).status_code == 204
    assert db.get_task(conn, a)["ground"] == 1
    # A finished task is left alone, and no project is created for it.
    db.complete_task(conn, filed, TODAY)
    assert client.post("/tasks/move", data={"id": filed, "project_id": str(other)}, headers=FETCH).status_code == 400
    assert client.post("/tasks/move", data={"id": filed, "new_project": "Ghost"},
                       headers=FETCH).status_code == 400
    assert db.get_task(conn, filed)["project_id"] == pid
    assert not [p for _, ps, _ in db.project_groups(conn) for p in ps if p["title"] == "Ghost"]


def test_dragging_moves_any_open_task_between_projects(client, conn):
    a, b = db.add_project(conn, "A"), db.add_project(conn, "B")
    nxt = db.add_task(conn, "a next", project_id=a, step="next")
    wait = db.add_task(conn, "a waiting", project_id=a, step="waiting")
    b_next = db.add_task(conn, "b next", project_id=b, step="next")
    move = lambda id, target: client.post("/tasks/move", data={"id": id, "project_id": target}, headers=FETCH).status_code
    # A next action lands as a later step: B keeps its own next, A stalls until you pick one.
    assert move(nxt, str(b)) == 204
    assert (db.get_task(conn, nxt)["project_id"], db.get_task(conn, nxt)["step"]) == (b, "later")
    assert db.get_task(conn, b_next)["step"] == "next" and not db.project_next(conn, a)
    # A waiting step stays waiting, so it stays on the Waiting list.
    assert move(wait, str(b)) == 204
    assert (db.get_task(conn, wait)["project_id"], db.get_task(conn, wait)["step"]) == (b, "waiting")
    # Dropped on its own project: nothing changes, not even its step.
    assert move(b_next, str(b)) == 204 and db.get_task(conn, b_next)["step"] == "next"
    # Out to H0 and back into a project.
    assert move(b_next, "h0") == 204
    assert (db.get_task(conn, b_next)["ground"], db.get_task(conn, b_next)["step"], db.get_task(conn, b_next)["project_id"]) == (1, "next", None)
    assert move(b_next, str(a)) == 204
    assert (db.get_task(conn, b_next)["ground"], db.get_task(conn, b_next)["step"]) == (0, "later")
    # A project step not worth doing is kept as reference.
    assert move(wait, "reference") == 204 and db.get_task(conn, wait) is None
    assert [r["title"] for r in db.references(conn)] == ["a waiting"]


def test_every_task_list_has_the_drop_panel(client, conn):
    ids = seed(conn)
    for url in ("/inbox", "/next", "/h0", "/waiting", f"/projects/{ids['active']}"):
        html = client.get(url).get_data(as_text=True)
        assert 'class="drop-panel"' in html and 'class="pin-panel"' in html, url
    # A project page doesn't offer the project itself.
    html = client.get(f"/projects/{ids['active']}").get_data(as_text=True)
    assert f'data-current="{ids["active"]}"' in html


def test_inbox_lists_projects_as_drop_targets_and_has_no_tick_boxes(client, conn):
    db.add_task(conn, "a")
    pid = db.add_project(conn, "P")
    html = client.get("/inbox").data.decode()
    for target in (str(pid), "h0", "__new__"):
        assert f'data-project="{target}"' in html
    assert 'type="checkbox"' not in html


# --- review fixes (2026-10-27) ---------------------------------------------

def test_someday_activate_sends_only_the_status(client, conn):
    """Activate on Someday mustn't resend the notes the page loaded with, or newer notes get overwritten."""
    pid = db.add_project(conn, "Boat", status="someday", notes="old")
    html = client.get("/someday").get_data(as_text=True)
    form = re.search(r'<form[^>]*action="/projects/%d"[^>]*>(.*?)</form>' % pid, html, re.S).group(1)
    assert 'name="notes"' not in form and 'name="title"' not in form
    db.update_project(conn, pid, notes="newer")
    client.post(f"/projects/{pid}", data={"status": "active"})
    p = db.get_project(conn, pid)
    assert (p["status"], p["notes"], p["title"]) == ("active", "newer", "Boat")


def test_date_button_does_not_share_the_calendar_class(client, conn):
    seed(conn)
    html = client.get("/tasks/2").get_data(as_text=True)
    assert 'class="pick"' in html and 'class="cal"' not in html


def test_horizons_h0_rows_have_no_h0_tag(client, conn):
    seed(conn)
    html = client.get("/horizons?level=ground").get_data(as_text=True)
    assert "vacuum" in html and "Ground level: no project, on purpose" not in html


def test_horizons_has_one_add_line_with_a_level(client, conn):
    html = client.get("/horizons?level=area").get_data(as_text=True)
    assert len(re.findall(r'<form[^>]*class="add', html)) == 1
    client.post("/horizons", data={"level": "area", "title": "Health"})
    assert [h["title"] for h in db.horizons(conn)["area"]] == ["Health"]


def test_search_lists_projects_and_task_rows(client, conn):
    pid = db.add_project(conn, "Tile shop")
    done = db.add_task(conn, "tile order", step="next", project_id=pid)
    db.complete_task(conn, done, TODAY)
    html = client.get("/search?q=tile").get_data(as_text=True)
    assert re.search(r'<h2>Projects</h2>\s*<ul class="tasks">', html)
    done_part = html.split("<h2>Done</h2>")[1]
    assert 'class="check"' in done_part and 'class="name"' in done_part  # row(): ✓ and the project after the title


def test_waiting_with_only_projects_has_no_empty_tasks_section(client, conn):
    db.add_project(conn, "Visa", status="waiting")
    html = client.get("/waiting").get_data(as_text=True)
    assert "Visa" in html and "<h2>Tasks</h2>" not in html and "empty clear" not in html


def test_review_count_is_not_a_big_title(client, conn):
    seed(conn)
    html = client.get("/review?step=inbox").get_data(as_text=True)
    assert 'class="count"' in html and 'class="big' not in html


def test_revive_and_activate_buttons_are_not_make_next(client, conn):
    seed(conn)
    done = client.get("/done").get_data(as_text=True)
    assert re.search(r'class="row-btn"><button[^>]*>Revive', done)
    assert 'class="row-btn"' in client.get("/someday?tickled=1").get_data(as_text=True)


def test_style_has_no_dead_or_undefined_variables():
    css = open(os.path.join(os.path.dirname(db.__file__), "static", "style.css")).read()
    defined = set(re.findall(r"(--[\w-]+):", css))
    used = set(re.findall(r"var\((--[\w-]+)", css))
    assert used - defined == set()
    assert defined - used == set()


def test_project_pickers_are_one_list(client, conn):
    assert [(label, ps) for label, ps, _ in db.project_groups(conn)] == [("Projects", [])]  # "＋ New…" even with none
    ids = seed(conn)
    pid = db.add_project(conn, "Another active")
    db.reorder_projects(conn, "active", [pid, ids["active"], ids["stalled"]])  # dragged first on Projects, so first here too
    # Active then waiting then someday; then done. No abandoned one.
    assert [(label, [p["title"] for p in ps]) for label, ps, _ in db.project_groups(conn)] == [
        ("Projects", ["Another active", "Kitchen", "Stalled", "Waiting proj", "Someday proj"]), ("Done", ["Done proj"])]
    expected = [(label, [str(p["id"]) for p in ps]) for label, ps, _ in db.project_groups(conn)]

    def groups(html, group, item):
        return [(label, re.findall(item, body)) for label, body in re.findall(group, html, re.S)]

    db.add_task(conn, "loose")
    task = client.get("/tasks/1").data.decode()
    assert groups(task, r'<optgroup label="([\w ]+)">(.*?)</optgroup>', r'<option value="(\d+)"') == expected
    inbox = client.get("/inbox").data.decode()
    assert groups(inbox, r'<section class="group"><h3>([\w ]+)</h3>(.*?)</section>', r'data-project="(\d+)"') == expected
    assert "Someday proj · someday" in task and "Dropped proj" not in task + inbox
    # "＋ New project…" once, in both.
    assert task.count('value="__new__"') == 1 and inbox.count('data-project="__new__"') == 1


def test_filing_into_a_done_project_reopens_it(client, conn):
    pid = db.add_project(conn, "Old", status="done")
    a = db.add_task(conn, "a")
    assert client.post("/tasks/move", data={"id": a, "project_id": str(pid)}, headers=FETCH).status_code == 204
    assert (db.get_project(conn, pid)["status"], db.get_project(conn, pid)["completed_at"]) == ("active", None)
    done = db.add_project(conn, "Finished", status="done")
    db.add_task(conn, "b", project_id=done, step="next")
    assert db.get_project(conn, done)["status"] == "active"
    # A done task joining it doesn't.
    gone = db.add_project(conn, "Gone", status="done")
    t = db.add_task(conn, "c")
    db.complete_task(conn, t, TODAY)
    db.update_task(conn, t, project_id=gone)
    assert db.get_project(conn, gone)["status"] == "done"


# --- reminders ---------------------------------------------------------------

def local(iso):
    """A stored UTC time as a naive local datetime, so assertions hold in any timezone."""
    return datetime.fromisoformat(iso).astimezone().replace(tzinfo=None)


AT = datetime(2026, 10, 22, 14, 30)  # Thursday afternoon, local


@pytest.mark.parametrize("text, expected", [
    ("tomorrow 9am", datetime(2026, 10, 23, 9, 0)), ("fri 14:00", datetime(2026, 10, 23, 14, 0)),
    ("oct 30 at 5pm", datetime(2026, 10, 30, 17, 0)), ("sat 9:15 pm", datetime(2026, 10, 24, 21, 15)),
    ("9am tomorrow", datetime(2026, 10, 23, 9, 0)), ("tomorrow noon", datetime(2026, 10, 23, 12, 0)),
    ("today 12am", datetime(2026, 10, 22, 0, 0)), ("today 12pm", datetime(2026, 10, 22, 12, 0)),
    ("5pm", datetime(2026, 10, 22, 17, 0)), ("9am", datetime(2026, 10, 23, 9, 0)),  # the next one
    ("tomorrow", datetime(2026, 10, 23, 9, 0)), ("in 2 hours", datetime(2026, 10, 22, 16, 30)),
    ("in 30 min", datetime(2026, 10, 22, 15, 0)), ("in 3 days", datetime(2026, 10, 25, 9, 0)),
    ("2026-10-30 09:00", datetime(2026, 10, 30, 9, 0)), ("2026-10-30T18:45", datetime(2026, 10, 30, 18, 45)),
    ("tomorrow 25:00", None), ("tomorrow 13pm", None), ("blah 9am", None), ("blah", None), ("", None),
])
def test_natural_times(text, expected):
    assert dates.parse_when(text, AT) == expected


def test_task_page_sets_a_reminder_in_words(client, conn):
    tid = db.add_task(conn, "call", ground=1)
    client.post(f"/tasks/{tid}", data={"title": "call", "remind_at": "tomorrow 9am"})
    assert local(db.get_task(conn, tid)["remind_at"]) == datetime(2026, 10, 23, 9, 0)
    html = client.get(f"/tasks/{tid}").get_data(as_text=True)
    assert 'name="remind_at" value="2026-10-23 09:00"' in html
    client.post(f"/tasks/{tid}", data={"title": "call", "remind_at": "gibberish"}, follow_redirects=True)
    assert local(db.get_task(conn, tid)["remind_at"]) == datetime(2026, 10, 23, 9, 0)  # kept
    client.post(f"/tasks/{tid}", data={"title": "call", "remind_at": ""})
    assert db.get_task(conn, tid)["remind_at"] is None


def test_an_unparseable_reminder_is_flashed(client, conn):
    tid = db.add_task(conn, "call", ground=1)
    r = client.post(f"/tasks/{tid}", data={"title": "call", "remind_at": "gibberish"}, follow_redirects=True)
    assert "Didn&#39;t understand reminder" in r.get_data(as_text=True)


def test_changing_a_reminder_rearms_it(conn):
    tid = db.add_task(conn, "call", ground=1, remind_at="2026-10-22T13:00:00+00:00")
    db.mark_reminded(conn, tid, NOW)
    db.update_task(conn, tid, title="call", remind_at="2026-10-22T13:00:00+00:00")  # unchanged: stays sent
    assert db.get_task(conn, tid)["reminded_at"]
    db.update_task(conn, tid, remind_at="2026-10-23T13:00:00+00:00")
    assert db.get_task(conn, tid)["reminded_at"] is None


def test_due_reminders_are_sent_once_and_retried_on_failure(client, conn):
    from app import send_reminders
    due = db.add_task(conn, "due", ground=1, remind_at=db.iso(NOW - timedelta(minutes=1)))
    db.add_task(conn, "later", ground=1, remind_at=db.iso(NOW + timedelta(minutes=1)))
    db.complete_task(conn, db.add_task(conn, "done", ground=1, remind_at=db.iso(NOW - timedelta(hours=1))), TODAY)
    db.abandon_task(conn, db.add_task(conn, "dropped", remind_at=db.iso(NOW - timedelta(hours=1))))

    def fail(t, at):
        raise OSError("ntfy down")
    send_reminders(conn, NOW, fail)
    assert db.get_task(conn, due)["reminded_at"] is None
    sent = []
    send_reminders(conn, NOW, lambda t, at: sent.append(t))
    assert [t["title"] for t in sent] == ["due"]
    send_reminders(conn, NOW, lambda t, at: sent.append(t))
    assert len(sent) == 1


def capture_ntfy(monkeypatch, gtd_url="https://home.example.ts.net"):
    """Point ntfy() at a fake urlopen; returns the list each request's URL and JSON body land in."""
    import json
    import app as app_module
    got = []

    def urlopen(req, timeout):
        got.append({"url": req.full_url, "json": json.loads(req.data), "timeout": timeout})
    monkeypatch.setattr(app_module.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(app_module, "NTFY_URL", "http://127.0.0.1:5007/gtd")
    monkeypatch.setattr(app_module, "GTD_URL", gtd_url)
    return got


def test_ntfy_publishes_title_context_link_and_buttons(client, conn, monkeypatch):
    from app import ntfy
    got = capture_ntfy(monkeypatch)
    pid = db.add_project(conn, "Kitchen")
    tid = db.add_task(conn, "call plumber ☎", project_id=pid, step="next", due_date="2026-10-30",
                      notes="\n  ask about the leak  \nsecond line", remind_at=db.iso(NOW))
    with app.test_request_context():
        ntfy(db.due_reminders(conn, NOW)[0], NOW)
    base = "https://home.example.ts.net"
    assert got == [{"url": "http://127.0.0.1:5007/", "timeout": 10, "json": {
        "topic": "gtd", "title": "call plumber ☎", "message": "Kitchen · due 2026-10-30\nask about the leak",
        "tags": ["bell"], "click": f"{base}/tasks/{tid}",
        "actions": [
            {"action": "http", "label": "Done", "url": f"{base}/tasks/{tid}/complete", "method": "POST",
             "headers": {"X-Requested-With": "fetch"}, "clear": True},
            {"action": "http", "label": "Snooze 15m", "url": f"{base}/tasks/{tid}/snooze", "method": "POST",
             "clear": True}]}}]


def test_ntfy_names_h0_and_inbox_and_has_no_buttons_without_an_address(client, conn, monkeypatch):
    from app import ntfy
    got = capture_ntfy(monkeypatch, gtd_url="")
    db.add_task(conn, "water plants", ground=1, due_date="2026-10-22", remind_at=db.iso(NOW))
    db.add_task(conn, "stray thought", remind_at=db.iso(NOW))
    with app.test_request_context():
        for t in db.due_reminders(conn, NOW):
            ntfy(t, NOW)
    assert [g["json"]["message"] for g in got] == ["H0 · due 2026-10-22", "Inbox"]
    assert all("click" not in g["json"] and "actions" not in g["json"] for g in got)


def test_a_late_reminder_says_when_it_was_due(client, conn, monkeypatch):
    from app import ntfy
    got = capture_ntfy(monkeypatch)
    at = datetime(2026, 10, 22, 14, 0).astimezone()
    db.add_task(conn, "on time", remind_at=db.iso(at - timedelta(minutes=1)))
    db.add_task(conn, "late", remind_at=db.iso(at - timedelta(minutes=30)))
    db.add_task(conn, "days late", remind_at=db.iso(at - timedelta(days=2)))
    with app.test_request_context():
        for t in db.due_reminders(conn, at):
            ntfy(t, at)
    assert {g["json"]["title"]: g["json"]["message"] for g in got} == {
        "on time": "Inbox", "late": "Inbox\nLate: was due 13:30", "days late": "Inbox\nLate: was due 2026-10-20 14:00"}


def test_snooze_pushes_the_reminder_15_minutes_from_now_and_rearms_it(client, conn):
    tid = db.add_task(conn, "call", ground=1, remind_at=db.iso(NOW - timedelta(minutes=5)))
    db.mark_reminded(conn, tid, NOW)
    assert client.post(f"/tasks/{tid}/snooze").status_code == 204
    t = db.get_task(conn, tid)
    assert t["remind_at"] == db.iso(NOW + timedelta(minutes=15)) and t["reminded_at"] is None
    assert client.post("/tasks/999/snooze").status_code == 404


def test_snoozing_a_finished_task_does_nothing(client, conn):
    tid = db.add_task(conn, "call", ground=1, remind_at=db.iso(NOW))
    db.complete_task(conn, tid, TODAY)
    assert client.post(f"/tasks/{tid}/snooze").status_code == 204
    assert db.get_task(conn, tid)["remind_at"] == db.iso(NOW)


def test_the_notifications_done_button_completes_the_task(client, conn):
    tid = db.add_task(conn, "call", ground=1, remind_at=db.iso(NOW))
    assert client.post(f"/tasks/{tid}/complete", headers={"X-Requested-With": "fetch"}).status_code == 204
    assert db.get_task(conn, tid)["state"] == "done"


def test_a_rejected_reminder_is_given_up_but_a_server_error_is_retried(client, conn):
    import urllib.error
    from app import send_reminders
    tid = db.add_task(conn, "call", ground=1, remind_at=db.iso(NOW))

    def http(code):
        def send(t, at):
            raise urllib.error.HTTPError("http://ntfy", code, "no", {}, None)
        return send
    send_reminders(conn, NOW, http(503))
    send_reminders(conn, NOW, http(429))  # rate limited: try again
    assert db.get_task(conn, tid)["reminded_at"] is None
    send_reminders(conn, NOW, http(400))  # it would never be accepted
    assert db.get_task(conn, tid)["reminded_at"] == db.iso(NOW)


def test_a_reminder_in_the_past_is_refused_unless_unchanged(client, conn):
    past = db.iso(NOW - timedelta(hours=1))
    tid = db.add_task(conn, "call", ground=1, remind_at=past)
    db.mark_reminded(conn, tid, NOW)
    shown = datetime.fromisoformat(past).astimezone().strftime("%Y-%m-%d %H:%M")
    r = client.post(f"/tasks/{tid}", data={"title": "call", "remind_at": shown}, follow_redirects=True)
    assert "has already passed" not in r.get_data(as_text=True)
    assert db.get_task(conn, tid)["reminded_at"]  # untouched: not re-sent
    r = client.post(f"/tasks/{tid}", data={"title": "call", "remind_at": "yesterday 9am"}, follow_redirects=True)
    assert "has already passed" in r.get_data(as_text=True)
    assert db.get_task(conn, tid)["remind_at"] == past


def test_reminder_preview_reads_the_text_back(client):
    when = local(db.iso(NOW + timedelta(hours=2)))
    assert client.get("/parse/remind?q=in 2 hours").get_json() == {"reads": when.strftime("%a %b %-d, %H:%M"),
                                                                   "past": False}
    assert client.get("/parse/remind?q=yesterday").get_json() == {"reads": "Wed Oct 21, 09:00", "past": True}
    assert client.get("/parse/remind?q=gibberish").get_json() == {"reads": None, "past": False}


def test_due_preview_reads_the_text_back(client):  # today is Thu Oct 22
    assert client.get("/parse/due?q=tomorrow").get_json() == {"reads": "Fri Oct 23", "past": False}
    assert client.get("/parse/due?q=2026-10-30").get_json() == {"reads": "Fri Oct 30", "past": False}
    assert client.get("/parse/due?q=gibberish").get_json() == {"reads": None, "past": False}


def test_repeat_preview_reads_the_rule_and_the_next_date(client):  # today is Thu Oct 22
    def reads(q, due=""):
        return client.get("/parse/recur", query_string={"q": q, "due": due}).get_json()["reads"]
    assert reads("weekly") == "every week · next Thu Oct 29"  # no due date: it gets today
    assert reads("Every 2 Weeks", "oct 30") == "every 2 weeks · next Fri Nov 13"
    assert reads("every 2 weeks", "2026-10-01") == "every 2 weeks · next Thu Oct 29"  # overdue: the first one after today
    assert reads("monthly after done", "oct 30") == "every month after done · next Sun Nov 22 if done today"
    assert reads("every 2 weeks", "gibberish") == "every 2 weeks · next Thu Nov 5"
    assert reads("fortnightly") is None
    assert client.get("/parse/nonsense?q=x").status_code == 404


def test_rows_show_a_bell_for_a_reminder_still_to_come(client, conn):
    seed(conn)
    html = client.get("/h0").get_data(as_text=True)
    bell = re.search(r'<li class="task[^>]*data-id="7".*?</li>', html, re.S).group(0)  # "call mum"
    assert '<span class="remind" title="Reminder 2026-10-23' in bell and "🔔" in bell
    sent = re.search(r'<li class="task[^>]*data-id="4".*?</li>', client.get("/waiting").get_data(as_text=True),
                     re.S).group(0)  # "quote from Bob": already sent
    assert '<span class="remind"></span>' in sent


@pytest.mark.parametrize("url, data", [
    ("/capture", {}), ("/next/add", {"project_id": "h0"}), ("/h0/add", {}), ("/waiting/add", {}),
    ("/calendar", {"due_date": "2026-10-30"}),
])
def test_a_trailing_bang_time_on_an_add_line_sets_a_reminder(client, conn, url, data):
    client.post(url, data={"title": "call back !tomorrow 3pm", **data})
    t = conn.execute("SELECT * FROM tasks WHERE title = 'call back'").fetchone()
    assert local(t["remind_at"]) == datetime(2026, 10, 23, 15, 0)


def test_a_project_add_line_takes_a_bang_time(client, conn):
    pid = db.add_project(conn, "Kitchen")
    client.post(f"/projects/{pid}/tasks", data={"title": "call plumber  !in 30 min"})
    t = db.project_tasks(conn, pid)[0]
    assert t["title"] == "call plumber" and t["remind_at"] == db.iso(NOW + timedelta(minutes=30))


@pytest.mark.parametrize("title, flashed", [
    ("Yes! do it", None), ("wow !", None), ("!3pm", None),  # not a trailing " !time" after a title
    ("call back !blah", "Didn&#39;t understand reminder"), ("call back !yesterday", "has already passed"),
])
def test_a_bang_that_isnt_a_reminder_stays_in_the_title(client, conn, title, flashed):
    html = client.post("/capture", data={"title": title}, follow_redirects=True).get_data(as_text=True)
    t = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchone()
    assert t["title"] == title and t["remind_at"] is None
    assert flashed in html if flashed else "understand reminder" not in html and "has already passed" not in html


@pytest.mark.parametrize("url, data", [
    ("/capture", {}), ("/next/add", {"project_id": "h0"}), ("/h0/add", {}), ("/waiting/add", {}),
    ("/calendar", {}),
])
def test_add_lines_take_due_repeat_and_reminder_in_any_order(client, conn, url, data):
    client.post(url, data={"title": "pay rent *monthly !oct 30 9am @nov 1", **data})
    t = conn.execute("SELECT * FROM tasks WHERE title = 'pay rent'").fetchone()
    assert (t["due_date"], t["recur"], local(t["remind_at"])) == ("2026-11-01", "monthly", datetime(2026, 10, 30, 9, 0))


def test_a_captured_repeat_without_a_due_date_is_due_today(client, conn):
    client.post("/capture", data={"title": "water plants *every 3 days"})
    t = conn.execute("SELECT * FROM tasks WHERE title = 'water plants'").fetchone()
    assert (t["due_date"], t["recur"]) == ("2026-10-22", "every 3 days")


@pytest.mark.parametrize("title, flashed", [
    ("pay rent @blah", "Didn&#39;t understand date"), ("pay rent *sometimes", "Didn&#39;t understand repeat rule"),
    ("a @ b", None), ("5 * 3", None), ("mail bob@x.com", None),
])
def test_an_unreadable_due_or_repeat_stays_in_the_title(client, conn, title, flashed):
    html = client.post("/capture", data={"title": title}, follow_redirects=True).get_data(as_text=True)
    t = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchone()
    assert (t["title"], t["due_date"], t["recur"]) == (title, None, None)
    assert flashed is None or flashed in html


def test_the_add_syntax_is_shown_where_tasks_are_added(client, conn):
    hint = "@fri due · *weekly repeat · !3pm remind"
    pid = db.add_project(conn, "P")
    for url in ("/inbox", "/next", "/h0", "/waiting", "/calendar", f"/projects/{pid}"):
        assert client.get(url).get_data(as_text=True).count(hint) == 2, url  # its add line, and the capture dialog
    assert client.get("/projects").get_data(as_text=True).count(hint) == 1  # the dialog only
    assert "<kbd>@fri</kbd> <kbd>*weekly</kbd> <kbd>!3pm</kbd>" in client.get("/next").get_data(as_text=True)  # the ? overlay


def test_a_greater_than_is_no_longer_a_due_mark(client, conn):
    client.post("/capture", data={"title": "pay rent >fri"})
    t = conn.execute("SELECT * FROM tasks ORDER BY id DESC").fetchone()
    assert (t["title"], t["due_date"]) == ("pay rent >fri", None)

def test_capture_confirms_the_reminder(client, conn):
    html = client.post("/capture", data={"title": "call back !tomorrow 3pm"}, follow_redirects=True).get_data(as_text=True)
    assert "Added to inbox: call back · reminder Fri Oct 23, 15:00" in html
    html = client.post("/capture", data={"title": "pay rent @nov 1 *monthly"}, follow_redirects=True).get_data(as_text=True)
    assert "Added to inbox: pay rent · due 2026-11-01 · ↻ monthly" in html


def test_the_task_page_title_keeps_its_bang(client, conn):
    tid = db.add_task(conn, "call", ground=1)
    client.post(f"/autosave/tasks/{tid}", data={"title": "call !3pm", "base_title": "call"})
    t = db.get_task(conn, tid)
    assert t["title"] == "call !3pm" and t["remind_at"] is None


def test_a_repeats_next_instance_has_no_reminder(conn):
    remind = db.iso(datetime(2026, 10, 28, 18, 0).astimezone())
    tid = db.add_task(conn, "bins", ground=1, recur="weekly", due_date="2026-10-29", remind_at=remind)
    new = db.get_task(conn, db.complete_task(conn, tid, TODAY))
    assert new["due_date"] == "2026-11-05" and new["remind_at"] is None


def test_new_items_go_to_the_top(client, conn):
    """Whatever arrives in a list (added, filed, moved there) shows first."""
    ids = seed(conn)
    client.post("/capture", data={"title": "fresh inbox"})
    assert db.inbox_tasks(conn)[0]["title"] == "fresh inbox"
    client.post("/waiting/add", data={"title": "fresh wait", "project_id": ids["active"]})
    assert db.tasks_with_step(conn, "waiting")[0]["title"] == "fresh wait"
    client.post("/next/add", data={"title": "fresh next", "project_id": ids["stalled"]})
    undated = [t["title"] for t in db.next_list(conn) if not t["due_date"]]
    assert undated[0] == "fresh next"
    client.post("/h0/add", data={"title": "fresh h0"})
    assert [t["title"] for t in db.h0_actions(conn) if not t["due_date"]][0] == "fresh h0"
    steps = lambda: [t["title"] for t in db.project_tasks(conn, ids["active"])]
    db.reorder_project(conn, ids["active"], [t["id"] for t in db.project_tasks(conn, ids["active"])])
    db.add_task(conn, "fresh step", step="later", project_id=ids["active"])
    assert steps()[1] == "fresh step"  # right under the pinned next action
    loose = db.add_task(conn, "loose")
    db.update_task(conn, loose, project_id=ids["active"])  # filed from the inbox
    assert steps()[1] == "loose"
    a = db.add_project(conn, "a")
    db.reorder_projects(conn, "active", [p["id"] for p in db.projects(conn)])
    z = db.add_project(conn, "zzz")
    assert db.projects(conn)[0]["id"] == z
    db.update_project(conn, a, status="someday")
    assert db.projects(conn, "someday")[0]["id"] == a


# --- abandoned projects, inbox → reference -----------------------------------

def test_abandoning_a_project_abandons_its_open_steps_and_lists_it_on_done(client, conn):
    pid = db.add_project(conn, "Boat", status="someday")
    a = db.add_task(conn, "a", step="next", project_id=pid)
    b = db.add_task(conn, "b", step="later", project_id=pid)
    done = db.add_task(conn, "finished", step="later", project_id=pid)
    db.complete_task(conn, done, TODAY)
    earlier = db.add_task(conn, "dropped before", step="later", project_id=pid)
    db.abandon_task(conn, earlier)
    client.post(f"/tasks/{a}/start")
    r = client.post(f"/projects/{pid}", data={"status": "abandoned"}, headers={"Referer": f"/projects/{pid}"})
    assert r.headers["Location"] == f"/projects/{pid}"  # no jump
    p = db.get_project(conn, pid)
    assert p["status"] == "abandoned" and p["completed_at"] and p["position"] is None
    assert [db.get_task(conn, i)["state"] for i in (a, b, done)] == ["abandoned", "abandoned", "done"]
    assert db.timer_running(conn) is None and len(db.time_entries(conn)) == 1  # stopped, time kept
    # It leaves every list and picker, and Done lists it (not its steps one by one), revivable.
    assert ">Boat<" not in client.get("/someday").get_data(as_text=True)
    assert not [p for _, ps, _ in db.project_groups(conn) for p in ps if p["id"] == pid]
    page = client.get("/done").get_data(as_text=True).split("<h2>Abandoned")[1]
    assert "Boat" in page and f"/projects/{pid}/revive" in page and ">a<" not in page
    project_page = client.get(f"/projects/{pid}?done=1").get_data(as_text=True)
    assert "<option selected>abandoned</option>" in project_page
    assert f'href="/tasks/{a}"' in project_page and f"/tasks/{a}/revive" in project_page  # its steps, each revivable
    # Revive (Done's button, or picking someday): back as someday with the steps it took down; one abandoned before
    # stays abandoned.
    client.post(f"/projects/{pid}", data={"status": "someday"})
    p = db.get_project(conn, pid)
    assert p["status"] == "someday" and p["completed_at"] is None
    assert [(db.get_task(conn, i)["state"], db.get_task(conn, i)["step"]) for i in (a, b, done, earlier)] == [
        ("open", "later"), ("open", "later"), ("done", "later"), ("abandoned", "later")]


def test_abandon_and_revive_by_status(client, conn):
    pid = db.add_project(conn, "P")
    client.post(f"/projects/{pid}/revive")  # not abandoned: nothing happens
    assert db.get_project(conn, pid)["status"] == "active"
    client.post(f"/projects/{pid}", data={"status": "abandoned"})
    client.post(f"/projects/{pid}", data={"status": "abandoned"})  # twice: still the first time
    first = db.get_project(conn, pid)["completed_at"]
    assert db.get_project(conn, pid)["status"] == "abandoned" and first
    # An untouched save (no status) leaves it abandoned; picking another status revives it into that one.
    client.post(f"/projects/{pid}", data={"title": "P"})
    assert db.get_project(conn, pid)["status"] == "abandoned"
    t = db.add_task(conn, "t", step="later", project_id=db.add_project(conn, "Q"))
    q = db.get_task(conn, t)["project_id"]
    db.add_task(conn, "u", step="next", project_id=q)
    client.post(f"/projects/{q}", data={"status": "abandoned"})
    client.post(f"/projects/{q}", data={"status": "active"})
    assert db.get_project(conn, q)["status"] == "active" and db.get_task(conn, t)["state"] == "open"
    assert client.post("/projects/999/revive").status_code == 404

def test_reviving_one_step_of_an_abandoned_project_brings_the_project_back(client, conn):
    pid = db.add_project(conn, "P")
    t = db.add_task(conn, "t", step="next", project_id=pid)
    db.abandon_project(conn, pid)
    client.post(f"/tasks/{t}/revive")
    assert db.get_project(conn, pid)["status"] == "someday" and db.get_task(conn, t)["state"] == "open"


def test_task_page_of_an_abandoned_project_keeps_its_project(client, conn):
    pid = db.add_project(conn, "Gone")
    t = db.add_task(conn, "t", step="next", project_id=pid)
    db.abandon_project(conn, pid)
    html = client.get(f"/tasks/{t}").get_data(as_text=True)
    assert f'<option value="{pid}" selected>Gone</option>' in html
    client.post(f"/tasks/{t}", data={"title": "t", "project_id": str(pid)})
    assert db.get_task(conn, t)["project_id"] == pid


def test_inbox_item_dropped_on_reference_becomes_one(client, conn):
    a = db.add_task(conn, "wifi password", notes="hunter2")
    timed = db.add_task(conn, "timed")
    db.timer_start(conn, timed, NOW - timedelta(hours=1))
    db.timer_stop(conn, NOW)
    assert 'data-project="reference"' in client.get("/inbox").get_data(as_text=True)
    assert client.post("/tasks/move", data={"id": a, "project_id": "reference"}, headers=FETCH).status_code == 204
    assert [(r["title"], r["notes"]) for r in db.references(conn)] == [("wifi password", "hunter2")]
    assert db.get_task(conn, a) is None  # it isn't a task any more
    # One with time on it is abandoned instead, so its time stays.
    assert client.post("/tasks/move", data={"id": timed, "project_id": "reference"}, headers=FETCH).status_code == 204
    assert db.get_task(conn, timed)["state"] == "abandoned" and len(db.time_entries(conn)) == 1
    assert len(db.references(conn)) == 2
    # Not a finished one.
    filed = db.add_task(conn, "h", ground=1)
    db.complete_task(conn, filed, TODAY)
    assert client.post("/tasks/move", data={"id": filed, "project_id": "reference"}, headers=FETCH).status_code == 400
    assert len(db.references(conn)) == 2


def test_done_inbox_item_becomes_h0(conn):
    """A finished task without a project is filed under H0, so the timer has no '(no project)' row."""
    t = db.add_task(conn, "loose end")
    new = db.add_task(conn, "loose repeat", recur="weekly")
    db.complete_task(conn, t, date(2026, 10, 22))
    instance = db.complete_task(conn, new, date(2026, 10, 22))
    assert db.get_task(conn, t)["ground"] == 1
    assert db.get_task(conn, new)["ground"] == 1
    assert db.get_task(conn, instance)["inbox"]  # the next instance still waits in the inbox
    p = db.add_project(conn, "Kitchen")
    s = db.add_task(conn, "step", project_id=p)
    db.complete_task(conn, s, date(2026, 10, 22))
    assert db.get_task(conn, s)["ground"] == 0


# --- areas (H2): notes on Horizons, apart from projects --------------------

def test_projects_have_no_area(client, conn):
    with pytest.raises(ValueError):
        db.add_project(conn, "x", area_id=1)
    client.post("/projects", data={"title": "Paint fence"})
    pid = db.projects(conn)[0]["id"]
    assert db.get_project(conn, pid)["title"] == "Paint fence"
    with pytest.raises(ValueError):
        db.update_project(conn, pid, area_id=None)
    # A new project from a task's picker needs only its name.
    tid = db.add_task(conn, "ring Bob")
    client.post(f"/tasks/{tid}", data={"project_id": "__new__", "new_project": "Garden"})
    assert db.get_task(conn, tid)["project_title"] == "Garden"
    seed(conn)
    for url in ("/projects", "/someday", "/waiting", f"/projects/{pid}", "/review?step=creative", "/inbox", f"/tasks/{tid}", "/timer"):
        page = client.get(url).get_data(as_text=True)
        assert 'name="area_id"' not in page and "data-area" not in page and "area-head" not in page, url


def test_projects_waiting_and_someday_are_flat_lists(client, conn):
    seed(conn)
    db.add_project(conn, "Desk")
    db.add_project(conn, "Visa", status="waiting")
    for url, status in (("/projects", "active"), ("/waiting", "waiting"), ("/someday?tickled=1", "someday")):
        page = client.get(url).get_data(as_text=True)
        assert page.count(f'data-reorder="/projects/order/{status}"') == 1, url


def test_an_area_is_a_note_on_horizons(client, conn):
    ids = seed(conn)
    page = client.get(f"/horizons/{ids['area']}").get_data(as_text=True)
    assert "Clean and calm" in page and "Kitchen" not in page and "Archive" not in page and 'aria-label="Delete"' in page
    card = client.get("/horizons?level=area").get_data(as_text=True)
    assert ">Home<" in card and "project" not in card.split('class="deck"')[1].split("</ol>")[0].lower().replace("projects", "")
    assert client.post(f"/horizons/{ids['area']}/delete").headers["Location"] == "/horizons?level=area"
    assert not db.get_horizon(conn, ids["area"])


def test_task_rows_show_the_project_not_the_area(client, conn):
    ids = seed(conn)
    page = client.get("/next").get_data(as_text=True)
    assert f'<a class="proj" href="/projects/{ids["active"]}">Kitchen</a></span>' in page

def test_project_page_lists_its_abandoned_steps_below_completed(client, conn):
    pid = db.add_project(conn, "Garden")
    done = db.add_task(conn, "weeded", step="later", project_id=pid)
    db.complete_task(conn, done, TODAY)
    dropped = db.add_task(conn, "pond", step="later", project_id=pid)
    db.abandon_task(conn, dropped)
    page = client.get(f"/projects/{pid}?done=1").get_data(as_text=True)
    completed, abandoned = page.split("<h2>Completed")[1].split("<h2>Abandoned")
    assert ">weeded<" in completed and ">pond<" not in completed
    assert ">pond<" in abandoned and f"/tasks/{dropped}/revive" in abandoned
    # Hidden with the completed ones; absent when there are none.
    assert "<h2>Abandoned" not in client.get(f"/projects/{pid}").get_data(as_text=True)
    other = db.add_project(conn, "Empty")
    assert "<h2>Abandoned" not in client.get(f"/projects/{other}?done=1").get_data(as_text=True)


# --- review fixes (2026-10-30) ---------------------------------------------

def test_cross_site_posts_are_refused(client, conn):
    tid = db.add_task(conn, "t", ground=1)
    assert client.post(f"/tasks/{tid}/delete", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(f"/tasks/{tid}/delete", headers={"Sec-Fetch-Site": "same-site"}).status_code == 403
    assert db.get_task(conn, tid)["state"] == "open"
    # The app's own pages, and clients that send neither header (ntfy's buttons), still work.
    assert client.post(f"/tasks/{tid}/snooze", headers={"Sec-Fetch-Site": "same-origin"}).status_code == 204
    assert client.post(f"/tasks/{tid}/complete", headers={"X-Requested-With": "fetch"}).status_code == 204
    assert client.get("/next", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 200  # links in are fine


def test_new_project_option_without_a_name_keeps_the_project(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "t", project_id=pid)
    client.post(f"/tasks/{tid}", data={"project_id": "__new__", "new_project": "", "step": "waiting"})
    t = db.get_task(conn, tid)
    assert (t["project_id"], t["step"]) == (pid, "waiting")


def test_completing_a_repeat_twice_at_once_spawns_one_instance(client, conn, monkeypatch):
    tid = db.add_task(conn, "water", ground=1, recur="weekly", due_date="2026-10-01")
    stale = db.get_task(conn, tid)  # what a second, concurrent request read before the first wrote
    db.complete_task(conn, tid, TODAY)
    monkeypatch.setattr(db, "get_task", lambda c, id: stale)
    assert db.complete_task(conn, tid, TODAY) is None
    monkeypatch.undo()
    assert conn.execute("SELECT count(*) FROM tasks WHERE title = 'water'").fetchone()[0] == 2


def test_a_failed_complete_changes_nothing(client, conn, monkeypatch):
    tid = db.add_task(conn, "water", ground=1, recur="weekly", due_date="2026-10-01")

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "add_task", boom)
    with pytest.raises(RuntimeError):
        db.complete_task(conn, tid, TODAY)
    monkeypatch.undo()
    other = db.connect(app.config["DB_PATH"])
    assert db.get_task(other, tid)["state"] == "open"


def test_record_page_selects_leave_title_and_notes_to_autosave(client, conn):
    """A select on a task or project page submits its whole form; a stale tab's title and notes mustn't overwrite
    edits saved elsewhere. Autosave (with its conflict check) owns them."""
    pid = db.add_project(conn, "P", notes="new elsewhere")
    tid = db.add_task(conn, "t", project_id=pid, notes="new elsewhere")
    client.post(f"/tasks/{tid}", data={"title": "stale", "notes": "stale", "step": "waiting"})
    client.post(f"/projects/{pid}", data={"title": "stale", "notes": "stale", "status": "someday"})
    t, p = db.get_task(conn, tid), db.get_project(conn, pid)
    assert (t["title"], t["notes"], t["step"]) == ("t", "new elsewhere", "waiting")
    assert (p["title"], p["notes"], p["status"]) == ("P", "new elsewhere", "someday")


def test_connect_creates_the_schema_once_per_file(tmp_path):
    path = str(tmp_path / "x.db")
    db.connect(path).close()
    c = db.connect(path)
    assert c.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    c.close()
    os.remove(path)  # a file replaced (a restore, a fresh test DB) gets its schema again
    assert db.horizons(db.connect(path))["purpose"]


def test_nav_partial_has_the_counts(client, conn):
    seed(conn)
    html = client.get("/nav").get_data(as_text=True)
    assert html.lstrip().startswith("<nav") and "badge" in html and "<main" not in html


def test_projects_have_no_updated_at(client, conn):
    assert "updated_at" not in db.SCHEMA


def test_every_project_step_and_listed_project_has_a_position(client, conn):
    """Lists sort by position alone, so nothing may leave one without (abandoned projects aren't listed)."""
    seed(conn)
    assert not conn.execute("SELECT 1 FROM tasks WHERE project_id IS NOT NULL AND position IS NULL").fetchone()
    assert not conn.execute("SELECT 1 FROM projects WHERE status != 'abandoned' AND position IS NULL").fetchone()


def test_the_task_pages_project_link_stands_out(client, conn):
    pid = db.add_project(conn, "P")
    tid = db.add_task(conn, "step", project_id=pid)
    assert f'class="open-project" href="/projects/{pid}"' in client.get(f"/tasks/{tid}").get_data(as_text=True)


def test_h0_tasks_need_no_date(client, conn):
    """H0 tasks are dated only when given one: made there, moved there, revived, or their date cleared."""
    client.post("/h0/add", data={"title": "h0 added"})
    client.post("/capture", data={"title": "captured", "project_id": "h0"})
    client.post("/next/add", data={"title": "next added", "project_id": "h0"})
    client.post("/waiting/add", data={"title": "parcel", "project_id": "h0", "due_date": ""})
    for title in ("h0 added", "captured", "next added", "parcel"):
        assert conn.execute("SELECT due_date FROM tasks WHERE title = ?", (title,)).fetchone()[0] is None, title
    inbox = db.add_task(conn, "filed")
    client.post("/tasks/move", data={"id": inbox, "project_id": "h0"}, headers={"X-Requested-With": "fetch"})
    assert db.get_task(conn, inbox)["due_date"] is None
    picked = db.add_task(conn, "picked")
    client.post(f"/tasks/{picked}", data={"project_id": "h0"})
    assert db.get_task(conn, picked)["due_date"] is None
    dated = db.add_task(conn, "dated", ground=1, due_date="2026-11-05", today=TODAY)
    client.post(f"/tasks/{dated}", data={"due_date": ""}, headers={"X-Requested-With": "fetch"})
    assert db.get_task(conn, dated)["due_date"] is None
    db.abandon_task(conn, dated)
    db.revive_task(conn, dated)
    assert db.get_task(conn, dated)["due_date"] is None
    assert "No date" in client.get("/h0").get_data(as_text=True)


def test_several_tasks_move_together(client, conn):
    """Shift-selected rows dragged at once: one request, one transaction, in the page's order."""
    a, b, c = db.add_task(conn, "a"), db.add_task(conn, "b"), db.add_task(conn, "c")
    pid = db.add_project(conn, "P")
    db.add_task(conn, "p next", project_id=pid, step="next")
    assert client.post("/tasks/move", data={"id": [a, b], "project_id": str(pid)}, headers=FETCH).status_code == 204
    assert [t["title"] for t in db.project_tasks(conn, pid)] == ["p next", "a", "b"]
    assert db.get_task(conn, c)["inbox"]
    # A new project is made once for all of them.
    d = db.add_task(conn, "d")
    assert client.post("/tasks/move", data={"id": [c, d], "new_project": "Fresh"}, headers=FETCH).status_code == 204
    assert db.get_task(conn, c)["project_id"] == db.get_task(conn, d)["project_id"]
    assert sum(p["title"] == "Fresh" for _, ps, _ in db.project_groups(conn) for p in ps) == 1
    # One that's no longer open: nothing moves.
    db.complete_task(conn, c, TODAY)
    assert client.post("/tasks/move", data={"id": [d, c], "project_id": "h0"}, headers=FETCH).status_code == 400
    assert not db.get_task(conn, d)["ground"]
    # To Reference, each becomes an item.
    e, f = db.add_task(conn, "e"), db.add_task(conn, "f")
    assert client.post("/tasks/move", data={"id": [e, f], "project_id": "reference"}, headers=FETCH).status_code == 204
    assert {"e", "f"} <= {r["title"] for r in db.references(conn)} and not db.get_task(conn, e)


def test_someday_projects_keep_when_they_were_last_reviewed(client, conn):
    old = db.add_project(conn, "old", status="someday")
    fresh = db.add_project(conn, "fresh", status="someday")
    never = db.add_project(conn, "never", status="someday")
    db.mark_reviewed(conn, old, NOW - timedelta(days=40))
    db.mark_reviewed(conn, fresh, NOW - timedelta(days=3))
    html = client.get("/review?step=someday").get_data(as_text=True)
    titles = re.findall(r'href="/projects/\d+">([^<]*)</a>', card(html, "someday"))
    assert titles == ["never", "old", "fresh"]  # never reviewed, then the longest ago
    assert all(w in card(html, "someday") for w in ("never", "5w ago", "3d ago"))
    r = client.post(f"/projects/{never}/reviewed", headers={"Referer": "/review?step=someday"})
    assert r.status_code == 302 and r.location.endswith("/review?step=someday")
    assert db.get_project(conn, never)["reviewed_at"] == db.iso(NOW)
    c = card(client.get("/review?step=someday").get_data(as_text=True), "someday")
    assert re.findall(r'href="/projects/\d+">([^<]*)</a>', c) == ["old", "fresh", "never"]
    someday = client.get("/someday").get_data(as_text=True)
    assert 'class="reviewed" title="Last reviewed">5w ago<' in someday and ">today<" in someday
    assert 'class="reviewed"' not in client.get("/projects").get_data(as_text=True)  # someday only
    assert client.post("/projects/999/reviewed").status_code == 404


# --- someday / maybe, the tickler, the full weekly review ------------------

def card(html, step):
    """A review step's card."""
    return re.search(rf'<li id="{step}".*?</li>\s*(?=<li id=|</ol>)', html, re.S).group(0)


def test_a_someday_projects_tickler_date(client, conn):
    p = db.add_project(conn, "Boat", status="someday")
    html = client.get(f"/projects/{p}").get_data(as_text=True)
    assert 'name="tickle_on"' in html
    client.post(f"/projects/{p}", data={"status": "someday", "tickle_on": "nov 15"})
    assert db.get_project(conn, p)["tickle_on"] == "2026-11-15"
    assert "2026-11-15" in client.get("/someday?tickled=1").get_data(as_text=True)
    client.post(f"/projects/{p}", data={"status": "someday", "tickle_on": ""})
    assert db.get_project(conn, p)["tickle_on"] is None
    client.post(f"/projects/{p}", data={"status": "active", "tickle_on": "nov 15"})  # active: no tickler
    assert db.get_project(conn, p)["tickle_on"] is None
    assert 'name="tickle_on"' not in client.get(f"/projects/{p}").get_data(as_text=True)


def test_review_follows_allens_order():
    assert REVIEW_STEPS == ("collect", "inbox", "sweep", "overdue", "nexts", "past", "ahead", "waiting", "projects",
                            "checklists", "someday", "creative", "horizons")


def test_review_projects_step(client, conn):
    seen = db.add_project(conn, "seen")
    db.add_task(conn, "paint", step="next", project_id=seen)
    db.mark_reviewed(conn, seen, NOW - timedelta(days=1))
    stuck = db.add_project(conn, "stuck")
    fresh = db.add_project(conn, "fresh")
    db.add_task(conn, "call", step="next", project_id=fresh)
    db.add_project(conn, "parked", status="waiting")
    c = card(client.get("/review?step=projects").get_data(as_text=True), "projects")
    assert re.findall(r'href="/projects/\d+">(?:⚠ )?([^<]*)</a>', c) == ["stuck", "fresh", "parked", "seen"]
    assert "⚠ stuck" in c and "paint" in c and "call" in c
    assert '<span class="count">3</span>' in c  # not looked at this week
    assert f'action="/projects/{stuck}/reviewed"' in c


def test_review_look_back_checklists_and_new_ideas(client, conn):
    t = db.add_task(conn, "sent the forms", ground=1)
    db.complete_task(conn, t, TODAY)
    assert "sent the forms" in card(client.get("/review?step=past").get_data(as_text=True), "past")
    pack = db.add_reference(conn, "Packing", notes="- [ ] passport\n- [x] charger")
    db.add_reference(conn, "Plumber", notes="call Sam - [ ] not a line")
    assert [r["title"] for r in db.checklists(conn)] == ["Packing"]
    c = card(client.get("/review?step=checklists").get_data(as_text=True), "checklists")
    assert f'href="/reference/{pack}"' in c and "Plumber" not in c
    html = client.get("/review?step=creative").get_data(as_text=True)  # the add line, above the deck
    assert html.index('action="/someday/add"') < html.index('<ol class="deck') and 'name="kind"' in html
    assert 'action="/someday/add"' not in client.get("/review?step=someday").get_data(as_text=True)
    r = client.post("/someday/add", data={"title": "learn Italian"}, headers={"Referer": "/review?step=creative"})
    assert r.location.endswith("/review?step=creative") and db.someday_tasks(conn)[0]["title"] == "learn Italian"


def test_look_back_lists_a_repeat_once(client, conn):
    t = db.add_task(conn, "Vitamins", recur="daily", due_date="2026-10-20", ground=1)
    for _ in range(3):
        t = db.complete_task(conn, t, TODAY)
    c = card(client.get("/review?step=past").get_data(as_text=True), "past")
    assert c.count(">Vitamins<") == 1 and "×3" in c


def test_a_checklist_line_may_end_at_its_box(conn):
    db.add_reference(conn, "Bare", notes="- [ ]\n- [x]")
    assert [r["title"] for r in db.checklists(conn)] == ["Bare"]


def test_projects_know_when_they_are_stalled(conn):
    stuck, going = db.add_project(conn, "stuck"), db.add_project(conn, "going")
    db.add_task(conn, "paint", step="next", project_id=going)
    parked = db.add_project(conn, "parked", status="waiting")
    rows = {p["title"]: p for p in db.projects(conn) + db.projects(conn, "waiting")}
    assert (rows["stuck"]["stalled"], rows["going"]["stalled"], rows["parked"]["stalled"]) == (1, 0, 0)
    assert rows["going"]["next_title"] == "paint"
    assert [p["id"] for p in db.stalled_projects(conn)] == [stuck]
    assert db.get_project(conn, parked)["stalled"] == 0


def test_someday_hides_what_is_tickled_to_a_later_date(client, conn):
    seed(conn)
    later = db.add_project(conn, "Sail round the world", status="someday")
    db.set_project_tickle(conn, later, "2026-11-15")
    html = client.get("/someday").get_data(as_text=True).split('class="drop-panel"')[0]
    assert "write a book" not in html and "Sail round the world" not in html  # out of sight until their date
    assert ">sail<" in html  # its date has come: shown (and in the Inbox)
    assert ">learn Italian<" in html
    assert re.search(r'href="/someday\?tickled=1" title="3 [^"]*">Show tickled<', html)
    shown = client.get("/someday?tickled=1").get_data(as_text=True)
    assert "write a book" in shown and "until 2026-11-15" in shown and re.search(r'href="/someday"[^>]*>Hide tickled<', shown)


def test_h0_tasks_can_be_someday(client, conn):
    t = db.add_task(conn, "learn Italian", ground=1, step="someday", today=TODAY)
    assert (db.get_task(conn, t)["step"], db.get_task(conn, t)["due_date"]) == ("someday", None)  # not dated: no tickler
    # Not on H0 or in its count; on Someday and in its count.
    assert t not in [x["id"] for x in db.h0_actions(conn)] and db.counts(conn, TODAY)["someday"] == 1
    html = client.get(f"/tasks/{t}").get_data(as_text=True)
    assert "<option selected>someday</option>" in html and ">Tickler <" in html.replace("\n", "")
    # Becoming someday drops a date already come (it would be back in the Inbox at once), keeps one to come.
    n = db.add_task(conn, "call", ground=1, due_date=TODAY.isoformat(), today=TODAY)
    client.post(f"/tasks/{n}", data={"step": "someday"})
    assert (db.get_task(conn, n)["step"], db.get_task(conn, n)["due_date"]) == ("someday", None)
    later = db.add_task(conn, "paint", ground=1, due_date="2026-12-01", today=TODAY)
    db.update_task(conn, later, step="someday", today=TODAY)
    assert db.get_task(conn, later)["due_date"] == "2026-12-01"
    client.post(f"/tasks/{n}", data={"step": "next"})  # committed to: an H0 next action, still undated
    assert (db.get_task(conn, n)["step"], db.get_task(conn, n)["due_date"]) == ("next", None)
    # Someday is H0's only: a project step can't be, and one filed from someday is a later step.
    p = db.add_project(conn, "Boat")
    assert db.get_task(conn, db.add_task(conn, "x", project_id=p, step="someday"))["step"] == "later"
    client.post(f"/tasks/{t}", data={"project_id": str(p), "step": "someday"})
    assert (db.get_task(conn, t)["project_id"], db.get_task(conn, t)["step"], db.get_task(conn, t)["ground"]) == (p, "later", 0)
    with pytest.raises(sqlite3.IntegrityError):  # the schema says so too
        conn.execute("UPDATE tasks SET step = 'someday', ground = 0 WHERE id = ?", (t,))


def test_someday_add_line_makes_a_task_or_a_project(client, conn):
    r = client.post("/someday/add", data={"title": "learn Italian @dec 1"}, headers={"Referer": "/someday"})
    assert r.headers["Location"].endswith("/someday")
    t = db.someday_tasks(conn)[0]
    assert (t["title"], t["ground"], t["step"], t["tickle_on"]) == ("learn Italian", 1, "someday", "2026-12-01")
    client.post("/someday/add", data={"title": "boat", "kind": "project"})
    assert [p["title"] for p in db.projects(conn, "someday")] == ["boat"]
    assert len(db.someday_tasks(conn)) == 1
    html = client.get("/someday?tickled=1").get_data(as_text=True)
    assert html.index("learn Italian") < html.index(">boat<")
    assert f'href="/tasks/{t["id"]}"' in html and 'class="drop-panel"' in html  # drag one to commit to it


@pytest.mark.parametrize("step", ["someday", "waiting"])
def test_someday_and_waiting_task_pages_disable_repeats_and_keep_the_rule(client, conn, step):
    t = db.add_task(conn, "idea", ground=1, step=step, recur="weekly", due_date="2026-12-01")
    recur_box = lambda: re.search(r'<input name="recur"[^>]*>', client.get(f"/tasks/{t}").get_data(as_text=True))[0]
    assert 'value="weekly"' in recur_box() and " disabled" in recur_box()
    client.post(f"/tasks/{t}", data={"due_date": "2026-12-01", "step": step, "project_id": "h0"})
    assert db.get_task(conn, t)["recur"] == "weekly"  # a disabled box isn't posted: the rule stays
    client.post(f"/tasks/{t}", data={"due_date": "2026-12-01", "step": "next", "project_id": "h0"})
    assert " disabled" not in recur_box()


def test_tasks_move_to_and_from_someday(client, conn):
    p = db.add_project(conn, "Boat")
    a = db.add_task(conn, "a", notes="x", recur="weekly", due_date="2026-12-01")
    b = db.add_task(conn, "b", project_id=p, step="next")
    html = client.get("/inbox").get_data(as_text=True)
    assert html.count('data-project="someday"') == 1  # one target, beside H0
    assert client.post("/tasks/move", data={"id": [a, b], "project_id": "someday"}, headers=FETCH).status_code == 204
    for id in (a, b):  # kept whole: notes, dates and repeats come along
        t = db.get_task(conn, id)
        assert (t["ground"], t["project_id"], t["step"], t["state"]) == (1, None, "someday", "open")
    assert (db.get_task(conn, a)["notes"], db.get_task(conn, a)["due_date"]) == ("x", "2026-12-01")
    assert 'data-project="someday"' not in client.get("/someday?tickled=1").get_data(as_text=True)  # already there
    client.post("/tasks/move", data={"id": a, "project_id": "h0"}, headers=FETCH)  # committed to, at ground level
    assert db.get_task(conn, a)["step"] == "next"
    client.post("/tasks/move", data={"id": b, "project_id": str(p)}, headers=FETCH)  # or as a project's step
    assert (db.get_task(conn, b)["step"], db.get_task(conn, b)["ground"]) == ("later", 0)


def test_tickled_tasks_and_projects_come_back_to_the_inbox(client, conn):
    due = db.add_task(conn, "learn Italian", ground=1, step="someday", due_date="2026-10-22")
    later = db.add_task(conn, "read Dune", ground=1, step="someday", due_date="2026-11-01")
    plain = db.add_task(conn, "sail", ground=1, step="someday")
    p = db.add_project(conn, "Boat", status="someday")
    db.set_project_tickle(conn, p, "2026-10-20")
    assert [(t["kind"], t["title"]) for t in db.tickled(conn, TODAY)] == [("project", "Boat"), ("task", "learn Italian")]
    assert db.counts(conn, TODAY)["inbox"] == 2
    html = client.get("/inbox").get_data(as_text=True)
    assert f'href="/tasks/{due}"' in html and f'href="/projects/{p}"' in html and "H0 · someday" in html
    assert f'href="/tasks/{later}"' not in html and f'href="/tasks/{plain}"' not in html
    assert "empty clear" not in html  # not empty: something is back
    # Leaving someday clears a project's tickler date; a task leaves the list by its date or its step.
    client.post(f"/projects/{p}", data={"status": "active"})
    assert db.get_project(conn, p)["tickle_on"] is None and db.counts(conn, TODAY)["inbox"] == 1
    client.post(f"/tasks/{due}", data={"step": "next"})
    assert db.tickled(conn, TODAY) == []


def test_review_someday_step_lists_tasks_and_projects_and_hides_tickled_ones(client, conn):
    old = db.add_project(conn, "old proj", status="someday")
    db.mark_reviewed(conn, old, NOW - timedelta(days=40))
    never = db.add_task(conn, "never task", ground=1, step="someday")
    db.add_task(conn, "hidden task", ground=1, step="someday", due_date="2026-12-01")
    back = db.add_task(conn, "back task", ground=1, step="someday", due_date="2026-10-22")
    db.mark_reviewed(conn, back, NOW - timedelta(days=3), table="task")
    c = card(client.get("/review?step=someday").get_data(as_text=True), "someday")
    assert re.findall(r'href="/(?:tasks|projects)/\d+">([^<]*)</a>', c) == ["never task", "old proj", "back task"]
    assert "hidden task" not in c and '<span class="count">2</span>' in c
    r = client.post(f"/tasks/{never}/reviewed", headers={"Referer": "/review?step=someday"})
    assert r.location.endswith("/review?step=someday")
    assert db.someday_tasks(conn)[-1]["reviewed_at"] == db.iso(NOW)
    assert client.post("/tasks/999/reviewed").status_code == 404


def test_someday_tasks_are_searchable(client, conn):
    t = db.add_task(conn, "learn Italian", notes="grammar", ground=1, step="someday")
    html = client.get("/search?q=grammar").get_data(as_text=True)
    assert "<h2>Someday / maybe</h2>" in html and f"/tasks/{t}" in html
    assert any(s["url"] == f"/tasks/{t}" and s["kind"] == "someday" for s in client.get("/search/suggest?q=italian").json)

