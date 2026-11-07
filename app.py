import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlsplit

from flask import (Flask, abort, flash, g, get_flashed_messages, make_response, redirect, render_template, request,
                   session, url_for)

import dates
import db
import extcal
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
CALENDARS = extcal.parse_config(os.environ.get("GTD_CALENDARS"))  # [(name, ics url)] shown on the calendar


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


CAPTURE_VIEWS = ("inbox", "next_view", "waiting", "someday")  # the task lists: base.html puts the capture line on them


@app.context_processor
def template_globals():
    timer = db.timer_running(conn())
    # A record's page in a frame, without chrome: "overlay" (every task, project, horizon and reference item opens in
    # one). base.html leaves out the nav, bar and pill there, so their numbers aren't worked out.
    embed = request.args.get("embed")
    return {"counts": None if embed else db.counts(conn(), today()), "today": today().isoformat(),
            "timer": timer, "stopped": None if timer or embed else stopped_timer(), "embed": embed,
            "capture_line": request.endpoint in CAPTURE_VIEWS, "overlays": [] if embed else opened(),
            "app_url": GTD_URL or request.host_url.rstrip("/"),
            "contexts": db.contexts(conn()), "filter_ids": [x["id"] for x in chosen_contexts()],
            "context_filter": [x["name"] for x in chosen_contexts()], "hidden_by_filter": g.get("hidden_by_filter", 0)}


# A record reached by URL opens over its list: ?project=, ?horizon=, ?reference=, ?task= (a project's step over its
# project, so in that order).
OVERLAYS = (("project", "project_view"), ("horizon", "horizon_view"), ("reference", "reference_view"),
            ("task", "task_view"))


def opened():
    """The overlays a page opens with, bottom first: their frames' URLs."""
    return [url_for(view, id=id, embed="overlay") for kind, view in OVERLAYS
            if (id := request.args.get(kind, type=int))]


STOPPED = timedelta(minutes=10)  # how long a stopped timer's pill stays, to finish its task or start it again


def stopped_timer():
    """The entry just stopped, while its pill stays: its task still open, within STOPPED, and not dismissed (×)."""
    e = db.timer_last(conn())
    if (e and e["task_state"] == "open" and now() - datetime.fromisoformat(e["end_time"]) <= STOPPED
            and session.get("pill_dismissed") != e["id"]):
        return e


NEW = "__new__"  # project_select's "＋ New project…" options (_task.html): __new__, __new__waiting, __new__someday


def form_value(name):
    """Stripped form value, or None when blank."""
    return (request.form.get(name) or "").strip() or None


def form_ids():
    """A drag-and-drop's ids in their new order (the form's comma-separated `ids`)."""
    return [int(i) for i in request.form.get("ids", "").split(",") if i.strip().isdigit()]


def new_project_made(title, status):
    """Every new project is made here, so a background save that made one tells its page (projects_changed())."""
    g.projects_changed = True
    return db.add_project(conn(), title, status=status)


@app.after_request
def projects_changed(r):
    """The page's project lists (project_select(), project_targets()) are stale once a project is made in the
    background: the header has refreshProjects() in static/app.js redraw them all."""
    if g.get("projects_changed"):
        r.headers["X-Projects-Changed"] = "1"
    return r


def fetched():
    """A background save by the page's JS (X-Requested-With: fetch), which updates the page itself."""
    return request.headers.get("X-Requested-With") == "fetch"


def task_fields_from_form(current=None):
    """Editable task fields present in the submitted form. On an add line (no `current` task) the title may end
    with ^due, *repeat, !remind and @context (split_title())."""
    fields = {}
    for name in ("title", "notes", "due_date", "recur"):
        if name in request.form:
            fields[name] = form_value(name)
    if "notes" in fields:
        fields["notes"] = fields["notes"] or ""
    raw = request.form.get("project_id")
    # The box stays posted, hidden, once another project is picked: it counts only with its own "＋ New…" option.
    new_project = form_value("new_project") if raw is None or raw.startswith(NEW) else None
    if raw is not None and not (raw.startswith(NEW) and not new_project):  # "＋ New project…" with no name: unchanged
        # The picker offers "h0": ground level, a deliberate choice of no project.
        fields["ground"] = int(raw == "h0")
        fields["project_id"] = int(raw) if raw.isdigit() else None
    if new_project:  # typed into the "new project" box. Its group sets the status.
        status = (raw or "").removeprefix(NEW)
        fields["project_id"] = new_project_made(new_project, status=status if status in PROJECT_STATUSES else "active")
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
        picked = [*contexts_from_form(), *typed.pop("contexts", ())]  # contexts add up, from the picks and the title
        fields.update(typed)  # typed into the title: wins over the line's own boxes
        if picked:
            fields["contexts"] = picked
    due = fields["due_date"] if "due_date" in fields else current and current["due_date"]  # an unreadable one was dropped
    if fields.get("recur") and not due:
        fields["due_date"] = today().isoformat()
    return fields


