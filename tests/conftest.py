import sys
from datetime import date
from pathlib import Path

import jinja2
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db  # noqa: E402
from app import app  # noqa: E402

TODAY = date(2026, 10, 22)


@pytest.fixture
def client(tmp_path):
    app.config.update(TESTING=True, DB_PATH=str(tmp_path / "t.db"), TODAY=TODAY)
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
    for name in ("@home", "@calls", "@spare"):
        db.add_context(c, name)
    active = db.add_project(c, "Kitchen", notes="tiles\nand grout")
    stalled = db.add_project(c, "Stalled")
    waiting = db.add_project(c, "Waiting proj", status="waiting")
    someday = db.add_project(c, "Someday proj", status="someday")
    done = db.add_project(c, "Done proj", status="done")
    db.add_task(c, "inbox item", notes="from email")
    db.add_task(c, "buy tiles", status="next", context="@home", project_id=active, due_date="2026-10-01")
    db.add_task(c, "lay tiles", status="later", project_id=active)
    db.add_task(c, "quote from Bob", status="waiting", waiting_on="Bob", project_id=active, due_date="2026-10-22")
    db.add_task(c, "vacuum", status="next", context="@home", ground=1, recur="weekly", due_date="2026-10-22")
    db.add_task(c, "bins", status="next", context="@home", ground=1, recur="every 2 weeks", due_date="2026-11-03")
    db.add_task(c, "call mum", status="next", context="@calls", ground=1)
    db.add_task(c, "parked step", status="next", context="@calls", project_id=waiting)
    db.add_task(c, "dream step", status="later", project_id=someday)
    db.add_task(c, "legacy orphan", status="next")
    finished = db.add_task(c, "old tiles", status="next", project_id=done)
    db.complete_task(c, finished, TODAY)
    db.add_horizon(c, "goal", "Ship the kitchen")
    area = db.add_horizon(c, "area", "Home")
    db.update_horizon(c, area, notes="Clean and calm\nsecond line")
    return {"active": active, "stalled": stalled, "waiting": waiting, "someday": someday, "done": done}
