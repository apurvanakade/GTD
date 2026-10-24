"""One-off import of active Mindwtr tasks and projects.

    uv run import_mindwtr.py                 # dry run against the live Local API
    uv run import_mindwtr.py --apply         # write into gtd.db
    uv run import_mindwtr.py --from-json tasks.json projects.json   # use a snapshot

Mindwtr itself is never modified.
"""
import argparse
import json
import os
import re
import sys
import urllib.request
from collections import Counter

import db

API = "http://127.0.0.1:3456"
SECRETS = os.path.expanduser("~/Library/Application Support/mindwtr/config/secrets.toml")
STATUSES = {"inbox", "next", "waiting", "someday"}
UNITS = {"DAILY": "day", "WEEKLY": "week", "MONTHLY": "month", "YEARLY": "year"}


def fetch(path):
    token = re.search(r"[0-9a-f]{64}", open(SECRETS).read()).group(0)
    req = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def map_recurrence(rec):
    """Mindwtr recurrence -> our rule string, or None if it can't be expressed simply."""
    if not rec:
        return None
    parts = dict(p.split("=", 1) for p in (rec.get("rrule") or "").split(";") if "=" in p)
    freq = parts.get("FREQ") or (rec.get("rule") or "").upper()
    unit = UNITS.get(freq)
    by_day = rec.get("byDay") or [d for d in parts.get("BYDAY", "").split(",") if d]
    # One weekday on a weekly rule survives because stepping by weeks keeps the weekday.
    if not unit or len(by_day) > 1 or (by_day and unit != "week") or "BYMONTHDAY" in parts:
        return None
    n = int(parts.get("INTERVAL", 1))
    return f"every {n} {unit}{'s' if n > 1 else ''}"


def plan(tasks, projects):
    """Pure mapping: returns (projects, tasks, reference_items, warnings)."""
    out_projects = [
        {"src": p["id"], "title": p["title"], "status": p["status"], "notes": p.get("supportNotes") or ""}
        for p in projects
        if p["status"] in ("active", "someday") and not p.get("deletedAt")
    ]
    live = {p["src"] for p in out_projects}
    out_tasks, reference, warnings = [], [], []
    for t in tasks:
        if t.get("deletedAt"):
            continue
        if t["status"] == "reference":
            reference.append(t)
            continue
        if t["status"] not in STATUSES:
            continue
        contexts = t.get("contexts") or []
        notes = (t.get("description") or "").strip()
        if contexts:  # this app has no contexts; keep Mindwtr's in the notes
            notes = (notes + "\n\nContexts: " + " ".join(contexts)).strip()
        title, rule = t["title"], None
        if t.get("recurrence"):
            rule = map_recurrence(t["recurrence"])
            if rule is None:
                title = "⚠ " + title
                notes = (notes + f"\n\nMindwtr repeat rule not imported: {t['recurrence'].get('rrule')}").strip()
                warnings.append(f"recurrence needs fixing by hand: {t['title']}")
        if t.get("projectId") and t["projectId"] not in live:
            warnings.append(f"project not imported, task left loose: {t['title']}")
        out_tasks.append({
            "title": title, "notes": notes, "status": t["status"],
            "project_src": t.get("projectId") if t.get("projectId") in live else None,
            "due_date": (t.get("dueDate") or "")[:10] or None,
            "recur": rule,
        })
    return out_projects, out_tasks, reference, warnings


def apply(conn, projects, tasks):
    ids = {p["src"]: db.add_project(conn, p["title"], status=p["status"], notes=p["notes"]) for p in projects}
    for t in tasks:
        t = dict(t)
        status = t.pop("status")  # only a project's task keeps it, as its step; someday becomes a later step
        db.add_task(conn, t.pop("title"), project_id=ids.get(t.pop("project_src")),
                    step=status if status in ("next", "waiting") else "later", **t)


def write_reference(items, path="reference.md"):
    with open(path, "a") as f:
        for t in items:
            f.write(f"## {t['title']}\n\n{(t.get('description') or '').strip()}\n\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--from-json", nargs=2, metavar=("TASKS", "PROJECTS"))
    args = ap.parse_args()
    if args.from_json:
        tasks, projects = (json.load(open(p)) for p in args.from_json)
    else:
        tasks, projects = fetch("/tasks"), fetch("/projects")
    tasks, projects = tasks.get("tasks", tasks), projects.get("projects", projects)

    out_projects, out_tasks, reference, warnings = plan(tasks, projects)
    print("Projects:", dict(Counter(p["status"] for p in out_projects)))
    print("Tasks:   ", dict(Counter(t["status"] for t in out_tasks)),
          f"({sum(bool(t['recur']) for t in out_tasks)} repeating)")
    print("Reference items -> reference.md:", len(reference))
    for w in warnings:
        print("  !", w)

    if not args.apply:
        print("\nDry run. Re-run with --apply to write", db.DB_PATH)
        return
    conn = db.connect()
    if conn.execute("SELECT count(*) FROM tasks").fetchone()[0]:
        sys.exit(f"{db.DB_PATH} already has tasks; refusing to import twice.")
    apply(conn, out_projects, out_tasks)
    write_reference(reference)
    print("Imported into", db.DB_PATH)


if __name__ == "__main__":
    main()