def warn(message):
    """Something typed couldn't be read. A flash, and the bookmarklet's popup stays open to show it (capture())."""
    flash(message)
    g.warned = True


def parse_due(raw):
    """A due date typed in words as ISO, or None, flashed."""
    due = dates.parse(raw, today())
    if not due:
        warn(f"Didn't understand date “{raw}” – try “tomorrow”, “saturday” or “oct 30”.")
    return due and due.isoformat()


def parse_recur(raw):
    """A repeat rule, or None, flashed."""
    if not recur.parse(raw):
        warn(f"Didn't understand repeat rule “{raw}” – try “weekly” or “every 6 months”.")
        return None
    return raw


def parse_reminder(raw):
    """A reminder typed in words (local time) as a UTC ISO time, or None, flashed, when unreadable or already past."""
    when = dates.parse_when(raw, local_now())
    if not when:
        warn(f"Didn't understand reminder “{raw}” – try “tomorrow 9am”, “fri 14:00” or “in 2 hours”.")
    elif when <= local_now():
        warn(f"Reminder “{raw}” has already passed – not set.")
    else:
        return db.iso(when.astimezone())


CONTEXT = re.compile(r"[^\W\d_][\w-]*")  # one word, starting with a letter, so @5km and @/notes stay in a title


def parse_context(raw):
    """A context name, or None, flashed: one word, starting with a letter."""
    if CONTEXT.fullmatch(raw):
        return raw
    warn(f"Didn't understand context “{raw}” – a context is one word, starting with a letter.")


def context_name(raw):
    """A name typed in a box (a task's new context, a rename): spaces become hyphens. None, flashed, if it can't be one;
    blank if blank, with nothing to say."""
    name = re.sub(r"\s+", "-", (raw or "").strip().removeprefix("@"))
    return name and parse_context(name)


def contexts_from_form():
    """The form's context words (`context`, one per tick) and a new one's name (`new_context`)."""
    picks = [context_name(raw) for raw in [*request.form.getlist("context"), request.form.get("new_context")]]
    return [name for name in picks if name]


# An add line's title can end with these, in any order: "pay rent ^nov 1 *monthly !oct 30 9am @home". Each needs a
# space before it and none after, so "Yes! do it", "a @ b", "mail bob@x.com" and a bare "!3pm" stay plain titles. Only
# @context can be given more than once. All on the number row, typed with shift.
TITLE_MARKS = {"^": ("due_date", parse_due), "*": ("recur", parse_recur), "!": ("remind_at", parse_reminder),
               "@": ("contexts", parse_context)}
TITLE_MARK = re.compile(r"\s([\^*!@])(\S[^\^*!@]*?)\s*$")


def split_title(title):
    """(title, fields) with the trailing marks peeled off the end. The first unusable one (and all before it) stays
    in the title, so nothing typed is lost, and is flashed."""
    fields = {}
    while m := TITLE_MARK.search(title):
        name, parse = TITLE_MARKS[m.group(1)]
        if name != "contexts" and name in fields:
            warn(f"Only one {m.group(1)} mark: “{m.group(1)}{m.group(2)}” stays in the title.")
            break
        value = parse(m.group(2))
        if not value:
            break
        if name == "contexts":
            fields.setdefault(name, []).append(value)
        else:
            fields[name] = value
        title = title[:m.start()].rstrip()
    return title, fields


def tickle_from_form():
    """The form's tickler date (`tickle_on`, in words): {"tickle_on": date}, or {"tickle_on": None} when blank, or {}
    when absent or unreadable (flashed)."""
    if "tickle_on" not in request.form:
        return {}
    raw = form_value("tickle_on")
    if not raw:
        return {"tickle_on": None}
    due = parse_due(raw)
    return {"tickle_on": due} if due else {}


@app.template_global()
def someday_url(x):
    """A row of db.tickled() or db.someday_to_review(): a task's page or a project's."""
    return url_for("task_view" if x["kind"] == "task" else "project_view", id=x["id"])


def orphaned(project_id, ground, say=None):
    """Open tasks need a project unless marked H0 (ground level); those with neither are inbox items. With `say`
    ("it", "this"), flashes where it went."""
    if not project_id and not ground:
        if say:
            flash(f"No project and not H0 · ground level, so {say} went to the inbox to be processed.")
        return True
    return False


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


@app.get("/capture")
def capture_page():
    """The bookmarklet's popup (bookmarklet() in base.html's ? overlay): capture from any web page, its link too."""
    return render_template("capture.html", embed="popup", url=request.args.get("url", ""),
                           page_title=request.args.get("title", ""))


