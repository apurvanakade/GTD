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


def test_finishing_a_projects_next_action_stays_on_the_page(page, live):
    page.goto(live + "/next")
    row(page, "buy tiles").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.url == live + "/next"


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


def test_slash_on_inbox_focuses_its_add_box(page, live):
    page.goto(live + "/inbox")
    page.keyboard.press("/")
    assert page.evaluate("document.activeElement.closest('main form.add') !== null")
    assert not page.locator(".capture-dialog").evaluate("d => d.open")


def test_task_rows_are_one_height(page, live):
    """A Make next or Activate button mustn't make its row taller than the others."""
    heights = []
    for url in ("/projects/1", "/someday"):
        page.goto(live + url)
        heights += page.locator("ul.tasks > li.task").evaluate_all("els => els.map(e => e.getBoundingClientRect().height)")
    assert len(heights) >= 3 and max(heights) - min(heights) < 1, heights


def test_project_row_arrow_folds_out_its_steps(page, live):
    for url, project, step in (("/projects", "Kitchen", "lay tiles"), ("/someday", "Someday proj", "dream step")):
        page.goto(live + url)
        r = row(page, project)
        link = r.get_by_role("link", name=step, exact=True)
        assert not link.is_visible()
        r.locator("button.peek").click()
        assert link.is_visible() and r.locator("button.peek").get_attribute("aria-expanded") == "true"
        link.click()
        page.wait_for_url("**/tasks/*")
        assert page.locator("input.big").input_value() == step


def test_expand_all_folds_out_every_project(page, live):
    for url in ("/projects", "/someday"):
        page.goto(live + url)
        steps, toggle = page.locator("ul.steps"), page.locator("button.peek-all")
        assert toggle.inner_text() == "Expand all" and not any(s.is_visible() for s in steps.all())
        toggle.click()
        assert toggle.inner_text() == "Collapse all" and all(s.is_visible() for s in steps.all())
        assert set(page.locator("button.peek").all_inner_texts()) == {"▾"}
        toggle.click()
        assert not any(s.is_visible() for s in steps.all())
        assert set(page.locator("button.peek").all_inner_texts()) == {"▸"}


def test_search_suggests_as_you_type_and_opens_the_pick(page, live):
    page.goto(live + "/next")
    page.fill(".search input", "lay tls")
    page.wait_for_selector(".suggest a")
    assert page.locator(".suggest a").first.inner_text().startswith("lay tiles")
    page.locator(".suggest a").first.click()
    page.wait_for_url("**/tasks/3")


def test_search_suggestions_keyboard(page, live):
    page.goto(live + "/next")
    page.keyboard.press("f")
    page.keyboard.type("tiles")
    page.wait_for_selector(".suggest a")
    page.keyboard.press("ArrowDown")
    second = page.locator(".suggest a").nth(1).get_attribute("href")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert page.url == live + second
    page.goto(live + "/next")
    page.fill(".search input", "tiles")
    page.wait_for_selector(".suggest a")
    page.keyboard.press("Escape")
    assert page.locator(".suggest").is_hidden()


def test_add_line_reveals_its_extra_fields_on_focus(page, live):
    page.goto(live + "/projects/1")
    more = page.locator("main form.add .add-more")
    assert more.is_hidden()
    page.locator("main form.add input[name=title]").focus()
    assert more.is_visible()


def test_new_option_reveals_the_name_box(page, live):
    page.goto(live + "/tasks/2")
    box = page.locator("input[name=new_project]")
    assert box.is_hidden()
    page.select_option("select[name=project_id]", "__new__")
    assert box.is_visible()
    page.fill("input[name=new_project]", "Garage")
    page.get_by_role("button", name="Save", exact=True).click()
    assert page.locator("select[name=project_id] option:checked").inner_text() == "Garage"


def test_clicking_a_calendar_day_sets_the_add_date(page, live):
    page.goto(live + "/calendar")
    page.locator('.cal .day[data-date="2026-10-29"]').click()
    title = page.locator("main form.add input[name=title]")
    assert page.evaluate("document.activeElement.name") == "title"
    assert "29 Oct" in title.get_attribute("placeholder")
    assert page.locator("main form.add input[name=due_date]").input_value() == "2026-10-29"


def test_row_dates_line_up_with_or_without_a_repeat(page, live, conn):
    db.add_task(conn, "dentist", status="next", ground=1, due_date="2026-10-30")
    page.goto(live + "/h0")
    edges = {t: row(page, t).locator(".date").evaluate("e => Math.round(e.getBoundingClientRect().right)")
             for t in ("vacuum", "dentist")}
    assert edges["vacuum"] == edges["dentist"], edges
