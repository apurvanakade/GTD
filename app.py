import os
from collections import defaultdict
from datetime import date, timedelta

from flask import Flask, abort, flash, g, redirect, render_template, request, url_for

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


@app.context_processor
def template_globals():
    return {"counts": db.counts(conn(), today()), "CONTEXTS": db.contexts(conn()), "today": today().isoformat()}


def form_value(name):
    """Stripped form value, or None when blank."""
    return (request.form.get(name) or "").strip() or None


def task_fields_from_form():
    """Editable task fields present in the submitted form."""
    fields = {}
    for name in ("title", "notes", "context", "due_date", "recur", "waiting_on"):
        if name in request.form:
            fields[name] = form_value(name)
    if "notes" in fields:
        fields["notes"] = fields["notes"] or ""
    if "project_id" in request.form:
        fields["project_id"] = int(request.form["project_id"]) if request.form["project_id"] else None
    if "status" in request.form:
        fields["status"] = request.form["status"]
    if fields.get("recur") and not recur.parse(fields["recur"]):
        flash(f"Didn't understand repeat rule “{fields['recur']}” – try “weekly” or “every 6 months”.")
        fields.pop("recur")
    if fields.get("recur") and not fields.get("due_date"):
        fields["due_date"] = today().isoformat()
    return fields


def back(default="next_view"):
    return redirect(request.form.get("next") or request.referrer or url_for(default))


@app.route("/")
def home():
    return redirect(url_for("next_view"))


@app.post("/capture")
def capture():
    title = form_value("title")
    if title:
        db.add_task(conn(), title, status="inbox")
    return back()


# --- inbox -----------------------------------------------------------------

@app.route("/inbox")
def inbox():
    return render_template("inbox.html", tasks=db.tasks_with_status(conn(), "inbox"))


@app.route("/process")
def process():
    items = db.tasks_with_status(conn(), "inbox")
    if not items:
        flash("Inbox zero.")
        return redirect(url_for("inbox"))
    return render_template("process.html", t=items[0], remaining=len(items), projects=db.projects(conn()))


@app.post("/process/<int:id>")
def process_item(id):
    c = conn()
    t = db.get_task(c, id) or abort(404)
    action = request.form.get("action")
    title = form_value("title") or t["title"]
    notes = (request.form.get("notes") or "").strip()
    if action == "trash":
        db.delete_task(c, id)
    elif action == "done":
        db.update_task(c, id, title=title, notes=notes)
        db.complete_task(c, id, today())
    elif action == "someday":
        db.update_task(c, id, title=title, notes=notes, status="someday")
    elif action == "project":
        pid = db.add_project(c, title, notes=notes)
        db.delete_task(c, id)
        flash("Project created – add its first next action.")
        return redirect(url_for("project_view", id=pid, processing=1))
    elif action in ("next", "waiting"):
        fields = task_fields_from_form()
        fields.update(title=title, notes=notes, status=action)
        if action == "next" and not fields.get("context"):
            flash("Pick a context for the next action.")
            return redirect(url_for("process"))
        if action == "waiting" and not fields.get("waiting_on"):
            flash("Who are you waiting on?")
            return redirect(url_for("process"))
        db.update_task(c, id, **fields)
    else:
        abort(400)
    return redirect(url_for("process"))


# --- lists -----------------------------------------------------------------

@app.route("/next")
def next_view():
    ctx = request.args.get("ctx")
    tasks = db.next_actions(conn(), today())
    by_ctx = {}
    for t in tasks:
        by_ctx.setdefault(t["context"] or "(no context)", []).append(t)
    shown = {ctx: by_ctx.get(ctx, [])} if ctx else dict(sorted(by_ctx.items(), key=lambda kv: (kv[0] != "(no context)", kv[0])))
    return render_template("next.html", groups=shown, by_ctx=by_ctx, ctx=ctx,
                           upcoming=db.upcoming_recurring(conn(), today()))


@app.route("/waiting")
def waiting():
    return render_template("list.html", title="Waiting for", tasks=db.tasks_with_status(conn(), "waiting"),
                           empty="Nothing delegated.")


@app.route("/someday")
def someday():
    return render_template("someday.html", tasks=db.tasks_with_status(conn(), "someday"),
                           projects=db.projects(conn(), "someday"))


@app.route("/review")
def review():
    c = conn()
    return render_template("review.html", stalled=db.stalled_projects(c), overdue=db.overdue(c, today()))


# --- tasks -----------------------------------------------------------------

@app.route("/tasks/<int:id>", methods=["GET", "POST"])
def task_view(id):
    c = conn()
    t = db.get_task(c, id) or abort(404)
    if request.method == "POST":
        fields = task_fields_from_form()
        if not fields.get("title"):
            fields.pop("title", None)
        db.update_task(c, id, **fields)
        return redirect(request.form.get("next") or url_for("task_view", id=id))
    return render_template("task.html", t=t, projects=db.projects(c), back=request.referrer)


@app.post("/tasks/<int:id>/complete")
def complete(id):
    db.complete_task(conn(), id, today())
    return ("", 204) if request.headers.get("X-Requested-With") == "fetch" else back()


@app.post("/tasks/<int:id>/delete")
def delete(id):
    db.delete_task(conn(), id)
    return redirect(request.form.get("next") or url_for("next_view"))