def bookmark(fields):
    """The popup's "Bookmark link": the page goes on top of the notes as a Markdown link, its title the task's if none."""
    link = form_value("link")
    if not link:
        return
    name = re.sub(r"\s+", " ", re.sub(r"[\[\]]", "", request.form.get("page_title", ""))).strip() or link
    fields["title"] = fields.get("title") or name
    fields["notes"] = "\n".join(filter(None, [f"[{name}]({quote(link, safe=SAFE_URL)})", fields.get("notes")]))


SAFE_URL = ":/?#[]@!$&'*+,;=%~"  # all but ( ) and spaces, which would end a Markdown link


@app.post("/capture")
def capture():
    fields = task_fields_from_form()
    bookmark(fields)
    if fields.get("title"):
        title = fields.pop("title")
        db.add_task(conn(), title, today=today(), **fields)
        if urlsplit(request.referrer or "").path != url_for("inbox"):  # the inbox list already shows it
            parts = [f"Added to inbox: {title}"]
            if "due_date" in fields:
                parts.append(f"due {fields['due_date']}")
            if "recur" in fields:
                parts.append(f"↻ {fields['recur']}")
            if fields.get("remind_at"):
                parts.append(f"reminder {local_minute(fields['remind_at'], WHEN)}")
            parts += [f"@{name}" for name in fields.get("contexts", ())]
            flash(" · ".join(parts))
    if fetched():  # the capture dialog, in the background: whatever is open under it stays
        return {"flashes": get_flashed_messages(), "warned": g.get("warned", False)}
    return back()


# --- inbox -----------------------------------------------------------------

@app.route("/inbox")
def inbox():
    return render_template("inbox.html", tasks=db.inbox_tasks(conn()), tickled=db.tickled(conn(), today()),
                           project_groups=db.project_groups(conn()))


@app.post("/tasks/move")
def task_move():
    """The drop panel on every task list: move the dropped open tasks (one row, or several shift-selected, in page
    order) to a task tab (H0's Next, Waiting or Someday: that step), a project (a new one too) or Reference, all or
    none. Into a project a task lands as a later step, since the project picks its next; a waiting task stays
    waiting. Dropped where it already is, nothing changes. Called by fetch, which updates the page itself."""
    c = conn()
    tasks = [db.get_task(c, id) for id in request.form.getlist("id", type=int)]
    if not tasks or not all(t and t["state"] == "open" for t in tasks):  # e.g. a stale tab: finished since
        abort(400)
    if request.form.get("project_id") == "reference":  # not actionable: kept as a reference item instead
        db.task_to_reference(c, [t["id"] for t in tasks], now())
        return "", 204
    h0_step = H0_TARGETS.get(request.form.get("project_id"))  # a task tab: that H0 step
    with db.transaction(c):
        fields = {"project_id": None, "ground": 1} if h0_step else \
            {k: v for k, v in task_fields_from_form().items() if k in ("project_id", "ground")}
        if orphaned(fields.get("project_id"), fields.get("ground")):
            abort(400)
        for t in reversed(tasks):  # each goes to the top of its project: the last first keeps the page's order
            if fields.get("project_id") == t["project_id"] and bool(fields.get("ground")) == bool(t["ground"]) \
                    and h0_step in (None, t["step"]):
                continue
            # Into a project: a later step, unless it's waiting.
            step = h0_step or ("waiting" if t["step"] == "waiting" else "later")
            db.update_task(c, t["id"], today=today(), **fields, step=step)
    return "", 204


H0_TARGETS = {"h0": "next", "waiting": "waiting", "someday": "someday"}  # the drop panel's task tabs: H0, that step


@app.post("/tasks/order")
def tasks_order():
    """A task list's drag-and-drop (Inbox, a band of Next, Waiting, Someday) sends its task ids in their new order."""
    db.reorder_tasks(conn(), form_ids())
    return ("", 204) if fetched() else back()


# --- lists -----------------------------------------------------------------

# The context filter: the checkboxes on Site info (static/app.js), kept in the `contexts` cookie as ids joined by "-".
# It holds across pages but only thins the working lists: Next, Waiting, Someday and the Calendar. Not the Inbox,
# where everything lands, nor the project pages, the review or the nav's dots.
FILTERED_VIEWS = ("next_view", "waiting", "someday", "calendar_view")


def chosen_contexts():
    """The contexts checked, in their order; a deleted one's id means nothing."""
    ids = request.cookies.get("contexts", "").split("-")
    return [x for x in db.contexts(conn()) if str(x["id"]) in ids]


