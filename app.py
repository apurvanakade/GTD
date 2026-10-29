import os
import re
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlsplit

from flask import Flask, abort, flash, g, make_response, redirect, render_template, request, url_for

import dates
import db
import recur

HORIZON_LEVELS = [
    ("purpose", "H5 · Purpose & principles", "Why do I do what I do? What principles won't I compromise?"),
    ("vision", "H4 · Vision (3–5 years)", "What does wild success look like at work, at home, in health?"),
    ("goal", "H3 · Goals (1–2 years)", "Goal"),
    ("area", "H2 · Areas of focus", "Area"),
]

app = Flask(__name__)
app.secret_key = os.environ.get("GTD_SECRET", os.urandom(16))
NTFY_URL = os.environ.get("NTFY_URL")  # the ntfy topic reminders are POSTed to; unset (dev, tests): none are sent
GTD_URL = (os.environ.get("GTD_URL") or "").rstrip("/")  # the app's own address, so a reminder opens its task


def conn():
    if "db" not in g:
        g.db = db.connect(app.config.get("DB_PATH"))
    return g.db


@app.teardown_appcontext
def close_db(_exc):
    c = g.pop("db", None)
    if c is not None:
        c.close()


def today():
    return app.config.get("TODAY") or date.today()


def now():
    return app.config.get("NOW") or datetime.now(timezone.utc)


@app.context_processor
def template_globals():
    return {"counts": db.counts(conn(), today()), "today": today().isoformat(),
            "timer": db.timer_running(conn())}


NEW = "__new__"  # project_select's "＋ New…" option (_task.html)


def form_value(name):
    """Stripped form value, or None when blank."""
    return (request.form.get(name) or "").strip() or None


def task_fields_from_form():
    """Editable task fields present in the submitted form."""
    fields = {}
    for name in ("title", "notes", "due_date", "recur"):
        if name in request.form:
            fields[name] = form_value(name)
    if "notes" in fields:
        fields["notes"] = fields["notes"] or ""
    if "project_id" in request.form:
        # The picker offers "h0": ground level, a deliberate choice of no project.
        raw = request.form["project_id"]
        fields["ground"] = int(raw == "h0")
        fields["project_id"] = int(raw) if raw.isdigit() else None
    new_project = form_value("new_project")
    if new_project:  # typed into the "or new project…" box; overrides the dropdown
        fields["project_id"] = db.add_project(conn(), new_project)
        fields["ground"] = 0
    if request.form.get("step") in ("next", "waiting", "later"):  # a project's step; H0 and inbox tasks drop it
        fields["step"] = request.form["step"]
    if fields.get("due_date"):
        due = dates.parse(fields["due_date"], today())
        if due:
            fields["due_date"] = due.isoformat()
        else:
            flash(f"Didn't understand date “{fields['due_date']}” – try “tomorrow”, “saturday” or “oct 30”.")
            fields.pop("due_date")
    if fields.get("recur") and not recur.parse(fields["recur"]):
        flash(f"Didn't understand repeat rule “{fields['recur']}” – try “weekly” or “every 6 months”.")
        fields.pop("recur")
    if fields.get("recur") and not fields.get("due_date"):
        fields["due_date"] = today().isoformat()
    if "remind_at" in request.form:
        raw = form_value("remind_at")
        when = raw and dates.parse_when(raw, datetime.combine(today(), now().astimezone().time()))
        if raw and not when:
            flash(f"Didn't understand reminder “{raw}” – try “tomorrow 9am”, “fri 14:00” or “in 2 hours”.")
        else:
            fields["remind_at"] = when and db.iso(when.astimezone())  # the form's time is local
    return fields


def orphaned(project_id, ground):
    """Open tasks need a project unless marked H0 (ground level); those with neither are inbox items."""
    return not project_id and not ground


def back(default="next_view"):
    """Stay on the page the form was on: nothing jumps elsewhere unless you follow a link or "Save & exit"."""
    return redirect(request.referrer or url_for(default))


@app.route("/")
def home():
    return redirect(url_for("next_view"))


@app.post("/capture")
def capture():
    title = form_value("title")
    if title:
        db.add_task(conn(), title)
        if not (request.referrer or "").endswith(url_for("inbox")):  # the inbox list already shows it
            flash(f"Added to inbox: {title}")
    return back()


