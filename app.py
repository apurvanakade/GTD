import json
import os
import re
import threading
import time
import urllib.error
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


def local_now():
    """Naive local time, as dates.parse_when() takes it."""
    return datetime.combine(today(), now().astimezone().time())


@app.context_processor
def template_globals():
    return {"counts": db.counts(conn(), today()), "today": today().isoformat(),
            "timer": db.timer_running(conn()), "areas": db.areas(conn()), "IDLE_DAYS": IDLE_DAYS}


NEW = "__new__"  # project_select's "＋ New project…" option, one per area: __new__:<area id> (_task.html)


def form_value(name):
    """Stripped form value, or None when blank."""
    return (request.form.get(name) or "").strip() or None


def area_from(raw):
    """The id of the area a form names, or None."""
    h = raw and str(raw).isdigit() and db.get_horizon(conn(), int(raw))
    return h["id"] if h and h["level"] == "area" else None


def task_fields_from_form(current=None):
    """Editable task fields present in the submitted form. On an add line (no `current` task) the title may end
    with >due, *repeat and !remind (split_title())."""
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
    if new_project:  # typed into the "new project" box; overrides the dropdown. Its area: the "＋ New…" picked, or area_id
        raw = request.form.get("project_id") or ""
        area_id = area_from(raw.partition(":")[2] if raw.startswith(NEW) else request.form.get("area_id"))
        if area_id:
            fields["project_id"] = db.add_project(conn(), new_project, area_id=area_id)
            fields["ground"] = 0
        else:
            flash(f"A project needs an area: “{new_project}” wasn't created. Pick “＋ New project…” under an area.")
    if request.form.get("step") in ("next", "waiting", "later"):  # a project's step; H0 and inbox tasks drop it
        fields["step"] = request.form["step"]
    for name, parse in (("due_date", parse_due), ("recur", parse_recur)):
        if fields.get(name):
            fields[name] = parse(fields[name])
            if not fields[name]:
                fields.pop(name)
    if "remind_at" in request.form:
        raw = form_value("remind_at")
        if not raw:
            fields["remind_at"] = None
        elif not (current and raw == local_minute(current["remind_at"])):  # what the page showed, maybe long past: left alone
            remind_at = parse_reminder(raw)
            if remind_at:
                fields["remind_at"] = remind_at
    if current is None and fields.get("title"):
        fields["title"], typed = split_title(fields["title"])
        fields.update(typed)  # typed into the title: wins over the line's own boxes
    if fields.get("recur") and not fields.get("due_date"):
        fields["due_date"] = today().isoformat()
    return fields


def parse_due(raw):
    """A due date typed in words as ISO, or None, flashed."""
    due = dates.parse(raw, today())
    if not due:
        flash(f"Didn't understand date “{raw}” – try “tomorrow”, “saturday” or “oct 30”.")
    return due and due.isoformat()


def parse_recur(raw):
    """A repeat rule, or None, flashed."""
    if not recur.parse(raw):
        flash(f"Didn't understand repeat rule “{raw}” – try “weekly” or “every 6 months”.")
        return None
    return raw


def parse_reminder(raw):
    """A reminder typed in words (local time) as a UTC ISO time, or None, flashed, when unreadable or already past."""
    when = dates.parse_when(raw, local_now())
    if not when:
        flash(f"Didn't understand reminder “{raw}” – try “tomorrow 9am”, “fri 14:00” or “in 2 hours”.")
    elif when <= local_now():
        flash(f"Reminder “{raw}” has already passed – not set.")
    else:
        return db.iso(when.astimezone())


# An add line's title can end with these, in any order: "pay rent >nov 1 *monthly !oct 30 9am". Each needs a space
# before it and none after, so "Yes! do it", "a > b" and a bare "!3pm" stay plain titles.
TITLE_MARKS = {">": ("due_date", parse_due), "*": ("recur", parse_recur), "!": ("remind_at", parse_reminder)}
TITLE_MARK = re.compile(r"\s([>*!])(\S[^>*!]*?)\s*$")


