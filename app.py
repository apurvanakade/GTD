import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone

from flask import (Flask, abort, flash, g, get_flashed_messages, make_response, redirect, render_template, request,
                   session, url_for)

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


@app.before_request
def same_origin_only():
    """There's no login, so refuse a write another site's page makes the browser send (CSRF). Browsers say where a
    request comes from in Sec-Fetch-Site; clients that aren't browsers (the phone's ntfy buttons) don't send it."""
    if request.method == "POST" and request.headers.get("Sec-Fetch-Site", "same-origin") not in ("same-origin", "none"):
        abort(403)


@app.context_processor
def template_globals():
    timer = db.timer_running(conn())
    return {"counts": db.counts(conn(), today()), "today": today().isoformat(),
            "timer": timer, "stopped": None if timer else stopped_timer(),
            "embed": request.args.get("embed") == "1"}  # a task page inside Process (inbox.html): no chrome


STOPPED = timedelta(minutes=10)  # how long a stopped timer's pill stays, to finish its task or start it again


def stopped_timer():
    """The entry just stopped, while its pill stays: its task still open, within STOPPED, and not dismissed (×)."""
    e = db.timer_last(conn())
    if (e and e["task_state"] == "open" and now() - datetime.fromisoformat(e["end_time"]) <= STOPPED
            and session.get("pill_dismissed") != e["id"]):
        return e


NEW = "__new__"  # project_select's "＋ New project…" option (_task.html)


def form_value(name):
    """Stripped form value, or None when blank."""
    return (request.form.get(name) or "").strip() or None


def task_fields_from_form(current=None):
    """Editable task fields present in the submitted form. On an add line (no `current` task) the title may end
    with >due, *repeat and !remind (split_title())."""
    fields = {}
    for name in ("title", "notes", "due_date", "recur"):
        if name in request.form:
            fields[name] = form_value(name)
    if "notes" in fields:
        fields["notes"] = fields["notes"] or ""
    new_project = form_value("new_project")
    raw = request.form.get("project_id")
    if raw is not None and not (raw == NEW and not new_project):  # "＋ New project…" with no name: unchanged
        # The picker offers "h0": ground level, a deliberate choice of no project.
        fields["ground"] = int(raw == "h0")
        fields["project_id"] = int(raw) if raw.isdigit() else None
    if new_project:  # typed into the "new project" box; overrides the dropdown
        fields["project_id"] = db.add_project(conn(), new_project)
        fields["ground"] = 0
    if request.form.get("step") in ("next", "waiting", "later", "someday"):  # a project's or H0's step; inbox items drop it
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


# An add line's title can end with these, in any order: "pay rent @nov 1 *monthly !oct 30 9am". Each needs a space
# before it and none after, so "Yes! do it", "a @ b", "mail bob@x.com" and a bare "!3pm" stay plain titles.
TITLE_MARKS = {"@": ("due_date", parse_due), "*": ("recur", parse_recur), "!": ("remind_at", parse_reminder)}
TITLE_MARK = re.compile(r"\s([@*!])(\S[^@*!]*?)\s*$")


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


def tickle_from_form(fields):
    """The form's tickler date (`tickle_on`, in words) into fields: blank clears it; unreadable is flashed, left out;
    one already come is flashed too, since it's back in the Inbox at once."""
    if "tickle_on" in request.form:
        raw = form_value("tickle_on")
        if not raw:
            fields["tickle_on"] = None
        elif due := parse_due(raw):
            fields["tickle_on"] = due
            if due <= today().isoformat():
                flash("Its tickler date has come: it's back in the Inbox.")
    return fields


@app.template_global()
def someday_url(x):
    """A row of db.tickled() or db.someday_to_review(): a task's page or a project's."""
    return url_for("task_view" if x["kind"] == "task" else "project_view", id=x["id"])


def orphaned(project_id, ground):
    """Open tasks need a project unless marked H0 (ground level); those with neither are inbox items."""
    return not project_id and not ground