# --- inbox -----------------------------------------------------------------

@app.route("/inbox")
def inbox():
    return render_template("inbox.html", tasks=db.inbox_tasks(conn()), project_groups=db.project_groups(conn()))


@app.post("/inbox/move")
def inbox_move():
    """Inbox's drag panel: file the dropped item into a project (or H0, or a new project) as a later step;
    the project picks its next. Called by fetch, which removes the row itself."""
    c = conn()
    t = db.get_task(c, request.form.get("id", type=int) or 0)
    if not t or not t["inbox"]:  # e.g. a stale tab: it has been filed already
        abort(400)
    fields = {k: v for k, v in task_fields_from_form().items() if k in ("project_id", "ground")}
    if orphaned(fields.get("project_id"), fields.get("ground")):
        abort(400)
    db.update_task(c, t["id"], **fields)
    return "", 204


# --- lists -----------------------------------------------------------------

def toggle_url(show, default=False):
    """This page's URL with ?done flipped, keeping other query args. A page that shows completed by default flips ?done=0."""
    args = {k: v for k, v in request.args.items() if k != "done" and k not in request.view_args}
    flip = {} if show == default else {"done": int(not default)}
    return url_for(request.endpoint, **request.view_args, **flip, **args)


def completed(default=False, **filters):
    """Template variables for the "Show completed" toggle (?done=1, or ?done=0 to hide where shown by default): the done tasks, newest completion first."""
    show = request.args.get("done", "1" if default else "0") == "1"
    return {"show_done": show, "done_tasks": db.done_tasks(conn(), **filters) if show else [],
            "toggle_url": toggle_url(show, default)}


@app.route("/next")
def next_view():
    return render_template("next.html", tasks=db.next_list(conn()), project_groups=db.project_groups(conn()),
                           **completed(ground=0))


def add_next_action():
    """Add the form's task as a next action: a later step if its project already has one, the inbox if orphaned."""
    if not form_value("title"):  # before reading the form, which may create a "＋ New…" project
        return
    fields = task_fields_from_form()
    pid = fields.get("project_id")
    fields["step"] = "later" if pid and db.project_next(conn(), pid) else "next"
    if orphaned(pid, fields.get("ground")):
        flash("No project and not H0 · ground level, so it went to the inbox to be processed.")
    db.add_task(conn(), fields.pop("title"), **fields)


@app.post("/next/add")
def next_add():
    add_next_action()
    return back("next_view")


@app.route("/h0")
def h0_view():
    return render_template("h0.html", tasks=db.h0_actions(conn()), **completed(ground=1))


def soon(due):
    """How soon an ISO due date is: overdue, today, week (≤7 days), month (≤30), later; '' when undated."""
    if not due:
        return ""
    days = (date.fromisoformat(due) - today()).days
    return "overdue" if days < 0 else "today" if days == 0 else "week" if days <= 7 else "month" if days <= 30 else "later"


# Next and H0 fold into these bands: (key, label, open by default, the soon() keys in it); undated tasks are "No date".
BANDS = [("now", "Now", True, ("overdue", "today")), ("week", "This week", False, ("week",)),
         ("later", "Later", False, ("month", "later")), ("nodate", "No date", False, ("",))]


@app.template_filter()
def bands(tasks):
    """The tasks split into BANDS, as (key, label, open, tasks), leaving out empty bands."""
    out = [(key, label, is_open, [t for t in tasks if soon(t["due_date"]) in keys]) for key, label, is_open, keys in BANDS]
    return [b for b in out if b[3]]


@app.post("/h0/add")
def h0_add():
    fields = task_fields_from_form()
    if fields.get("title"):
        db.add_task(conn(), fields.pop("title"), ground=1, project_id=None,
                    **{k: v for k, v in fields.items() if k not in ("ground", "project_id", "step")})
    return redirect(url_for("h0_view"))


@app.route("/waiting")
def waiting():
    return render_template("list.html", title="Waiting for", tasks=db.tasks_with_step(conn(), "waiting"),
                           projects=db.projects(conn(), "waiting"), empty="Nothing delegated.",
                           project_groups=db.project_groups(conn()))