def split_title(title):
    """(title, fields) with the trailing marks peeled off the end. The first unusable one (and all before it) stays
    in the title, so nothing typed is lost."""
    fields = {}
    while m := TITLE_MARK.search(title):
        name, parse = TITLE_MARKS[m.group(1)]
        value = name not in fields and parse(m.group(2))
        if not value:
            break
        fields[name], title = value, title[:m.start()].rstrip()
    return title, fields


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
    fields = task_fields_from_form()
    if fields.get("title"):
        title = fields.pop("title")
        db.add_task(conn(), title, **fields)
        if not (request.referrer or "").endswith(url_for("inbox")):  # the inbox list already shows it
            parts = [f"Added to inbox: {title}"]
            if "due_date" in fields:
                parts.append(f"due {fields['due_date']}")
            if "recur" in fields:
                parts.append(f"↻ {fields['recur']}")
            if "remind_at" in fields:
                parts.append(f"reminder {local_minute(fields['remind_at'], WHEN)}")
            flash(" · ".join(parts))
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
    if request.form.get("project_id") == "reference":  # not actionable: kept as a reference item instead
        db.add_reference(c, t["title"], notes=t["notes"])
        if db.task_seconds(c, t["id"], now()):
            db.abandon_task(c, t["id"], now())  # its time is kept
        else:
            db.delete_task(c, t["id"])
        return "", 204
    fields = {k: v for k, v in task_fields_from_form().items() if k in ("project_id", "ground")}
    if orphaned(fields.get("project_id"), fields.get("ground")):
        abort(400)
    db.update_task(c, t["id"], **fields)
    return "", 204


# --- lists -----------------------------------------------------------------

def toggle_url(show, default=False):
    """This page's URL with ?done flipped, keeping other query args. A page that shows completed by default flips ?done=0."""
    args = {k: v for k, v in request.args.items() if k != "done" and k not in request.view_args}
    flip = {} if (not show) == default else {"done": int(not show)}
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
    return render_template("someday.html", groups=db.by_area(conn(), db.projects(conn(), "someday")),
                           steps=db.open_steps(conn(), "someday"))