def back(default="next_view"):
    """Stay on the page the form was on: nothing jumps elsewhere unless you follow a link or "Save & exit"."""
    return redirect(request.referrer or url_for(default))


@app.route("/nav")
def nav():
    """Just the nav, for refreshing its dots after a change made in place (refreshNav() in static/app.js)."""
    return render_template("_nav.html")


@app.route("/")
def home():
    return redirect(url_for("next_view"))


@app.post("/capture")
def capture():
    fields = task_fields_from_form()
    if fields.get("title"):
        title = fields.pop("title")
        db.add_task(conn(), title, today=today(), **fields)
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
    return render_template("inbox.html", tasks=db.inbox_tasks(conn()), tickled=db.tickled(conn(), today()),
                           project_groups=db.project_groups(conn()))


@app.post("/tasks/move")
def task_move():
    """The drop panel on every task list: move the dropped open tasks (one row, or several shift-selected, in page
    order) to another project (or H0, H0's someday / maybe, a new project, Reference), all or none. A task lands as
    a later step, since the project picks its next (in H0, a next action); a waiting task stays waiting. Dropped where
    it already is, nothing changes. Called by fetch, which updates the page itself."""
    c = conn()
    tasks = [db.get_task(c, id) for id in request.form.getlist("id", type=int)]
    if not tasks or not all(t and t["state"] == "open" for t in tasks):  # e.g. a stale tab: finished since
        abort(400)
    if request.form.get("project_id") == "reference":  # not actionable: kept as a reference item instead
        db.task_to_reference(c, [t["id"] for t in tasks], now())
        return "", 204
    maybe = request.form.get("project_id") == "someday"  # not committed to: an H0 someday task
    with db.transaction(c):
        fields = {"project_id": None, "ground": 1} if maybe else \
            {k: v for k, v in task_fields_from_form().items() if k in ("project_id", "ground")}
        if orphaned(fields.get("project_id"), fields.get("ground")):
            abort(400)
        for t in reversed(tasks):  # each goes to the top of its project: the last first keeps the page's order
            if (fields.get("project_id") == t["project_id"] and bool(fields.get("ground")) == bool(t["ground"])
                    and maybe == (t["step"] == "someday")):
                continue
            if maybe:
                step = {"step": "someday"}
            elif fields.get("project_id"):
                step = {"step": "waiting" if t["step"] == "waiting" else "later"}
            else:  # into H0: a next action (_fit_step()), unless it's waiting there already
                step = {"step": "waiting" if t["step"] == "waiting" else "next"}
            db.update_task(c, t["id"], today=today(), **fields, **step)
    return "", 204


@app.post("/tasks/order")
def tasks_order():
    """A task list's drag-and-drop (Inbox, a band of Next, Waiting, Someday) sends its task ids in their new order."""
    db.reorder_tasks(conn(), [int(i) for i in request.form.get("ids", "").split(",") if i.strip().isdigit()])
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return back()


# --- lists -----------------------------------------------------------------

def toggle_url(show, arg="done"):
    """This page's URL with ?done (or another flag) flipped, keeping other query args."""
    args = {k: v for k, v in request.args.items() if k != arg and k not in request.view_args}
    flip = {} if show else {arg: 1}
    return url_for(request.endpoint, **request.view_args, **flip, **args)


def completed(abandoned=False, **filters):
    """Template variables for the "Show completed" toggle (?done=1): the done tasks, newest completion first, and with
    abandoned=True the abandoned ones too, listed below them."""
    show = request.args.get("done") == "1"
    return {"show_done": show, "done_tasks": db.done_tasks(conn(), **filters) if show else [],
            "abandoned_tasks": db.done_tasks(conn(), state="abandoned", **filters) if show and abandoned else [],
            "toggle_url": toggle_url(show)}


@app.route("/next")
def next_view():
    return render_template("next.html", tasks=db.next_list(conn()), project_groups=db.project_groups(conn()),
                           **completed())