@app.post("/waiting/add")
def waiting_add():
    if form_value("title"):
        fields = task_fields_from_form()
        fields["ground"] = 0  # H0 tasks are always next actions
        if orphaned(fields.get("project_id"), 0):
            flash("No project, so it went to the inbox to be processed.")
        db.add_task(conn(), fields.pop("title"), step="waiting", **fields)
    return redirect(url_for("waiting"))


@app.route("/someday")
def someday():
    return render_template("someday.html", projects=db.projects(conn(), "someday"),
                           steps=db.open_steps(conn(), "someday"))


@app.post("/someday/add")
def someday_add():
    title = form_value("title")
    if title:
        db.add_project(conn(), title, status="someday")
    return redirect(url_for("someday"))


# --- reference -------------------------------------------------------------

@app.route("/reference")
def reference():
    return render_template("reference.html", items=db.references(conn()))


@app.post("/reference/add")
def reference_add():
    title = form_value("title")
    if title:  # a new item opens at once, so its details can go straight in
        return redirect(url_for("reference_view", id=db.add_reference(conn(), title)))
    return back("reference")


@app.route("/reference/<int:id>", methods=["GET", "POST"])
def reference_view(id):
    c = conn()
    r = db.get_reference(c, id) or abort(404)
    if request.method == "POST":
        db.update_reference(c, id, title=form_value("title") or r["title"],
                            notes=(request.form.get("notes") or "").strip())
        flash("Saved.")
        return back("reference")
    return render_template("reference_item.html", r=r)


@app.post("/reference/<int:id>/delete")
def reference_delete(id):
    db.delete_reference(conn(), id)
    return redirect(url_for("reference"))


@app.template_filter()
def links(text):
    """Every URL in a note (http, obsidian://, …), for the reference page's clickable links."""
    return re.findall(r"\b[a-z][a-z0-9+.-]*://[^\s<>\"]*[^\s<>\".,;:!?')\]]", text or "", re.I)


@app.route("/done")
def done():
    return render_template("done.html", tasks=db.done_tasks(conn()), projects=db.projects(conn(), "done"),
                           abandoned=db.done_tasks(conn(), state="abandoned"))


# The weekly review's steps, in order; review.html has one section per key.
REVIEW_STEPS = ("sweep", "inbox", "overdue", "stalled", "nexts", "waiting", "someday", "ahead", "horizons")


@app.template_filter()
def local_date(utc):
    """The local date of a SQLite datetime('now') stamp, which is UTC: a task finished at 9pm is done today."""
    return datetime.fromisoformat(utc).replace(tzinfo=timezone.utc).astimezone().date().isoformat() if utc else ""


@app.template_filter()
def long_date(iso):
    return date.fromisoformat(iso).strftime("%a %-d %b %Y")


@app.route("/review")
def review():
    c = conn()
    all_next = db.next_actions(c, today())
    nexts = [t for t in all_next if not (t["due_date"] and t["due_date"] >= today().isoformat())]
    checked = db.review_checked(c)
    step = request.args.get("step")
    if step not in REVIEW_STEPS:  # the first step still to do
        step = next((s for s in REVIEW_STEPS if s not in checked), REVIEW_STEPS[0])
    past = db.reviews(c)
    # The last 26 weeks, Monday first, oldest first, each with the review done in it (or None).
    monday = today() - timedelta(days=today().weekday())
    by_week = {}
    for d in past:
        by_week.setdefault(date.fromisoformat(d) - timedelta(days=date.fromisoformat(d).weekday()), d)
    weeks = [(m, by_week.get(m)) for m in (monday - timedelta(weeks=k) for k in range(25, -1, -1))]
    return render_template("review.html", stalled=db.stalled_projects(c), overdue=db.overdue(c, today()),
                           nexts=nexts, scheduled=len(all_next) - len(nexts),
                           steps=REVIEW_STEPS, step=step, checked=checked, past=past, weeks=weeks)


@app.route("/review/<step>", methods=["POST"])
def review_tick(step):
    """Tick or untick a review step. Ticking the last one finishes the review."""
    if step not in REVIEW_STEPS:
        abort(404)
    c = conn()
    db.toggle_review_step(c, step)
    if db.review_checked(c) >= set(REVIEW_STEPS):
        db.complete_review(c, today())
        flash(f"Weekly review completed on {long_date(today().isoformat())}.")
        return redirect(url_for("review"))
    return redirect(url_for("review") if step in db.review_checked(c) else url_for("review", step=step))


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    return render_template("search.html", q=q, results=db.search(conn(), q) if q else None)