@app.post("/tasks/<int:id>/activate")
def activate(id):
    c = conn()
    t = db.get_task(c, id) or abort(404)
    db.update_task(c, id, status="next")
    if not t["context"]:
        flash("Activated – pick a context.")
        return redirect(url_for("task_view", id=id))
    return back("someday")


# --- projects --------------------------------------------------------------

@app.route("/projects")
def projects_view():
    return render_template("projects.html", projects=db.projects(conn()))


@app.post("/projects")
def project_create():
    title = form_value("title")
    if not title:
        return redirect(url_for("projects_view"))
    return redirect(url_for("project_view", id=db.add_project(conn(), title)))


@app.route("/projects/<int:id>", methods=["GET", "POST"])
def project_view(id):
    c = conn()
    p = db.get_project(c, id) or abort(404)
    if request.method == "POST":
        fields = {k: form_value(k) for k in ("title", "notes") if k in request.form}
        fields["notes"] = fields.get("notes") or ""
        fields["title"] = fields.get("title") or p["title"]
        status = request.form.get("status")
        if status == "done" and db.project_tasks(c, id):
            flash("Finish, move or delete the project's open tasks first.")
        elif status in ("active", "someday", "done"):
            fields["status"] = status
        db.update_project(c, id, **fields)
        if fields.get("status") in ("done", "someday"):
            return redirect(url_for("projects_view"))
        return redirect(url_for("project_view", id=id))
    return render_template("project.html", p=p, tasks=db.project_tasks(c, id),
                           processing=request.args.get("processing"))


@app.post("/projects/<int:id>/tasks")
def project_add_task(id):
    c = conn()
    db.get_project(c, id) or abort(404)
    fields = task_fields_from_form()
    if fields.get("title"):
        fields.setdefault("status", "next")
        db.add_task(c, fields.pop("title"), project_id=id, **fields)
    return redirect(url_for("project_view", id=id, processing=request.args.get("processing")))


# --- calendar --------------------------------------------------------------

@app.route("/calendar", methods=["GET", "POST"])
def calendar_view():
    if request.method == "POST":
        fields = task_fields_from_form()
        if fields.get("title"):
            db.add_task(conn(), fields.pop("title"), status="next", **fields)
        return redirect(url_for("calendar_view", m=(fields.get("due_date") or "")[:7] or None))
    try:
        first = date.fromisoformat(request.args.get("m", "") + "-01")
    except ValueError:
        first = today().replace(day=1)
    last = recur.add_months(first, 1) - timedelta(days=1)
    start = first - timedelta(days=first.weekday())  # weeks start on Monday
    end = last + timedelta(days=6 - last.weekday())
    by_day = defaultdict(list)  # iso date -> [(task, is_projected_repeat)]
    for t in db.dated_tasks(conn(), start, end):
        by_day[t["due_date"]].append((t, False))
    for t in db.recurring_tasks(conn()):
        if recur.parse(t["recur"]):
            for d in recur.occurrences(t["recur"], date.fromisoformat(t["due_date"]), end):
                if d >= start:
                    by_day[d.isoformat()].append((t, True))
    weeks = [[start + timedelta(days=7 * w + i) for i in range(7)] for w in range(((end - start).days + 1) // 7)]
    return render_template("calendar.html", first=first, weeks=weeks, by_day=by_day,
                           prev=recur.add_months(first, -1).strftime("%Y-%m"),
                           next=recur.add_months(first, 1).strftime("%Y-%m"))


# --- horizons --------------------------------------------------------------

@app.route("/horizons")
def horizons_view():
    return render_template("horizons.html", levels=HORIZON_LEVELS, items=db.horizons(conn()),
                           project_count=len(db.projects(conn())))


@app.post("/contexts")
def context_create():
    name = db.context_name(request.form.get("name")) or abort(400)
    db.add_context(conn(), name)
    return redirect(url_for("horizons_view") + "#contexts")


@app.post("/contexts/delete")
def context_delete():
    name = request.form.get("name") or abort(400)
    used = db.delete_context(conn(), name)
    if used:
        flash(f"{name} is still used by {used} open task{'s' if used != 1 else ''}. Change those first.")
    return redirect(url_for("horizons_view") + "#contexts")


@app.post("/horizons")
def horizon_create():
    level, title = request.form.get("level"), form_value("title")
    if level not in ("goal", "area") or not title:
        abort(400)
    return redirect(url_for("horizon_view", id=db.add_horizon(conn(), level, title)))


@app.route("/horizons/<int:id>", methods=["GET", "POST"])
def horizon_view(id):
    c = conn()
    h = db.get_horizon(c, id) or abort(404)
    if request.method == "POST":
        db.update_horizon(c, id, title=form_value("title") or h["title"],
                          notes=(request.form.get("notes") or "").strip())
        flash("Saved.")
        return redirect(request.form.get("next") or url_for("horizon_view", id=id))
    return render_template("horizon.html", h=h, label=dict((l, lab) for l, lab, _ in HORIZON_LEVELS)[h["level"]])


@app.post("/horizons/<int:id>/delete")
def horizon_delete(id):
    db.delete_horizon(conn(), id)
    return redirect(url_for("horizons_view"))


if __name__ == "__main__":
    app.run(host=os.environ.get("GTD_HOST", "127.0.0.1"), port=int(os.environ.get("GTD_PORT", 5005)),
            debug=bool(os.environ.get("GTD_DEBUG")))