def add_next_action():
    """Add the form's task as a next action: a later step if its project already has one, the inbox if orphaned."""
    if not form_value("title"):  # before reading the form, which may create a "＋ New…" project
        return
    fields = task_fields_from_form()
    pid = fields.get("project_id")
    fields["step"] = "later" if pid and db.project_next(conn(), pid) else "next"
    if orphaned(pid, fields.get("ground")):
        flash("No project and not H0 · ground level, so it went to the inbox to be processed.")
    db.add_task(conn(), fields.pop("title"), today=today(), **fields)


@app.post("/next/add")
def next_add():
    add_next_action()
    return back("next_view")


def soon(due):
    """How soon an ISO due date is: overdue, today, week (≤7 days), month (≤30), later; '' when undated."""
    if not due:
        return ""
    days = (date.fromisoformat(due) - today()).days
    return "overdue" if days < 0 else "today" if days == 0 else "week" if days <= 7 else "month" if days <= 30 else "later"


# Next folds into these bands: (key, label, open by default, the soon() keys in it); undated tasks are "No date".
BANDS = [("now", "Now", True, ("overdue", "today")), ("week", "This week", False, ("week",)),
         ("later", "Later", False, ("month", "later")), ("nodate", "No date", False, ("",))]


@app.template_filter()
def bands(tasks):
    """The tasks split into BANDS, as (key, label, open, tasks), leaving out empty bands."""
    out = [(key, label, is_open, [t for t in tasks if soon(t["due_date"]) in keys]) for key, label, is_open, keys in BANDS]
    return [b for b in out if b[3]]


@app.route("/waiting")
def waiting():
    """Waiting projects, then waiting tasks (H0's and project steps, in one dragged order)."""
    c = conn()
    return render_template("waiting.html", projects=db.projects(c, "waiting"), tasks=db.tasks_with_step(c, "waiting"),
                           steps=db.open_steps(c, "waiting"), project_groups=db.project_groups(c))


@app.post("/waiting/add")
def waiting_add():
    if form_value("title"):
        fields = task_fields_from_form()
        if orphaned(fields.get("project_id"), fields.get("ground")):
            flash("No project and not H0 · ground level, so it went to the inbox to be processed.")
        db.add_task(conn(), fields.pop("title"), today=today(), step="waiting", **fields)
    return redirect(url_for("waiting"))


@app.route("/someday")
def someday():
    """H0's someday tasks, then someday projects. What's tickled to a later date is out of sight until then,
    unless ?tickled=1 asks for it."""
    c = conn()
    projects, tasks = db.projects(c, "someday"), db.someday_tasks(c)
    later = lambda x: x["tickle_on"] and x["tickle_on"] > today().isoformat()
    tickled = sum(map(bool, map(later, [*projects, *tasks])))
    show = request.args.get("tickled") == "1"
    if not show:
        projects, tasks = [p for p in projects if not later(p)], [t for t in tasks if not later(t)]
    return render_template("someday.html", projects=projects, tasks=tasks,
                           steps=db.open_steps(c, "someday"), tickled=tickled, show_tickled=show,
                           tickled_url=toggle_url(show, "tickled"), project_groups=db.project_groups(c))


@app.post("/someday/add")
def someday_add():
    """Someday's add line (and the review's new ideas): a someday project (kind=project), else an H0 someday task."""
    if request.form.get("kind") == "project":
        add_project_from_form("someday")
    elif form_value("title"):
        fields = task_fields_from_form()
        db.add_task(conn(), fields.pop("title"), today=today(), ground=1, step="someday",
                    **{k: v for k, v in fields.items() if k not in ("ground", "project_id", "step")})
    return back("someday")


@app.post("/tasks/<int:id>/reviewed")
def task_reviewed(id):
    """The review's someday step: this H0 someday task has been looked at now."""
    db.get_task(conn(), id) or abort(404)
    db.mark_reviewed(conn(), id, now(), table="task")
    return back("review")


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