@app.route("/search/suggest")
def search_suggest():
    """JSON for the search box's dropdown: [{title, url, kind, meta}]."""
    out = []
    for kind, r in db.suggest(conn(), request.args.get("q") or ""):
        if kind == "task":
            out.append({"title": r["title"], "url": url_for("task_view", id=r["id"]),
                         "kind": r["state"] if r["state"] != "open" else "H0" if r["ground"]
                                 else "inbox" if r["inbox"] else r["step"],
                         "meta": r["project_title"] or ""})
        elif kind == "project":
            out.append({"title": r["title"], "url": url_for("project_view", id=r["id"]), "kind": "project",
                        "meta": "" if r["status"] == "active" else r["status"]})
        elif kind == "reference":
            out.append({"title": r["title"], "url": url_for("reference_view", id=r["id"]), "kind": "reference",
                        "meta": ""})
        else:
            url = url_for("horizon_view", id=r["id"]) if r["level"] in ("goal", "area") else url_for("horizons_view")
            out.append({"title": r["title"], "url": url, "kind": r["level"], "meta": ""})
    return out


# --- tasks -----------------------------------------------------------------

@app.route("/tasks/<int:id>", methods=["GET", "POST"])
def task_view(id):
    c = conn()
    t = db.get_task(c, id) or abort(404)
    if request.method == "POST":
        fields = task_fields_from_form()
        if not fields.get("title"):
            fields.pop("title", None)
        project_id = fields["project_id"] if "project_id" in fields else t["project_id"]
        ground = fields["ground"] if "ground" in fields else t["ground"]
        if orphaned(project_id, ground) and not t["inbox"] and t["state"] == "open":
            flash("No project and not H0 · ground level, so this went to the inbox to be processed.")
        elif ground and fields.get("step") in ("waiting", "later"):
            flash("H0 tasks are always next actions.")
        flash_demoted(db.update_task(c, id, **fields))
        nxt = request.form.get("next") or None
        if request.form.get("exit"):
            return redirect(nxt or url_for("next_view"))
        flash("Saved.")
        return redirect(url_for("task_view", id=id, back=nxt))  # stay, but keep where "exit" goes
    return render_template("task.html", t=t, project_groups=db.project_groups(c),
                           back=request.args.get("back") or request.referrer,
                           seconds=db.task_seconds(c, id, now()))


def flash_demoted(titles):
    if titles:
        flash(f"One next action per project: moved {', '.join(f'“{t}”' for t in titles)} to later.")


@app.post("/tasks/<int:id>/complete")
def complete(id):
    c = conn()
    db.get_task(c, id) or abort(404)
    db.complete_task(c, id, today(), now())
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    if request.form.get("exit"):
        return redirect(request.form.get("next") or url_for("next_view"))
    return back()


@app.post("/tasks/<int:id>/done-then-project")
def complete_then_project(id):
    """✓→: finish a step and open its project, to pick the next action. The one deliberate jump."""
    c = conn()
    t = db.get_task(c, id) or abort(404)
    db.complete_task(c, id, today(), now())
    return redirect(url_for("project_view", id=t["project_id"])) if t["project_id"] else back()


@app.post("/tasks/<int:id>/delete")
def delete(id):
    """Abandons the task (it stays on Done, revivable); deleting an abandoned task deletes it for good."""
    c = conn()
    t = db.get_task(c, id)
    if t and t["state"] == "abandoned":
        db.delete_task(c, id)
    elif t:
        db.abandon_task(c, id, now())
        return back()  # an abandoned task's page still shows it, with Revive
    nxt = request.form.get("next")
    if nxt and urlsplit(nxt).path == url_for("task_view", id=id):  # e.g. after a save; the page is gone now
        nxt = None
    return redirect(nxt or url_for("next_view"))


@app.post("/tasks/<int:id>/revive")
def revive(id):
    db.revive_task(conn(), id)
    flash("Revived.")
    return back()


@app.post("/tasks/<int:id>/make-next")
def make_next(id):
    c = conn()
    db.get_task(c, id) or abort(404)
    flash_demoted(db.update_task(c, id, step="next"))
    return back()


# --- projects --------------------------------------------------------------