def filtered(tasks):
    """These tasks minus those in none of the checked contexts; all of them with none checked."""
    chosen = chosen_contexts()
    if not chosen:
        return tasks
    keep = db.tasks_in_contexts(conn(), [x["id"] for x in chosen])
    shown = [t for t in tasks if t["id"] in keep]
    g.hidden_by_filter = g.get("hidden_by_filter", 0) + len(tasks) - len(shown)
    return shown


def stay_in_view(fields):
    """A task added on a filtered list takes the checked contexts, or it would vanish as it's added. A @mark wins; if
    that leaves it hidden, it says so."""
    if urlsplit(request.referrer or "").path not in map(url_for, FILTERED_VIEWS):
        return
    chosen = [x["name"] for x in chosen_contexts()]
    if chosen and "contexts" not in fields:
        fields["contexts"] = chosen
    elif chosen and not {n.lower() for n in fields["contexts"]} & {n.lower() for n in chosen}:
        flash(f"Added @{' @'.join(fields['contexts'])}, but the filter hides it.")


@app.post("/tasks/<int:id>/contexts")
def task_contexts(id):
    """The task page's context picks."""
    db.get_task(conn(), id) or abort(404)
    db.set_task_contexts(conn(), id, contexts_from_form())
    return ("", 204) if fetched() else back()


@app.post("/contexts/order")
def contexts_order():
    """Site info's list, dragged."""
    db.reorder_contexts(conn(), form_ids())
    return ("", 204) if fetched() else back()


@app.post("/contexts/<int:id>/rename")
def context_rename(id):
    """Site info's name box, in the background: a name another context has merges into that one. Answers what the row
    now is."""
    x = db.get_context(conn(), id) or abort(404)
    name = context_name(request.form.get("name"))
    if not form_value("name"):
        warn("A context needs a name.")
    x = db.get_context(conn(), db.rename_context(conn(), id, name) if name else id)
    return {"id": x["id"], "name": x["name"], "open": x["open"], "flashes": get_flashed_messages()}


@app.post("/contexts/<int:id>/delete")
def context_delete(id):
    """Site info's ✕, once confirmed: its tasks stay."""
    db.get_context(conn(), id) or abort(404)
    db.delete_context(conn(), id)
    return ("", 204) if fetched() else back()


def toggle_url(show, arg="done"):
    """This page's URL with ?done (or another flag) flipped, keeping other query args."""
    args = {k: v for k, v in request.args.items()
            if k != arg and k not in request.view_args and k not in dict(OVERLAYS)}  # not reopening an overlay
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
    done = completed()
    return render_template("next.html", tasks=filtered(db.next_list(conn())), project_groups=db.project_groups(conn()),
                           **{**done, "done_tasks": filtered(done["done_tasks"])})


def add_next_action():
    """The calendar's add line: the form's task as a next action (beside any its project has), the inbox if orphaned."""
    if not form_value("title"):  # before reading the form, which may create a "＋ New…" project
        return
    fields = task_fields_from_form()
    pid = fields.get("project_id")
    fields["step"] = "next"
    orphaned(pid, fields.get("ground"), say="it")
    stay_in_view(fields)
    db.add_task(conn(), fields.pop("title"), today=today(), **fields)


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
    """Waiting tasks, H0's and project steps, in one dragged order. Waiting projects have their own tab."""
    c = conn()
    return render_template("waiting.html", tasks=filtered(db.tasks_with_step(c, "waiting")), project_groups=db.project_groups(c))


def hide_tickled(rows):
    """Someday's rows (tasks or projects) without those tickled to a later date, unless ?tickled=1 asks for them,
    and the template variables for that toggle."""
    later = lambda x: x["tickle_on"] and x["tickle_on"] > today().isoformat()
    show = request.args.get("tickled") == "1"
    return ([x for x in rows if show or not later(x)],
            {"tickled": sum(map(bool, map(later, rows))), "show_tickled": show, "tickled_url": toggle_url(show, "tickled")})


@app.route("/someday")
def someday():
    """H0's someday tasks; what's tickled to a later date is out of sight until then. Someday projects have their
    own tab."""
    tasks, toggle = hide_tickled(filtered(db.someday_tasks(conn())))
    return render_template("someday.html", tasks=tasks, project_groups=db.project_groups(conn()), **toggle)


@app.post("/someday/add")
def someday_add():
    """The review's new ideas: an H0 someday task or a someday project (kind=project)."""
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
    if not request.args.get("embed"):  # opens over Reference, like a task over its list
        return redirect(url_for("reference", reference=id))
    return render_template("reference_item.html", r=r, up=url_for("reference"))


@app.post("/reference/<int:id>/delete")
def reference_delete(id):
    db.delete_reference(conn(), id)
    return redirect(url_for("reference"))


DONE_DAYS = 7  # Done lists what was closed in this many days, today included