@app.route("/done")
def done():
    return render_template("done.html", tasks=db.done_tasks(conn()), projects=db.projects(conn(), "done"),
                           abandoned=db.done_tasks(conn(), state="abandoned"), dropped=db.abandoned_projects(conn()))


# The weekly review's steps, in Allen's order (get clear, get current, get creative); review.html has one card per key.
REVIEW_STEPS = ("collect", "inbox", "sweep", "overdue", "nexts", "past", "ahead", "waiting", "projects",
                "checklists", "someday", "creative", "horizons")
LOOK_BACK = timedelta(days=14)  # the review's look back: tasks finished this long ago and since


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
    nexts = [t for t in db.next_actions(c, today()) if not (t["due_date"] and t["due_date"] >= today().isoformat())]
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
    since = datetime.combine(today() - LOOK_BACK, datetime.min.time()).astimezone().astimezone(timezone.utc)
    return render_template("review.html", projects=db.projects_to_review(c),
                           overdue=db.overdue(c, today()), someday=db.someday_to_review(c, today()),
                           nexts=nexts, recent=db.done_since(c, since.strftime("%Y-%m-%d %H:%M:%S")),
                           checklists=db.checklists(c), unseen=unseen,
                           steps=REVIEW_STEPS, step=step, checked=checked, past=past, weeks=weeks)


def unseen(rows):
    """How many of these (projects, someday tasks) the review hasn't looked at in the last week."""
    return sum(not (r["reviewed_at"] and (today() - date.fromisoformat(local_date(r["reviewed_at"]))).days < 7)
               for r in rows)


@app.post("/projects/<int:id>/reviewed")
def project_reviewed(id):
    """The review's projects and someday steps: this project has been looked at now."""
    db.get_project(conn(), id) or abort(404)
    db.mark_reviewed(conn(), id, now())
    return back("review")


@app.template_filter()
def ago(utc):
    """How long ago a UTC stamp was, by local date: today, 3d ago, 2w ago, 4mo ago; never without one."""
    if not utc:
        return "never"
    days = (today() - date.fromisoformat(local_date(utc))).days
    return "today" if days <= 0 else f"{days}d ago" if days < 14 else f"{days // 7}w ago" if days < 60 else f"{days // 30}mo ago"


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
                         "kind": r["state"] if r["state"] != "open" else "inbox" if r["inbox"] else r["step"],  # next, waiting, later, someday
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
        # Title and notes are autosave's, with its conflict check: a select posting this stale form leaves them.
        fields = {k: v for k, v in task_fields_from_form(t).items() if k not in ("title", "notes")}
        project_id = fields["project_id"] if "project_id" in fields else t["project_id"]
        ground = fields["ground"] if "ground" in fields else t["ground"]
        if (project_id, bool(ground)) != (t["project_id"], bool(t["ground"])) and "step" in fields and fields["step"] == t["step"]:
            fields.pop("step")  # moved, with the step select untouched: it lands as a move does (db._fit_step())
        if orphaned(project_id, ground) and not t["inbox"] and t["state"] == "open":
            flash("No project and not H0 · ground level, so this went to the inbox to be processed.")
        flash_demoted(db.update_task(c, id, today=today(), **fields))
        if request.headers.get("X-Requested-With") == "fetch":  # a field saved in the background: just its messages
            return {"flashes": get_flashed_messages()}
        return redirect(url_for("task_view", id=id, embed=request.args.get("embed")))
    return render_template("task.html", t=t, project_groups=db.project_groups(c), seconds=db.task_seconds(c, id, now()))


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
    return redirect(url_for("done"))  # where abandoned tasks are listed; its own page is gone


@app.post("/tasks/<int:id>/revive")
def revive(id):
    db.revive_task(conn(), id)
    flash("Revived.")
    return back()


@app.post("/tasks/<int:id>/make-next")
def make_next(id):
    c = conn()
    db.get_task(c, id) or abort(404)
    flash_demoted(db.update_task(c, id, today=today(), step="next"))
    return back()