@app.route("/projects")
def projects_view():
    show = request.args.get("done") == "1"
    return render_template("projects.html", projects=db.projects(conn()), steps=db.open_steps(conn()), IDLE_DAYS=IDLE_DAYS,
                           show_done=show, toggle_url=toggle_url(show),
                           done_projects=db.projects(conn(), "done") if show else [])


@app.post("/projects/order/<any(active, someday):status>")
def projects_order(status):
    """The Projects and Someday pages' drag-and-drop sends the project ids in their new order."""
    db.reorder_projects(conn(), status, [int(i) for i in request.form.get("ids", "").split(",") if i.strip().isdigit()])
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return back()


@app.post("/projects")
def project_create():
    title = form_value("title")
    if title:
        db.add_project(conn(), title)
    return back("projects_view")


IDLE_DAYS = 14  # a project untouched this long fades on the Projects page


@app.template_filter()
def idle_days(utc):
    """Whole days since a UTC 'YYYY-MM-DD HH:MM:SS' stamp."""
    return (now() - datetime.fromisoformat(utc).replace(tzinfo=timezone.utc)).days


@app.route("/projects/<int:id>", methods=["GET", "POST"])
def project_view(id):
    c = conn()
    p = db.get_project(c, id) or abort(404)
    if request.method == "POST":
        fields = {k: form_value(k) for k in ("title", "notes") if k in request.form}
        if "notes" in fields:
            fields["notes"] = fields["notes"] or ""
        fields["title"] = fields.get("title") or p["title"]
        status = request.form.get("status")
        if status == "done" and db.project_tasks(c, id):
            flash("Finish, move or delete the project's open tasks first.")
        elif status in ("active", "waiting", "someday", "done"):
            fields["status"] = status
        db.update_project(c, id, **fields)
        if fields.get("status") == "someday":
            flash(f"Moved “{fields['title']}” to someday / maybe.")
        return back()
    return render_template("project.html", p=p, tasks=db.project_tasks(c, id), has_next=bool(db.project_next(c, id)),
                           seconds=db.project_seconds(c, id, now()),
                           **completed(default=True, project_id=id))


@app.post("/projects/<int:id>/delete")
def project_delete(id):
    c = conn()
    p = db.get_project(c, id) or abort(404)
    if db.delete_project(c, id):
        flash(f"Deleted “{p['title']}”.")
        return redirect(url_for("projects_view"))
    flash("Only an empty project can be deleted: finish or delete its tasks first.")
    return redirect(url_for("project_view", id=id))


@app.post("/projects/<int:id>/order")
def project_order(id):
    """The project page's drag-and-drop sends the task ids in their new order."""
    c = conn()
    db.get_project(c, id) or abort(404)
    db.reorder_project(c, id, [int(i) for i in request.form.get("ids", "").split(",") if i.strip().isdigit()])
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return redirect(url_for("project_view", id=id))


@app.post("/projects/<int:id>/tasks")
def project_add_task(id):
    c = conn()
    db.get_project(c, id) or abort(404)
    fields = task_fields_from_form()
    if fields.get("title"):
        fields.setdefault("step", "later" if db.project_next(c, id) else "next")
        db.add_task(c, fields.pop("title"), project_id=id, **fields)
    return redirect(url_for("project_view", id=id))


# --- calendar --------------------------------------------------------------

def sunday(d):
    """The Sunday starting d's calendar week."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


@app.post("/tasks/<int:id>/due")
def task_due(id):
    """The calendar's drag-and-drop moves an open task to the day it was dropped on."""
    c = conn()
    t = db.get_task(c, id) or abort(404)
    try:
        due = date.fromisoformat(request.form.get("due_date", "")).isoformat()
    except ValueError:
        abort(400)
    if t["state"] != "open":
        abort(400)
    db.update_task(c, id, due_date=due)
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return back("calendar_view")