@app.route("/done")
def done():
    """The last DONE_DAYS days' closed projects and tasks (a repeat's latest only), done ones split by done_bands."""
    c = conn()
    since = datetime.combine(today() - timedelta(days=DONE_DAYS - 1), datetime.min.time()).astimezone()
    closed = lambda state: dict(projects=db.closed_projects(c, state, limit=-1, since=since),
                                tasks=db.done_tasks(c, limit=-1, state=state, since=since, once=True))
    return render_template("done.html", done=closed("done"), abandoned=closed("abandoned"))


@app.template_filter()
def done_bands(closed):
    """Done's rows as (key, label, [(is_project, row)]), Today then Past week, projects first; empty ones left out."""
    rows = [(True, p) for p in closed["projects"]] + [(False, t) for t in closed["tasks"]]
    now = [r for r in rows if local_date(r[1]["completed_at"]) >= today().isoformat()]
    out = [("today", "Today", now), ("week", "Past week", [r for r in rows if r not in now])]
    return [b for b in out if b[2]]


# The weekly review's steps, in Allen's order (get clear, get current, get creative); review.html has one card per key.
REVIEW_STEPS = ("collect", "inbox", "sweep", "overdue", "nexts", "ahead", "waiting", "projects",
                "checklists", "someday", "creative", "horizons")


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
    return render_template("review.html", projects=db.projects_to_review(c),
                           open_steps={**db.open_steps(c), **db.open_steps(c, "waiting")},
                           overdue=db.overdue(c, today()), someday=db.someday_to_review(c, today()),
                           nexts=db.next_actions(c, today()), waiting=db.tasks_with_step(c, "waiting"),
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
    with db.transaction(c):
        db.toggle_review_step(c, step)
        checked = db.review_checked(c)
        finished = checked >= set(REVIEW_STEPS)
        if finished:
            db.complete_review(c, today())
    if finished:
        flash(f"Weekly review completed on {long_date(today().isoformat())}.")
        return redirect(url_for("review"))
    return redirect(url_for("review") if step in checked else url_for("review", step=step))


@app.route("/search")
def search():
    q = (request.args.get("q") or "").strip()
    results = db.search(conn(), q) if q else None
    capped = bool(results) and any(len(rows) >= db.SEARCH_LIMIT for rows in results.values())
    return render_template("search.html", q=q, results=results, capped=capped, limit=db.SEARCH_LIMIT)


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
            out.append({"title": r["title"], "url": horizon_url(r), "kind": r["level"], "meta": ""})
    return out


# --- tasks -----------------------------------------------------------------

@app.route("/tasks/<int:id>", methods=["GET", "POST"])
def task_view(id):
    c = conn()
    t = db.get_task(c, id) or abort(404)
    if request.method == "POST":
        if request.form.get("project_id") == "reference" and t["state"] == "open":  # File to's Reference: as a drop on it
            item = db.task_to_reference(c, [id], now())[0]
            return redirect(url_for("reference_view", id=item, embed=request.args.get("embed")))
        # Title and notes are autosave's, with its conflict check: a select posting this stale form leaves them.
        fields = {k: v for k, v in task_fields_from_form(t).items() if k not in ("title", "notes")}
        project_id = fields["project_id"] if "project_id" in fields else t["project_id"]
        ground = fields["ground"] if "ground" in fields else t["ground"]
        if (project_id, bool(ground)) != (t["project_id"], bool(t["ground"])) and "step" in fields and fields["step"] == t["step"]:
            fields.pop("step")  # moved, with the step select untouched: it lands as a move does (db._fit_step())
        if not t["inbox"] and t["state"] == "open":
            orphaned(project_id, ground, say="this")
        db.update_task(c, id, today=today(), **fields)
        if fetched():  # a field saved in the background: its messages, and how the boxes read (app.js shows them, so
            saved = db.get_task(c, id)  # words like "3pm" aren't read again, later, by the next post of this form)
            shown = {"due_date": saved["due_date"], "remind_at": local_minute(saved["remind_at"])}
            return {"flashes": get_flashed_messages(), "saved": {k: v or "" for k, v in shown.items() if k in fields}}
        return redirect(url_for("task_view", id=id, embed=request.args.get("embed")))
    if not request.args.get("embed"):  # a task has no page of its own: its list, with it open over it
        return redirect(task_home(t, task=id))
    up = url_for("project_view", id=t["project_id"]) if t["project_id"] else task_home(t)
    return render_template("task.html", t=t, project_groups=db.project_groups(c), seconds=db.task_seconds(c, id, now()),
                           up=up)


def task_home(t, **args):
    """The list a task is opened over when it's reached by URL (a reminder's tap, a new tab): over its project, else
    the list it's on, else Done."""
    if t["project_id"]:
        return project_home(db.get_project(conn(), t["project_id"]), **args)
    if t["state"] != "open":
        return url_for("done", **args)
    if t["inbox"]:
        return url_for("inbox", **args)
    return url_for({"waiting": "waiting", "someday": "someday"}.get(t["step"], "next_view"), **args)


@app.post("/tasks/<int:id>/complete")
def complete(id):
    c = conn()
    db.get_task(c, id) or abort(404)
    db.complete_task(c, id, today(), now())
    return ("", 204) if fetched() else back()


@app.post("/tasks/<int:id>/done-then-project")
def complete_then_project(id):
    """✓→: finish a step and open its project, to pick the next action. The one deliberate jump."""
    c = conn()
    t = db.get_task(c, id) or abort(404)
    db.complete_task(c, id, today(), now())
    # It's pressed in a task's frame but opens a page: with no project (a stale frame), the task's list, with it open.
    return redirect(url_for("project_view", id=t["project_id"]) if t["project_id"] else task_home(t, task=id))


@app.post("/tasks/<int:id>/delete")
def delete(id):
    """Abandons the task (it stays on Done, revivable); deleting a done or abandoned task deletes it for good."""
    c = conn()
    t = db.get_task(c, id)
    if t and t["state"] != "open":
        db.delete_task(c, id)
    elif t:
        db.abandon_task(c, id, now())
        return back()  # an abandoned task's page still shows it, with Revive
    return redirect(url_for("done"))  # where closed tasks are listed; its own page is gone


@app.post("/tasks/<int:id>/revive")
def revive(id):
    db.revive_task(conn(), id)
    flash("Revived.")
    return back()


@app.post("/tasks/<int:id>/make-next")
def make_next(id):
    c = conn()
    db.get_task(c, id) or abort(404)
    db.update_task(c, id, today=today(), step="next")
    return back()


@app.post("/tasks/<int:id>/make-later")
def make_later(id):
    """A project's next action back to a later step (an H0 task has no later: it stays next)."""
    c = conn()
    db.get_task(c, id) or abort(404)
    db.update_task(c, id, today=today(), step="later")
    return back()


# --- projects --------------------------------------------------------------

@app.route("/projects", defaults={"status": "active"})
@app.route("/projects/<any(waiting, someday):status>")
def projects_view(status):
    """A project tab: the active, waiting or someday projects. Active offers the closed ones (?done=1); someday
    hides what's tickled to a later date (?tickled=1 shows it)."""
    c = conn()
    projects, toggle = (hide_tickled if status == "someday" else lambda ps: (ps, {}))(db.projects(c, status))
    show = status == "active" and request.args.get("done") == "1"
    return render_template("projects.html", status=status, projects=projects, steps=db.open_steps(c, status),
                           show_done=show, toggle_url=toggle_url(show),
                           done_projects=[p for s in db.CLOSED for p in db.closed_projects(c, s)] if show else [],
                           **toggle)


@app.post("/projects/order/<any(active, waiting, someday):status>")
def projects_order(status):
    """A project tab's drag-and-drop sends the project ids in their new order."""
    c = conn()
    db.reorder_projects(c, status, form_ids())
    return ("", 204) if fetched() else back()


@app.post("/projects/<int:id>/move")
def project_move(id):
    """A project row dropped on a nav tab (Active, Waiting, Someday projects) takes that status; open projects only."""
    c = conn()
    p = db.get_project(c, id) or abort(404)
    status = request.form.get("status")
    if status not in PROJECT_STATUSES or p["status"] not in PROJECT_STATUSES:
        abort(400)
    if status != p["status"]:
        db.update_project(c, id, status=status)
    return ("", 204) if fetched() else back("projects_view")


def add_project_from_form(status="active"):
    """The add line's new project (a project tab, the review's new ideas)."""
    if title := form_value("title"):
        new_project_made(title, status=status)


@app.post("/projects")
def project_create():
    """A project tab's add line: a project of that tab's status (the hidden `status`)."""
    status = request.form.get("status")
    add_project_from_form(status if status in PROJECT_STATUSES else "active")
    return back("projects_view")


PROJECT_STATUSES = ("active", "waiting", "someday")  # what the status select sets; done or abandoned closes it


def project_list(p, **args):
    """The list a project is on: its project tab, else Done."""
    if p["status"] in PROJECT_STATUSES:
        return url_for("projects_view", status=p["status"], **args)
    return url_for("done", **args)


def project_home(p, **args):
    """What a project reached by URL opens as: its list, with its overlay (?project=<id>) over it."""
    return project_list(p, project=p["id"], **args)


@app.route("/projects/<int:id>", methods=["GET", "POST"])
def project_view(id):
    c = conn()
    p = db.get_project(c, id) or abort(404)
    if request.method == "POST":
        fields = {}  # title and notes are autosave's, as on the task page
        status = request.form.get("status")
        if status in db.CLOSED and p["status"] not in db.CLOSED:  # with its open steps; it's on Done, revivable
            db.close_project(c, id, status, now=now())
            flash(f"{'Done' if status == 'done' else 'Abandoned'}: “{p['title']}”. It's on Done.")
            return back()
        with db.transaction(c):
            if p["status"] in db.CLOSED and status in PROJECT_STATUSES:  # revived with its steps, then the status
                db.revive_project(c, id)
            if status in PROJECT_STATUSES:
                fields["status"] = status
            db.update_project(c, id, **fields)
            if "outcome" in request.form:
                db.set_project_outcome(c, id, request.form["outcome"])
            tickle = tickle_from_form()
            if tickle and fields.get("status", p["status"]) == "someday":  # someday projects only
                db.set_project_tickle(c, id, tickle["tickle_on"])
                if tickle["tickle_on"] and tickle["tickle_on"] <= today().isoformat():
                    flash("Its tickler date has come: it's back in the Inbox.")
        if fields.get("status") == "someday" != p["status"]:
            flash(f"Moved “{p['title']}” to someday / maybe.")
        return back()
    if not request.args.get("embed"):  # a project opens over its list, like a task
        return redirect(project_home(p))
    return render_template("project.html", p=p, up=project_list(p), tasks=db.project_tasks(c, id),
                           project_groups=db.project_groups(c), deletable=db.project_deletable(c, id),
                           times=db.step_totals(c, id, now()), **completed(abandoned=True, project_id=id))


@app.post("/projects/<int:id>/reached")
def project_reached(id):
    """✓ on a project's outcome: it's done, its open steps marked done or abandoned (`steps`), as asked."""
    c = conn()
    p = db.get_project(c, id) or abort(404)
    steps = request.form.get("steps", "done")
    if steps not in ("done", "abandoned") or p["status"] not in ("active", "waiting"):
        abort(400)
    n = len(db.project_tasks(c, id))
    db.close_project(c, id, "done", steps, now())
    flash(f"Done: “{p['title']}”" + (f", {n} open step{'s' * (n > 1)} {'marked done' if steps == 'done' else 'abandoned'}." if n else "."))
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
    flash("Only a project that never had tasks can be deleted: mark it done or abandoned instead.")
    return back()


@app.post("/projects/<int:id>/order")
def project_order(id):
    """The project page's drag-and-drop sends the task ids in their new order."""
    c = conn()
    db.get_project(c, id) or abort(404)
    db.reorder_project(c, id, form_ids())
    return ("", 204) if fetched() else back()


@app.post("/projects/<int:id>/tasks")
def project_add_task(id):
    c = conn()
    db.get_project(c, id) or abort(404)
    fields = task_fields_from_form()
    if fields.get("title"):
        fields.setdefault("step", "later")  # "next" is only ever chosen, never a default
        db.add_task(c, fields.pop("title"), today=today(), project_id=id, **fields)
    return back()


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
    return ("", 204) if fetched() else back("calendar_view")


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
    for t in filtered(db.dated_tasks(conn(), start, end)):
        by_day[t["due_date"]].append(t)
    calendars = app.config.get("CALENDARS", CALENDARS)
    events, cal_errors = extcal.events(calendars, start, end)
    weeks = [[start + timedelta(days=7 * w + i) for i in range(7)] for w in range(((end - start).days + 1) // 7)]
    if week:
        prev, next_ = ((week + timedelta(days=d)).isoformat() for d in (-7, 7))
    else:
        prev, next_ = (recur.add_months(first, d).strftime("%Y-%m") for d in (-1, 1))
    resp = make_response(render_template(
        "calendar.html", first=first, week=week, end=end, project_groups=db.project_groups(conn()), weeks=weeks,
        by_day=by_day, events=events, calendars=[name for name, _ in calendars], cal_errors=dict(cal_errors),
        prev=prev, next=next_,
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
    with db.transaction(c):
        tid = t["id"] if t else db.add_task(c, title, today=today())
        db.timer_start(c, tid, now())
    if not t:
        flash(f"Captured to inbox: {title}")
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
# An area's balance, -3 .. 3: how much attention it gets against what it needs.
BALANCE = {-3: "Far too little", -2: "Too little", -1: "A bit too little", 0: "Balanced",
           1: "A bit too much", 2: "Too much", 3: "Far too much"}
# Purpose (H5) and vision (H4) share one card, "purpose": two notes, one above the other.
HORIZON_STEPS = ("purpose", "goal", "area", "projects", "ground")
HORIZON_TITLES = {**{level: label for level, label, _ in HORIZON_LEVELS}, "purpose": "H5 · H4 · Purpose & vision",
                  "projects": "H1 · Projects", "ground": "H0 · Ground level"}
HORIZON_FLOORS = (("H5", "purpose", None), ("H4", "purpose", "vision"), ("H3", "goal", None),  # label, card, place on it
                  ("H2", "area", None), ("H1", "projects", None), ("H0", "ground", None))


@app.template_global()
def horizon_url(h):
    """Where a horizon opens: a goal's or area's overlay, or its place on the card purpose and vision share."""
    if h["level"] in ("goal", "area"):
        return url_for("horizon_view", id=h["id"])
    return url_for("horizons_view", level="purpose", _anchor="vision" if h["level"] == "vision" else None)


@app.route("/horizons")
def horizons_view():
    level = request.args.get("level")
    if level not in HORIZON_STEPS:
        level = None  # the overview: every horizon, folded out
    c = conn()
    return render_template("horizons.html", balance_words=BALANCE, levels=HORIZON_LEVELS, steps=HORIZON_STEPS, floors=HORIZON_FLOORS, step=level, titles=HORIZON_TITLES,
                           items=db.horizons(c), projects=db.projects(c), ground=db.h0_actions(c),
                           graph=None if level else db.horizon_graph(c, BALANCE))


@app.post("/horizons")
def horizon_create():
    level, title = request.form.get("level"), form_value("title")
    if level not in ("goal", "area") or not title:
        abort(400)
    db.add_horizon(conn(), level, title)
    return back("horizons_view")


@app.post("/horizons/order/<any(goal, area):level>")
def horizons_order(level):
    """A goal or area list's drag-and-drop sends the ids in their new order."""
    db.reorder_horizons(conn(), level, form_ids())
    return "", 204


@app.route("/horizons/<int:id>", methods=["GET", "POST"])
def horizon_view(id):
    c = conn()
    h = db.get_horizon(c, id) or abort(404)
    if request.method == "POST":  # title and notes are autosave's, as on the task page
        with db.transaction(c):
            if "outcome" in request.form and h["level"] in ("goal", "area"):
                db.set_horizon_outcome(c, id, request.form["outcome"])
            if "area" in request.form and h["level"] == "goal":
                area = request.form["area"]
                try:
                    db.set_goal_area(c, id, int(area) if area else None)
                except ValueError:
                    abort(400)
        flash("Saved.")
        return back()
    up = url_for("horizon_view", id=h["area_id"]) if h["area_id"] else url_for("horizons_view", level=h["level"])
    if not request.args.get("embed"):  # opens over its Horizons card, like a task over its list
        return redirect(url_for("horizons_view", level=h["level"], horizon=id))
    areas = db.horizons(c)["area"] if h["level"] == "goal" else []
    links = db.horizon_links(c, h) if h["level"] in ("goal", "area") else None
    return render_template("horizon.html", h=h, balance_words=BALANCE, up=up, areas=areas, links=links)


@app.post("/horizons/<int:id>/link")
def horizon_link(id):
    """Hang a project (under a goal or area), or a goal or H0 task (under an area), here: `kind` and `add` (its id),
    from the goal's or area's page; `remove` unlinks one from here. A child has one parent, so linking moves it."""
    c = conn()
    h = db.get_horizon(c, id) or abort(404)
    kind, add, remove = request.form.get("kind"), request.form.get("add", ""), request.form.get("remove", "")
    link = {"project": db.link_project, "task": db.link_task, "goal": db.set_goal_area}.get(kind)
    target = add or remove
    if not link or target and not target.isdigit():
        abort(400)
    linked = db.horizon_links(c, h)[kind + "s"] if h["level"] in ("goal", "area") else []
    if add or target and any(x["id"] == int(target) for x in linked):  # a remove only unlinks it from here; "＋ …" alone, nothing
        try:
            link(c, int(target), id if add else None)
        except ValueError:
            abort(400)
    return ("", 204) if request.headers.get("X-Requested-With") == "fetch" else back()


@app.post("/horizons/<int:id>/vision")
def horizon_vision(id):
    """An area's vision line, from the purpose & vision card, saved as it's left."""
    h = db.get_horizon(conn(), id) or abort(404)
    if h["level"] != "area":
        abort(400)
    db.set_area_vision(conn(), id, request.form.get("vision"))
    return ("", 204) if request.headers.get("X-Requested-With") == "fetch" else back()


@app.post("/horizons/<int:id>/balance")
def horizon_balance(id):
    """An area's meter, saved as it's dragged (static/app.js); blank clears it."""
    h = db.get_horizon(conn(), id) or abort(404)
    value = request.form.get("balance", "").strip()
    if h["level"] != "area" or value and not (value.lstrip("-").isdigit() and int(value) in BALANCE):
        abort(400)
    db.set_area_balance(conn(), id, int(value) if value else None)
    return ("", 204) if request.headers.get("X-Requested-With") == "fetch" else back()


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