# --- projects --------------------------------------------------------------

@app.route("/projects")
def projects_view():
    show = request.args.get("done") == "1"
    return render_template("projects.html", projects=db.projects(conn()), steps=db.open_steps(conn()),
                           show_done=show, toggle_url=toggle_url(show),
                           done_projects=db.projects(conn(), "done") if show else [])


@app.post("/projects/order/<any(active, waiting, someday):status>")
def projects_order(status):
    """The Projects, Waiting and Someday pages' drag-and-drop sends the project ids in their new order."""
    c = conn()
    db.reorder_projects(c, status, [int(i) for i in request.form.get("ids", "").split(",") if i.strip().isdigit()])
    if request.headers.get("X-Requested-With") == "fetch":
        return "", 204
    return back()


def add_project_from_form(status="active"):
    """The add line's new project (Projects, Someday)."""
    if title := form_value("title"):
        db.add_project(conn(), title, status=status)


@app.post("/projects")
def project_create():
    add_project_from_form()
    return back("projects_view")


PROJECT_STATUSES = ("active", "waiting", "someday", "done")  # what the status select sets; "abandoned" abandons


@app.route("/projects/<int:id>", methods=["GET", "POST"])
def project_view(id):
    c = conn()
    p = db.get_project(c, id) or abort(404)
    if request.method == "POST":
        fields = {}  # title and notes are autosave's, as on the task page
        status = request.form.get("status")
        if status == "abandoned" and p["status"] != "abandoned":  # with its open steps; it's on Done, revivable
            db.abandon_project(c, id, now())
            flash(f"Abandoned “{p['title']}”: it's on Done.")
            return back()
        if p["status"] == "abandoned" and status in PROJECT_STATUSES:  # revived with its steps, then given the status
            db.revive_project(c, id)
        if status == "done" and db.project_tasks(c, id):
            flash("Finish, move or delete the project's open tasks first.")
        elif status in PROJECT_STATUSES:
            fields["status"] = status
        with db.transaction(c):
            db.update_project(c, id, **fields)
            tickle = tickle_from_form({})
            if "tickle_on" in tickle and fields.get("status", p["status"]) == "someday":  # someday projects only
                db.set_project_tickle(c, id, tickle["tickle_on"])
        if fields.get("status") == "someday" != p["status"]:
            flash(f"Moved “{p['title']}” to someday / maybe.")
        return back()
    return render_template("project.html", p=p, tasks=db.project_tasks(c, id), has_next=bool(db.project_next(c, id)),
                           project_groups=db.project_groups(c), deletable=db.project_deletable(c, id),
                           times=db.step_totals(c, id, now()), **completed(abandoned=True, project_id=id))


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
    flash("Only a project that never had tasks can be deleted: mark it done or abandoned instead.")
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
        db.add_task(c, fields.pop("title"), today=today(), project_id=id, **fields)
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
    db.update_task(c, id, today=today(), due_date=due)
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
        tid = db.add_task(c, title, today=today())
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


@app.post("/timer/dismiss")
def timer_dismiss():
    """× on a stopped timer's pill: gone until another stretch is stopped."""
    e = db.timer_last(conn())
    if e:
        session["pill_dismissed"] = e["id"]
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
        db.update_task(c, id, today=today(), remind_at=db.iso(now() + SNOOZE))
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
        level = "purpose"  # the top of the deck
    return render_template("horizons.html", levels=HORIZON_LEVELS, steps=HORIZON_STEPS, step=level, titles=HORIZON_TITLES,
                           items=db.horizons(conn()), projects=db.projects(conn()), ground=db.h0_actions(conn()))


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
    return render_template("horizon.html", h=h, label=HORIZON_TITLES[h["level"]])


@app.post("/horizons/<int:id>/delete")
def horizon_delete(id):
    h = db.get_horizon(conn(), id) or abort(404)
    db.delete_horizon(conn(), id)
    return redirect(url_for("horizons_view", level=h["level"]))


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
