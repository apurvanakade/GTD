"""Drive the real JS in real Chromium. Slow, so excluded from the default run: `uv run pytest -m browser`.

Run these when a change touches templates/, static/ or the JS in base.html. The Stop hook and pre-commit
hook do that automatically. Everything else in the suite checks only the HTML contract with the JS.
"""
import threading

import pytest
from werkzeug.serving import make_server

import db
from app import app
from conftest import NOW, seed

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


@pytest.fixture
def live(client, conn):
    """The seeded app on a real port, fresh DB per test."""
    seed(conn)
    server = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest.fixture
def page(browser, live):
    pg = browser.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
    yield pg
    pg.close()
    assert pg.errors == []


def row(page, title):
    return page.locator("li.task", has=page.get_by_role("link", name=title, exact=True))


def test_every_nav_page_loads_without_js_errors(page, live):
    page.goto(live + "/")
    for href in page.eval_on_selector_all("nav a", "els => els.map(e => e.getAttribute('href'))"):
        assert page.goto(live + href).ok, href


def test_tick_off_fades_the_row_in_place(page, live, conn):
    page.goto(live + "/horizons")
    row(page, "vacuum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.url == live + "/horizons"
    assert row(page, "vacuum").get_attribute("class").split().count("gone") == 1
    assert db.get_task(conn, 5)["status"] == "done"


def test_finishing_a_projects_next_action_goes_to_the_project(page, live):
    page.goto(live + "/next")
    row(page, "buy tiles").locator("form.done button").click()
    page.wait_for_url("**/projects/*")
    assert page.get_by_role("button", name="Make next").count() >= 1


def test_plus_inbox_dialog_captures(page, live, conn):
    page.goto(live + "/next")
    page.click(".add-inbox")
    assert page.locator(".capture-dialog").evaluate("d => d.open")
    page.fill(".capture-dialog input[name=title]", "from the dialog")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert any(t["title"] == "from the dialog" for t in db.tasks_with_status(conn, "inbox"))


def test_slash_opens_the_capture_dialog_and_f_focuses_search(page, live):
    page.goto(live + "/next")
    page.keyboard.press("f")
    assert page.evaluate("document.activeElement.name") == "q"
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    assert page.locator(".capture-dialog").evaluate("d => d.open")


def test_calendar_button_copies_the_picked_date_into_the_text_box(page, live, conn):
    page.goto(live + "/tasks/2")
    page.fill("input[name=due_date]", "")
    page.click(".date-field .cal")  # showPicker() needs this real click's user activation
    page.locator(".date-field .picker").evaluate(
        "el => { el.value = '2026-11-05'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.input_value("input[name=due_date]") == "2026-11-05"
    with page.expect_navigation():
        page.get_by_role("button", name="Save", exact=True).click()
    assert db.get_task(conn, 2)["due_date"] == "2026-11-05"


def test_running_timer_clock_ticks(page, live):
    page.clock.install(time=NOW)
    page.goto(live + "/next")
    before = page.inner_text(".running-timer .clock")
    page.clock.run_for(3000)
    assert page.inner_text(".running-timer .clock") != before


def test_theme_toggle_overrides_system_and_persists(page, live):
    page.emulate_media(color_scheme="light")
    page.goto(live + "/next")
    bg = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")
    light = bg()
    page.locator(".theme-toggle").click()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    assert bg() != light
    page.reload()
    assert page.evaluate("document.documentElement.dataset.theme") == "dark"
    page.locator(".theme-toggle").click()
    assert bg() == light


def test_drag_reorders_project_steps(page, live, conn):
    page.goto(live + "/projects/1")
    titles = lambda: page.locator("ul.tasks li.task a.title").all_inner_texts()
    assert titles() == ["buy tiles", "lay tiles", "quote from Bob"]
    with page.expect_response(lambda r: r.url.endswith("/order")) as saved:
        row(page, "lay tiles").locator(".grip").drag_to(row(page, "buy tiles"), target_position={"x": 5, "y": 2})
    assert saved.value.status == 204
    assert titles() == ["lay tiles", "buy tiles", "quote from Bob"]
    assert [t["title"] for t in db.project_tasks(conn, 1)] == ["lay tiles", "buy tiles", "quote from Bob"]
    page.reload()
    assert titles() == ["lay tiles", "buy tiles", "quote from Bob"]