@app.post("/someday/add")
def someday_add():
    add_project_from_form("someday")
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
                           archived=db.areas(conn(), archived=True),
                           abandoned=db.done_tasks(conn(), state="abandoned"), dropped=db.abandoned_projects(conn()))


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
    return render_template("review.html", stalled=db.stalled_projects(c), no_area=db.no_area(c),
                           overdue=db.overdue(c, today()),
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
            url = url_for("horizon_view", id=r["id"]) if r["level"] in ("goal", "area") else url_for("horizons_view", level=r["level"])
            out.append({"title": r["title"], "url": url, "kind": r["level"], "meta": ""})
    return out


# --- tasks -----------------------------------------------------------------

@app.route("/tasks/<int:id>", methods=["GET", "POST"])
def task_view(id):
    c = conn()
    t = db.get_task(c, id) or abort(404)
    if request.method == "POST":
        fields = task_fields_from_form(t)
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
    return render_template("projects.html", groups=db.by_area(conn(), db.projects(conn())), steps=db.open_steps(conn()),
                           show_done=show, toggle_url=toggle_url(show),
                           done_projects=db.projects(conn(), "done") if show else [])


@app.post("/projects/order/<any(active, someday):status>")
def projects_order(status):
    """The Projects and Someday pages' drag-and-drop sends the project ids in their new order."""
    db.reorder_projects(conn(), status, [int(i) for i in request.form.get("ids", "").split(",") if i.strip().isdigit()])
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return back()


def add_project_from_form(status="active"):
    """The add line's new project (Projects, Someday, an area's page): it needs an area."""
    title, area_id = form_value("title"), area_from(request.form.get("area_id"))
    if title and not area_id:
        flash(f"Pick an area for “{title}”.")
    elif title:
        db.add_project(conn(), title, status=status, area_id=area_id)


@app.post("/projects")
def project_create():
    add_project_from_form()
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
        if area_from(request.form.get("area_id")):  # left blank (a project with none yet): unchanged
            fields["area_id"] = area_from(request.form["area_id"])
        # An abandoned project comes back only through Revive, which brings its steps back too.
        status = None if p["status"] == "abandoned" else request.form.get("status")
        if status == "done" and db.project_tasks(c, id):
            flash("Finish, move or delete the project's open tasks first.")
        elif status in ("active", "waiting", "someday", "done"):
            fields["status"] = status
        db.update_project(c, id, **fields)
        if fields.get("status") == "someday":
            flash(f"Moved “{fields['title']}” to someday / maybe.")
        return back()
    abandoned = db.done_tasks(c, project_id=id, state="abandoned") if p["status"] == "abandoned" else []
    return render_template("project.html", p=p, area=p["area_id"] and db.get_horizon(c, p["area_id"]), tasks=db.project_tasks(c, id), has_next=bool(db.project_next(c, id)),
                           abandoned=abandoned,
                           times=db.step_totals(c, id, now()), **completed(default=True, project_id=id))


@app.post("/projects/<int:id>/abandon")
def project_abandon(id):
    """Give up on a project, with its open steps: it moves to Done, revivable. Stays on the page."""
    c = conn()
    p = db.get_project(c, id) or abort(404)
    db.abandon_project(c, id, now())
    flash(f"Abandoned “{p['title']}”: it's on Done.")
    return back()


@app.post("/projects/<int:id>/revive")
def project_revive(id):
    c = conn()
    db.get_project(c, id) or abort(404)
    db.revive_project(c, id)
    flash("Revived to someday / maybe.")
    return back()


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
        fields.setdefault("step", "later")  # "next" is only ever chosen, never a default
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


WHEN = "%a %b %-d, %H:%M"  # a reminder time in words, as the preview and capture's flash show it


@app.template_filter("local_minute")
def local_minute(iso, fmt="%Y-%m-%d %H:%M"):
    """A stored UTC time as the local "YYYY-MM-DD HH:MM" the reminder box shows and dates.parse_when() reads back."""
    return iso and datetime.fromisoformat(iso).astimezone().strftime(fmt)


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

LATE = timedelta(minutes=2)  # a reminder sent later than this (the app was down) says when it was due
SNOOZE = timedelta(minutes=15)


def ntfy(t, at):
    """Publish as JSON (any title, no header encoding): the title, then the task's project and due date, the first
    line of its notes, and a late note. With GTD_URL: a tap opens the task; Done and Snooze buttons call the app."""
    lines = [(t["project_title"] or ("H0" if t["ground"] else "Inbox")) + (f" · due {t['due_date']}" if t["due_date"] else "")]
    lines += [line.strip() for line in (t["notes"] or "").splitlines() if line.strip()][:1]
    due = datetime.fromisoformat(t["remind_at"])
    if at - due > LATE:
        due, local_at = due.astimezone(), at.astimezone()
        lines.append("Late: was due " + due.strftime("%H:%M" if due.date() == local_at.date() else "%Y-%m-%d %H:%M"))
    base, topic = NTFY_URL.rsplit("/", 1)
    msg = {"topic": topic, "title": t["title"], "message": "\n".join(lines), "tags": ["bell"]}
    if GTD_URL:
        task = f"{GTD_URL}/tasks/{t['id']}"
        msg["click"] = task
        msg["actions"] = [  # an ntfy "http" action: the phone makes the request without opening anything
            {"action": "http", "label": "Done", "url": f"{task}/complete", "method": "POST",
             "headers": {"X-Requested-With": "fetch"}, "clear": True},
            {"action": "http", "label": "Snooze 15m", "url": f"{task}/snooze", "method": "POST", "clear": True}]
    urllib.request.urlopen(urllib.request.Request(base + "/", data=json.dumps(msg).encode(),
                                                  headers={"Content-Type": "application/json"}), timeout=10)


def send_reminders(c, at, send):
    """Send every due reminder. One ntfy rejects (4xx but 429) is given up, marked sent; any other failure stays
    unsent and is tried again next time."""
    for t in db.due_reminders(c, at):
        try:
            send(t, at)
        except urllib.error.HTTPError as err:
            if 400 <= err.code < 500 and err.code != 429:
                app.logger.error("reminder %s rejected, given up: %s", t["id"], err)
                db.mark_reminded(c, t["id"], at)
            else:
                app.logger.warning("reminder %s not sent: %s", t["id"], err)
        except OSError as err:
            app.logger.warning("reminder %s not sent: %s", t["id"], err)
        else:
            db.mark_reminded(c, t["id"], at)


@app.post("/tasks/<int:id>/snooze")
def snooze(id):
    """The notification's Snooze button: remind again in SNOOZE. A finished task stays as it is."""
    c = conn()
    t = db.get_task(c, id) or abort(404)
    if t["state"] == "open":
        db.update_task(c, id, remind_at=db.iso(now() + SNOOZE))
    return "", 204


DAY = "%a %b %-d"  # a date in words, as the Due and Repeats previews show it


def describe_recur(rule):
    """A repeat rule in plain words: "every 2 weeks after done"."""
    n, unit = recur.parse(rule)
    return f"every {unit if n == 1 else f'{n} {unit}s'}" + (" after done" if recur.after_done(rule) else "")


@app.route("/parse/<kind>")
def parse_preview(kind):
    """The task page's live previews of how its Due, Repeats and Remind boxes read. A repeat's next date steps from
    the Due box (`due`), as completing the task would; with no date it gets today, as saving would."""
    q = request.args.get("q") or ""
    if kind == "due":
        d = dates.parse(q, today())
        return {"reads": d and d.strftime(DAY), "past": False}
    if kind == "recur":
        if not recur.parse(q):
            return {"reads": None, "past": False}
        due = dates.parse(request.args.get("due") or "", today()) or today()
        after = " if done today" if recur.after_done(q) else ""
        return {"reads": f"{describe_recur(q)} · next {recur.next_due(q, due, today()).strftime(DAY)}{after}", "past": False}
    if kind == "remind":
        when = dates.parse_when(q, local_now())
        return {"reads": when and when.strftime(WHEN), "past": bool(when and when <= local_now())}
    abort(404)


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

# The Horizons page's cards, top down; horizons.html has one card per key.
HORIZON_STEPS = ("purpose", "vision", "goal", "area", "projects", "ground")
HORIZON_TITLES = {**{level: label for level, label, _ in HORIZON_LEVELS},
                  "projects": "H1 · Projects", "ground": "H0 · Ground level"}


@app.route("/horizons")
def horizons_view():
    level = request.args.get("level")
    if level not in HORIZON_STEPS:
        level = HORIZON_STEPS[0]
    show = request.args.get("done") == "1"
    return render_template("horizons.html", levels=HORIZON_LEVELS, steps=HORIZON_STEPS, step=level, titles=HORIZON_TITLES,
                           items=db.horizons(conn()), projects=db.projects(conn()), ground=db.h0_actions(conn()),
                           show_done=show, toggle_url=toggle_url(show), archived=db.areas(conn(), archived=True) if show else [])


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
    if h["level"] != "area":
        return render_template("horizon.html", h=h, label=HORIZON_TITLES[h["level"]])
    # An area's page is its hub: its projects by status, and an add line for a new one.
    lists = [(label, db.projects(c, status, area_id=id)) for label, status in AREA_LISTS]
    show = request.args.get("done") == "1"
    return render_template("horizon.html", h=h, label=HORIZON_TITLES["area"], lists=lists, steps=db.open_steps(c, None),
                           empty=not c.execute("SELECT 1 FROM projects WHERE area_id = ?", (id,)).fetchone(),
                           show_done=show, toggle_url=toggle_url(show),
                           done_projects=db.projects(c, "done", area_id=id) if show else [])


AREA_LISTS = (("Active", "active"), ("Waiting", "waiting"), ("Someday / maybe", "someday"))


@app.post("/horizons/<int:id>/delete")
def horizon_delete(id):
    h = db.get_horizon(conn(), id) or abort(404)
    if not db.delete_horizon(conn(), id):
        flash("Only an area no project is in can be deleted: move its projects first.")
        return redirect(url_for("horizon_view", id=id))
    return redirect(url_for("horizons_view", level=h["level"]))


@app.post("/horizons/<int:id>/archive")
def area_archive(id):
    """An area that's over (a term's course): it leaves the lists and pickers for Done. Stays on the page."""
    h = db.get_horizon(conn(), id) or abort(404)
    if h["archived_at"]:
        db.unarchive_area(conn(), id)
    elif not db.archive_area(conn(), id):
        flash("Finish, move or abandon its projects first.")
    else:
        flash(f"Archived “{h['title']}”: it's on Done.")
    return back()



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
