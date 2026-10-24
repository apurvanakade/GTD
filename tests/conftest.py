import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import jinja2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from app import app  # noqa: E402

TODAY = date(2026, 10, 22)
NOW = datetime(2026, 10, 22, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
def client(tmp_path):
    app.config.update(TESTING=True, DB_PATH=str(tmp_path / "t.db"), TODAY=TODAY, NOW=NOW)
    app.jinja_env.undefined = jinja2.StrictUndefined  # a typo in a template fails instead of rendering blank
    with app.test_client() as c:
        yield c


@pytest.fixture
def conn(client):
    c = db.connect(app.config["DB_PATH"])
    yield c
    c.close()


def seed(c):
    """One of everything the UI can show, so pages render every branch. Deterministic ids."""
    active = db.add_project(c, "Kitchen", notes="tiles\nand grout")
    stalled = db.add_project(c, "Stalled")
    waiting = db.add_project(c, "Waiting proj", status="waiting")
    someday = db.add_project(c, "Someday proj", status="someday")
    done = db.add_project(c, "Done proj", status="done")
    db.add_task(c, "inbox item", notes="from email")
    db.add_task(c, "buy tiles", step="next", project_id=active, due_date="2026-10-01")
    db.add_task(c, "lay tiles", step="later", project_id=active)
    db.add_task(c, "quote from Bob", step="waiting", project_id=active, due_date="2026-10-22")
    db.add_task(c, "vacuum", step="next", ground=1, recur="weekly", due_date="2026-10-22")
    db.add_task(c, "bins", step="next", ground=1, recur="every 2 weeks", due_date="2026-11-03")
    db.add_task(c, "call mum", step="next", ground=1)
    db.add_task(c, "parked step", step="next", project_id=waiting)
    db.add_task(c, "dream step", step="later", project_id=someday)
    db.add_task(c, "legacy orphan", step="next")
    finished = db.add_task(c, "old tiles", step="next", project_id=done)
    db.complete_task(c, finished, TODAY)
    db.abandon_task(c, db.add_task(c, "dropped idea", step="later", project_id=active))
    db.add_horizon(c, "goal", "Ship the kitchen")
    area = db.add_horizon(c, "area", "Home")
    db.update_horizon(c, area, notes="Clean and calm\nsecond line")
    db.add_reference(c, "Plumber", notes="Ask for Sam\nhttps://example.com/plumber")
    db.timer_start(c, 2, NOW - timedelta(hours=2))  # "buy tiles"
    db.timer_stop(c, NOW - timedelta(hours=1))
    db.timer_start(c, 7, NOW - timedelta(minutes=10))  # "call mum", still running
    db.complete_review(c, TODAY - timedelta(days=7))
    db.toggle_review_step(c, "sweep")  # this week's review under way
    return {"active": active, "stalled": stalled, "waiting": waiting, "someday": someday, "done": done}