@app.route("/calendar", methods=["GET", "POST"])
def calendar_view():
    if request.method == "POST":
        add_next_action()
        return back("calendar_view")
    week = None
    # A bare /calendar opens the last view used (cookie `cal_view`); an explicit ?w= or ?m= sets it.
    if "w" in request.args or ("m" not in request.args and request.cookies.get("cal_view") == "week"):
        try:
            week = date.fromisoformat(request.args.get("w", ""))
        except ValueError:
            week = today()
        week = sunday(week)
        first = week.replace(day=1)
        start, end = week, week + timedelta(days=6)
    else:
        try:
            first = date.fromisoformat(request.args.get("m", "") + "-01")
        except ValueError:
            first = today().replace(day=1)
        last = recur.add_months(first, 1) - timedelta(days=1)
        start, end = sunday(first), sunday(last) + timedelta(days=6)
    by_day = defaultdict(list)  # iso date -> [task]; a repeat shows only on its due date, no future copies
    for t in db.dated_tasks(conn(), start, end):
        by_day[t["due_date"]].append(t)
    weeks = [[start + timedelta(days=7 * w + i) for i in range(7)] for w in range(((end - start).days + 1) // 7)]
    if week:
        prev, next_ = ((week + timedelta(days=d)).isoformat() for d in (-7, 7))
    else:
        prev, next_ = (recur.add_months(first, d).strftime("%Y-%m") for d in (-1, 1))
    resp = make_response(render_template(
        "calendar.html", first=first, week=week, end=end, project_groups=db.project_groups(conn()), weeks=weeks,
        by_day=by_day, prev=prev, next=next_,
        this_week=sunday(today()).isoformat(), this_month=today().strftime("%Y-%m")))
    resp.set_cookie("cal_view", "week" if week else "month", max_age=365 * 86400, samesite="Lax")
    return resp


# --- timer -----------------------------------------------------------------
# Every time entry belongs to a task. Times are stored in UTC and shown in local time.

def local_input(iso):
    """A stored UTC time as the local "YYYY-MM-DDTHH:MM" a datetime-local input shows."""
    return datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%dT%H:%M")


@app.template_filter("local_minute")
def local_minute(iso):
    """A stored UTC time as the local "YYYY-MM-DD HH:MM" the reminder box shows and dates.parse_when() reads back."""
    return iso and datetime.fromisoformat(iso).astimezone().strftime("%Y-%m-%d %H:%M")


@app.template_filter("hm")
def hm(seconds):
    m = round(seconds or 0) // 60
    return f"{m // 60}h {m % 60:02d}m"


@app.template_filter("hms")
def hms(seconds):
    s = max(0, int(seconds or 0))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}"


@app.template_filter("elapsed")
def elapsed(iso):
    return hms((now() - datetime.fromisoformat(iso)).total_seconds())


@app.template_filter("epoch_ms")
def epoch_ms(iso):
    return int(datetime.fromisoformat(iso).timestamp() * 1000)


@app.route("/timer")
def timer_view():
    c = conn()
    midnight = lambda d: datetime.combine(d, datetime.min.time()).astimezone()
    monday = midnight(today() - timedelta(days=today().weekday()))
    return render_template("timer.html", entries=db.time_entries(c), open_tasks=db.open_tasks(c),
                           day=db.week_totals(c, midnight(today()), now()),
                           week=db.week_totals(c, monday, now()), local_input=local_input)


@app.post("/timer/start")
def timer_start():
    """Time the open task with this title, or capture it to the inbox first: everything timed is in the system."""
    c, title = conn(), form_value("title")
    if not title:
        return back("timer_view")
    t = next((t for t in db.open_tasks(c) if t["title"].lower() == title.lower()), None)
    if t:
        tid = t["id"]
    else:
        tid = db.add_task(c, title)
        flash(f"Captured to inbox: {title}")
    db.timer_start(c, tid, now())
    return back("timer_view")


@app.post("/tasks/<int:id>/start")
def task_start(id):
    t = db.get_task(conn(), id) or abort(404)
    if t["state"] == "open":
        db.timer_start(conn(), id, now())
    return back()


@app.post("/timer/stop")
def timer_stop():
    db.timer_stop(conn(), now())
    return back("timer_view")


@app.post("/timer/<int:id>")
def entry_edit(id):
    """Only fields changed from what the form showed are saved, so untouched times keep their seconds.
    Setting an end time on the running entry stops it."""
    c = conn()
    e = db.get_entry(c, id) or abort(404)
    fields = {}
    try:
        for name, col in (("start", "start_time"), ("end", "end_time")):
            raw = form_value(name)
            if raw and raw != (e[col] and local_input(e[col])):
                t = datetime.fromisoformat(raw).astimezone()  # the form's time is local
                if t > now():
                    raise ValueError("That time is in the future.")
                fields[col] = db.iso(t)
    except ValueError as err:
        flash(str(err) if "future" in str(err) else "Didn't understand that time.")
        return back("timer_view")
    start, end = fields.get("start_time", e["start_time"]), fields.get("end_time", e["end_time"])
    if end and end < start:
        flash("The end is before the start.")
        return back("timer_view")
    db.update_entry(c, id, **fields)
    return back("timer_view")


@app.post("/timer/<int:id>/delete")
def entry_delete(id):
    db.delete_entry(conn(), id)
    return back("timer_view")


# --- reminders -------------------------------------------------------------
# A background thread posts each due reminder to ntfy, which pushes it to the phone. Only with NTFY_URL set;
# gunicorn runs one worker, so there is one thread.

def ntfy(t):
    headers = {"Tags": "bell"}
    if GTD_URL:
        headers["Click"] = f"{GTD_URL}/tasks/{t['id']}"
    urllib.request.urlopen(urllib.request.Request(NTFY_URL, data=t["title"].encode(), headers=headers), timeout=10)


def send_reminders(c, at, send):
    """Send every due reminder; one that fails stays unsent and is tried again next time."""
    for t in db.due_reminders(c, at):
        try:
            send(t)
        except OSError as err:
            app.logger.warning("reminder %s not sent: %s", t["id"], err)
        else:
            db.mark_reminded(c, t["id"], at)


def reminder_loop():
    while True:
        time.sleep(60 - time.time() % 60)  # on the minute, so a 9:00 reminder comes at 9:00
        try:
            c = db.connect()
            try:
                send_reminders(c, datetime.now(timezone.utc), ntfy)
            finally:
                c.close()
        except Exception:
            app.logger.exception("reminders")


if NTFY_URL:
    threading.Thread(target=reminder_loop, daemon=True, name="reminders").start()


# --- horizons --------------------------------------------------------------

@app.route("/horizons")
def horizons_view():
    return render_template("horizons.html", levels=HORIZON_LEVELS, items=db.horizons(conn()),
                           projects=db.projects(conn()), ground=db.h0_actions(conn()))


@app.post("/horizons")
def horizon_create():
    level, title = request.form.get("level"), form_value("title")
    if level not in ("goal", "area") or not title:
        abort(400)
    db.add_horizon(conn(), level, title)
    return back("horizons_view")


@app.route("/horizons/<int:id>", methods=["GET", "POST"])
def horizon_view(id):
    c = conn()
    h = db.get_horizon(c, id) or abort(404)
    if request.method == "POST":
        db.update_horizon(c, id, title=form_value("title") or h["title"],
                          notes=(request.form.get("notes") or "").strip())
        flash("Saved.")
        return back()
    return render_template("horizon.html", h=h, label=dict((l, lab) for l, lab, _ in HORIZON_LEVELS)[h["level"]])


@app.post("/horizons/<int:id>/delete")
def horizon_delete(id):
    db.delete_horizon(conn(), id)
    return redirect(url_for("horizons_view"))



# --- autosave --------------------------------------------------------------

@app.route("/autosave/<any(tasks, projects, horizons, reference):kind>/<int:id>", methods=["GET", "POST"])
def autosave(kind, id):
    """A record page's title and notes save themselves (static/autosave.js). Each change comes with the text the
    page last saw (`base_<field>`); 409 with the current text when someone else changed it since."""
    c = conn()
    row = c.execute(f"SELECT title, notes FROM {kind} WHERE id = ?", (id,)).fetchone() or abort(404)
    if request.method == "GET":
        return {k: row[k] for k in ("title", "notes")}
    fields = {k: request.form[k].replace("\r\n", "\n").strip() for k in ("title", "notes") if k in request.form}
    if not fields.get("title", True):
        fields.pop("title")  # a title can't be blank; keep the old one until there's a new one
    theirs = db.save_text(c, kind, id, fields, {k: request.form.get("base_" + k) for k in fields})
    return (theirs, 409) if theirs else {}


if __name__ == "__main__":
    app.run(host=os.environ.get("GTD_HOST", "127.0.0.1"), port=int(os.environ.get("GTD_PORT", 5005)),
            debug=bool(os.environ.get("GTD_DEBUG")))
