"""Drive the real JS in real Chromium (and WebKit for the few Safari-only behaviors). Slow, so excluded from the default run: `uv run pytest -m browser`.

Run these when a change touches templates/, static/ or static/app.js. The Stop hook and pre-commit
hook do that automatically. Everything else in the suite checks only the HTML contract with the JS.
"""
import re
import threading
from datetime import datetime, timedelta

import pytest
from playwright.sync_api import expect
from werkzeug.serving import make_server

import db
from app import app, local_input
from conftest import NOW, seed

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="session")
def playwright():
    with sync_api.sync_playwright() as p:
        yield p


@pytest.fixture(scope="session")
def browser(playwright):
    b = playwright.chromium.launch()
    yield b
    b.close()


@pytest.fixture(scope="session")
def webkit(playwright):
    """Safari's engine, for the few behaviors where it differs from Chromium (the iPad uses it)."""
    b = playwright.webkit.launch()
    yield b
    b.close()


@pytest.fixture(scope="session")
def server():
    """One server for the run: shutting one down waits out serve_forever's 0.5 s poll."""
    s = make_server("127.0.0.1", 0, app, threaded=True)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{s.server_port}"
    s.shutdown()


@pytest.fixture
def live(server, client, conn):
    """The seeded app on a real port, fresh DB per test (the app reads DB_PATH on every request)."""
    seed(conn)
    return server


@pytest.fixture
def page(browser, live):
    pg = browser.new_page()
    pg.errors = []
    pg.on("pageerror", lambda e: pg.errors.append(str(e)))
    pg.on("console", lambda m: m.type == "error" and pg.errors.append(m.text))
    yield pg
    pg.close()
    assert pg.errors == []


@pytest.fixture
def webkit_page(webkit, live):
    pg = webkit.new_page()
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
    page.goto(live + "/horizons?level=ground")
    row(page, "vacuum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.url == live + "/horizons?level=ground"
    assert row(page, "vacuum").get_attribute("class").split().count("gone") == 1
    assert db.get_task(conn, 5)["state"] == "done"


def test_ticking_off_the_timed_task_drops_the_timer_pill(page, live):
    page.goto(live + "/horizons?level=ground")
    assert page.locator(".running-timer").count() == 1  # "call mum" is being timed
    row(page, "call mum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.locator(".running-timer").count() == 0


def test_a_ticked_row_opens_its_task_and_cant_be_ticked_again(page, live):
    page.goto(live + "/horizons?level=ground")
    row(page, "vacuum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    gone = row(page, "vacuum")
    assert gone.locator("form.done").count() == 0 and gone.locator(".check").inner_text() == "✓"
    gone.locator(".meta").click()
    assert task_overlay(page) == "vacuum"
    page.keyboard.press("Escape")
    gone.get_by_role("button", name="Revive").click()  # like any done row, it can be revived
    page.wait_for_selector("li.task:has-text('vacuum') form.done")
    assert page.url == live + "/horizons?level=ground"


def test_finishing_a_projects_next_action_stays_on_the_page(page, live, conn):
    db.add_task(conn, "order grout", step="next", project_id=1)  # undated: on Next
    page.goto(live + "/next")
    row(page, "order grout").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.url == live + "/next"


def test_done_then_project_opens_the_project_to_pick_next(page, live, conn):
    page.goto(live + "/tasks/2?embed=overlay")
    page.get_by_role("button", name="Done and open project").click()
    page.wait_for_url("**/projects/active?project=1")  # Projects, with its overlay
    frame = page.frame_locator("dialog.overlay iframe")
    assert frame.get_by_role("button", name="Make next").count() >= 1  # "lay tiles" can take over
    assert db.get_task(conn, 2)["state"] == "done"


def test_plus_inbox_dialog_captures(page, live, conn):
    page.goto(live + "/projects/active")
    page.click(".add-inbox")
    assert page.locator(".capture-dialog").evaluate("d => d.open")
    page.fill(".capture-dialog input[name=title]", "from the dialog")
    page.evaluate("window.mark = 1")
    page.keyboard.press("Enter")  # added in the background: the page stays
    page.wait_for_selector(".flashes >> text=Added to inbox: from the dialog")
    box = page.locator(".flash").bounding_box()
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)  # pointed at, it goes
    page.wait_for_selector(".flashes", state="detached")
    assert not page.locator(".capture-dialog").evaluate("d => d.open") and page.evaluate("window.mark") == 1
    assert any(t["title"] == "from the dialog" for t in db.inbox_tasks(conn))
    page.click(".add-inbox")
    page.fill(".capture-dialog input[name=title]", "meta enter in the dialog")
    page.locator(".capture-dialog .md-editor").click()
    page.keyboard.press("ControlOrMeta+Enter")  # in the notes it makes a list item instead
    assert page.locator(".capture-dialog textarea[name=notes]").input_value() == "- "
    page.keyboard.press("ControlOrMeta+z")
    page.locator(".capture-dialog input[name=title]").press("ControlOrMeta+Enter")
    page.wait_for_selector(".flashes >> text=Added to inbox: meta enter in the dialog")
    # Added with notes, the next task's notes start empty, and ⌘Z can't bring the last one's back.
    page.click(".add-inbox")
    page.fill(".capture-dialog input[name=title]", "with notes")
    page.locator(".capture-dialog .md-editor").click()
    page.keyboard.type("first")
    page.wait_for_timeout(1100)  # a pause: two undo steps
    page.keyboard.type(" notes")
    page.locator(".capture-dialog input[name=title]").press("ControlOrMeta+Enter")
    page.wait_for_selector(".flashes >> text=Added to inbox: with notes")
    page.click(".add-inbox")
    notes = page.locator(".capture-dialog .md-editor")
    notes.click()
    page.keyboard.press("ControlOrMeta+z")
    assert notes.text_content() == "" and page.locator(".capture-dialog textarea[name=notes]").input_value() == ""
    # Cancel adds nothing, and its category isn't offered later.
    cat = page.locator(".capture-dialog input[name=category]")
    cat.fill("Zebra")
    page.locator(".capture-dialog").get_by_role("button", name="Cancel").click()
    assert not page.locator(".capture-dialog").evaluate("d => d.open")
    page.click(".add-inbox")
    cat.fill("")
    cat.type("zeb")
    page.wait_for_timeout(100)
    assert page.locator(".ctx-suggest:not([hidden]) a").count() == 0
    page.keyboard.press("Escape")
    page.click(".add-inbox")
    page.mouse.click(5, 5)  # beside it, on the backdrop: closed
    assert not page.locator(".capture-dialog").evaluate("d => d.open")
    # ⌘/ captures while typing, in an overlay's notes too; the overlay stays open under it.
    page.goto(live + "/next")
    row(page, "measure wall").locator(".title").click()
    task_overlay(page)
    page.frame_locator("dialog.overlay iframe").locator(".md-editor").click()
    page.keyboard.press("Meta+/")
    page.wait_for_function("document.querySelector('.capture-dialog').open")
    page.keyboard.press("Tab")  # from the category box to the title
    page.keyboard.type("while typing")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('.capture-dialog').open")
    assert page.locator("dialog.overlay").evaluate("d => d.open")
    assert any(t["title"] == "while typing" for t in db.inbox_tasks(conn))
    page.mouse.click(5, 5)  # beside it, on the backdrop: closed
    page.wait_for_selector("dialog.overlay", state="detached")
    # The bookmarklet opens a popup; "Bookmark link" with no title files the page under its own title, and it closes.
    page.goto(live + "/someday")
    code = page.locator(".bookmarklet a").get_attribute("href").removeprefix("javascript:")
    with page.expect_popup() as info:
        page.evaluate(code)
    popup = info.value
    assert popup.is_checked("input[name=link]") and popup.locator(".md-editor").count() == 1
    assert popup.get_attribute("input[name=title]", "placeholder") == "Someday / maybe"
    with popup.expect_event("close"):  # listening first: it can close before a later wait starts
        popup.press("input[name=title]", "Enter")
    t = db.inbox_tasks(conn)[0]
    assert (t["title"], t["notes"]) == ("Someday / maybe", f"[Someday / maybe]({live}/someday)")


def test_the_bookmarklet_popup_stays_open_to_show_a_warning(page, live, conn):
    page.goto(live + "/capture?url=https://ex.com/&title=Ex")  # a plain tab can't close itself, so open it as a popup
    popup_opened = page.evaluate("window.open(location.href, 'p', 'popup') && true")
    popup = page.context.pages[-1]
    popup.wait_for_load_state()
    assert popup_opened
    popup.fill("input[name=title]", "read it !sometime next spring")
    popup.press("input[name=title]", "Enter")
    popup.locator(".flash", has_text="understand reminder").wait_for()
    assert not popup.is_closed()
    assert popup.input_value("input[name=title]") == ""  # it was added: Enter again wouldn't add it twice
    assert db.inbox_tasks(conn)[0]["title"] == "read it !sometime next spring"
    popup.close()


def test_slash_opens_the_capture_dialog_and_f_focuses_search(page, live):
    page.goto(live + "/projects/active")
    page.keyboard.press("f")
    assert page.evaluate("document.activeElement.name") == "q"
    page.locator("body").click(position={"x": 5, "y": 5})
    page.keyboard.press("/")
    assert page.locator(".capture-dialog").evaluate("d => d.open")


def test_on_a_touch_screen_the_calendar_button_is_the_native_picker(browser, live, conn):
    """iPad Safari doesn't reliably open showPicker(): there a tap lands on the native date input itself."""
    ctx = browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 1024, "height": 768})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(live + "/tasks/2?embed=overlay")
    assert page.evaluate("matchMedia('(pointer: coarse)').matches")
    page.fill("input[name=due_date]", "2026-11-03")
    field = page.locator(".date-field", has=page.locator("input[name=due_date]"))
    box = field.locator(".pick").bounding_box()
    x, y = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    assert page.evaluate(f"document.elementFromPoint({x}, {y}).className") == "picker"
    page.touchscreen.tap(x, y)
    assert field.locator(".picker").input_value() == "2026-11-03"  # starts on the box's date
    field.locator(".picker").evaluate(
        "el => { el.value = '2026-11-05'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.input_value("input[name=due_date]") == "2026-11-05"
    ctx.close()
    assert errors == []


def test_send_to_inbox_on_a_row_needs_hover(browser, page, live, conn):
    """A row's ↩ shows on hover; with no hover (a phone, an iPad) it would be an invisible tap target beside ▶ that
    clears the task's project and dates, so it isn't there at all (the task page's ↩ still is)."""
    page.goto(live + "/next")
    row = page.locator("li.task", has=page.locator("form.to-inbox")).first
    assert row.locator(".to-inbox button").evaluate("b => getComputedStyle(b).opacity") == "0"
    row.hover()
    sync_api.expect(row.locator(".to-inbox button")).to_have_css("opacity", "1")  # it eases in ("Hover motion")
    ctx = browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 1024, "height": 768})
    touch = ctx.new_page()
    touch.goto(live + "/next")
    assert touch.evaluate("matchMedia('(hover: none)').matches")
    assert touch.locator("form.to-inbox").count() > 0
    assert touch.eval_on_selector_all(".task .to-inbox", "els => els.every(e => !e.offsetParent)")
    touch.goto(live + "/tasks/2?embed=overlay")
    sync_api.expect(touch.get_by_role("button", name="Send to inbox")).to_be_visible()
    ctx.close()


def test_calendar_button_copies_the_picked_date_into_the_text_box(page, live, conn):
    page.goto(live + "/tasks/2?embed=overlay")
    page.fill("input[name=due_date]", "")
    field = page.locator(".date-field", has=page.locator("input[name=due_date]"))
    field.locator(".pick").click()  # showPicker() needs this real click's user activation
    field.locator(".picker").evaluate(
        "el => { el.value = '2026-11-05'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.input_value("input[name=due_date]") == "2026-11-05"
    sync_api.expect(page.locator(".when[data-parse=due]")).to_have_text("→ Thu Nov 5")
    for _ in range(50):
        if db.get_task(conn, 2)["due_date"] == "2026-11-05":
            break
        page.wait_for_timeout(50)
    assert db.get_task(conn, 2)["due_date"] == "2026-11-05"


def test_a_typed_date_saves_when_the_picker_is_closed_without_a_pick(page, live, conn):
    page.goto(live + "/tasks/2?embed=overlay")
    page.fill("input[name=due_date]", "fri")
    field = page.locator(".date-field", has=page.locator("input[name=due_date]"))
    field.locator(".pick").click()  # the box keeps its focus
    page.keyboard.press("Escape")  # no pick
    page.locator("main .md-editor").click()  # leaving the box saves it
    for _ in range(50):
        if db.get_task(conn, 2)["due_date"] == "2026-10-23":
            break
        page.wait_for_timeout(50)
    assert db.get_task(conn, 2)["due_date"] == "2026-10-23"


def test_running_timer_clock_ticks(page, live):
    page.clock.install(time=NOW)
    page.goto(live + "/next")
    before = page.inner_text(".running-timer .clock")
    page.clock.run_for(3000)
    assert page.inner_text(".running-timer .clock") != before


def test_task_page_timer_is_by_the_title_and_ticks(page, live):
    page.clock.install(time=NOW)
    page.goto(live + "/tasks/7?embed=overlay")  # "call mum", running
    timer = page.locator(".title-line .task-timer")
    stop, title = timer.get_by_role("button", name="Stop the timer"), page.locator("input.big").bounding_box()
    box = stop.bounding_box()
    assert stop.is_visible() and title["y"] <= box["y"] + box["height"] / 2 <= title["y"] + title["height"]  # same line
    before = timer.locator(".clock").inner_text()
    page.clock.run_for(3000)
    assert timer.locator(".clock").inner_text() != before
    page.goto(live + "/tasks/2?embed=overlay")  # "buy tiles", not running
    with page.expect_navigation():
        page.locator(".title-line .task-timer").get_by_role("button", name="Start the timer").click()
    assert page.locator(".title-line .task-timer").get_by_role("button", name="Stop the timer").is_visible()


def test_running_timer_stays_in_view_and_stops_from_anywhere(page, live, conn):
    page.set_viewport_size({"width": 900, "height": 400})
    page.goto(live + "/done")
    page.mouse.wheel(0, 10000)
    page.wait_for_function("window.scrollY > 0")
    assert page.locator(".running-timer").is_visible()
    box = page.locator(".running-timer").bounding_box()
    assert 0 <= box["y"] and box["y"] + box["height"] <= 400
    with page.expect_navigation():
        page.click(".running-timer button")
    assert db.timer_running(conn) is None
    assert page.url.endswith("/done")
    stopped = page.locator(".running-timer.stopped")  # stays, to finish the task from it
    assert stopped.is_visible() and stopped.get_by_role("link").inner_text() == "call mum"
    shown = stopped.locator(".clock").inner_text()
    page.wait_for_timeout(1100)
    assert stopped.locator(".clock").inner_text() == shown  # frozen
    with page.expect_navigation():
        stopped.get_by_role("button", name="Mark done").click()
    assert db.get_task(conn, 7)["state"] == "done" and page.url.endswith("/done")
    assert page.locator(".running-timer").count() == 0


def test_timer_entry_saves_when_focus_leaves_the_row(page, live, conn):
    page.goto(live + "/timer")
    entry = page.locator("form.entry", has=page.get_by_role("link", name="buy tiles", exact=True))
    new_start = local_input(db.iso(NOW - timedelta(hours=3)))
    entry.locator("input[name=start]").fill(new_start)
    entry.locator("input[name=end]").focus()  # moving within the row doesn't save yet
    page.wait_for_timeout(200)
    assert local_input(db.get_entry(conn, 1)["start_time"]) != new_start
    with page.expect_navigation():
        page.locator("h1").click()
    assert local_input(db.get_entry(conn, 1)["start_time"]) == new_start
    assert page.url.endswith("/timer")


def test_timer_entry_enter_saves_and_unchanged_blur_does_not(page, live, conn):
    page.goto(live + "/timer")
    entry = page.locator("form.entry", has=page.get_by_role("link", name="call mum", exact=True))
    entry.locator("input[name=start]").focus()
    page.locator("h1").click()
    page.wait_for_timeout(200)
    assert db.get_entry(conn, 2)["end_time"] is None
    entry.locator("input[name=end]").fill(local_input(db.iso(NOW - timedelta(minutes=5))))
    with page.expect_navigation():
        entry.locator("input[name=end]").press("Enter")
    assert db.get_entry(conn, 2)["end_time"] is not None  # stopped, not restarted by ▶
    assert db.timer_running(conn) is None


def test_timer_entry_end_before_start_is_marked_not_saved(page, live, conn):
    page.goto(live + "/timer")
    entry = page.locator("form.entry", has=page.get_by_role("link", name="buy tiles", exact=True))
    before = db.get_entry(conn, 1)["end_time"]
    entry.locator("input[name=end]").fill(local_input(db.iso(NOW - timedelta(hours=5))))
    page.locator("h1").click()
    page.wait_for_timeout(200)
    assert "bad" in entry.get_attribute("class")
    assert db.get_entry(conn, 1)["end_time"] == before
    entry.locator("input[name=end]").fill(local_input(db.iso(NOW - timedelta(minutes=30))))
    with page.expect_navigation():
        page.locator("h1").click()
    assert db.get_entry(conn, 1)["end_time"] != before


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
    page.evaluate("refreshNav()")  # the toggle sits in the nav, which a change made in place replaces
    page.locator("nav .theme-toggle").click()
    assert bg() == light


def test_drag_reorders_project_steps(page, live, conn):
    db.add_task(conn, "order grout", step="next", project_id=1)  # a second next action
    page.goto(live + "/projects/1?embed=overlay")
    titles = lambda: page.locator("ul[data-reorder] li.task a.title").all_inner_texts()
    drag = lambda a, b: row(page, a).locator(".grip").drag_to(row(page, b), target_position={"x": 5, "y": 2})
    assert titles() == ["order grout", "buy tiles", "quote from Bob", "lay tiles"]
    with page.expect_response(lambda r: r.url.endswith("/order")) as saved:
        drag("lay tiles", "quote from Bob")
    assert saved.value.status == 204
    order = ["order grout", "buy tiles", "lay tiles", "quote from Bob"]
    assert titles() == order and [t["title"] for t in db.project_tasks(conn, 1)] == order
    page.reload()
    assert titles() == order
    # The next actions reorder among themselves, on top.
    with page.expect_response(lambda r: r.url.endswith("/order")):
        drag("buy tiles", "order grout")
    order = ["buy tiles", "order grout", "lay tiles", "quote from Bob"]
    assert titles() == order and [t["title"] for t in db.project_tasks(conn, 1)] == order
    # Nothing goes above them, and they don't go below the other steps.
    drag("quote from Bob", "buy tiles")
    drag("order grout", "quote from Bob")
    assert titles() == order and [t["title"] for t in db.project_tasks(conn, 1)] == order


def test_drag_reorders_a_task_list(page, live, conn):
    """Inbox (Next, Waiting and Someday share the code): ⠿ reorders; shift-selected rows go as a block. The seed's
    tickled someday task ("sail") is in the same list, on top."""
    db.add_task(conn, "third")
    page.goto(live + "/inbox")
    titles = lambda: page.locator("ul[data-reorder] > li.task .title").all_inner_texts()
    assert titles() == ["sail", "third", "legacy orphan", "inbox item"]
    with page.expect_response(lambda r: r.url.endswith("/tasks/order")) as saved:
        row(page, "inbox item").locator(".grip").drag_to(row(page, "third"), target_position={"x": 40, "y": 2})
    assert saved.value.status == 204
    assert titles() == ["sail", "inbox item", "third", "legacy orphan"]
    assert [t["title"] for t in db.inbox_tasks(conn)] == ["inbox item", "third", "legacy orphan"]
    row(page, "inbox item").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    row(page, "third").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    with page.expect_response(lambda r: r.url.endswith("/tasks/order")):
        row(page, "third").locator(".grip").drag_to(row(page, "legacy orphan"), target_position={"x": 40, "y": 20})
    assert [t["title"] for t in db.inbox_tasks(conn)] == ["legacy orphan", "inbox item", "third"]


def test_drag_reorders_someday_projects(page, live, conn):
    db.add_project(conn, "Another dream", status="someday")  # every project tab shares the code; this covers them
    page.goto(live + "/projects/someday?tickled=1")
    page.locator("button.peek-all").click()  # open steps mustn't confuse the drag
    titles = lambda: page.locator("ul[data-reorder] > li.task > a.title").all_inner_texts()  # not the someday items
    assert titles() == ["Another dream", "Someday proj"]
    with page.expect_response(lambda r: r.url.endswith("/order/someday")) as saved:
        row(page, "Someday proj").locator(":scope > .grip").drag_to(row(page, "Another dream"), target_position={"x": 40, "y": 4})
    assert saved.value.status == 204
    assert [p["title"] for p in db.projects(conn, "someday")] == ["Someday proj", "Another dream"]
    page.reload()
    assert titles() == ["Someday proj", "Another dream"]


def test_slash_on_a_task_list_focuses_its_add_box(page, live):
    for path in ("/inbox", "/next", "/waiting", "/someday"):
        page.goto(live + path)
        page.keyboard.press("/")
        assert page.evaluate("document.activeElement.closest('main form.add') !== null"), path
        assert not page.locator(".capture-dialog").evaluate("d => d.open"), path


def test_project_row_arrow_folds_out_its_steps(page, live):
    for url, project, step in (("/projects/active", "Kitchen", "lay tiles"), ("/projects/someday?tickled=1", "Someday proj", "dream step")):
        page.goto(live + url)
        r = row(page, project)
        link = r.get_by_role("link", name=step, exact=True)
        assert not link.is_visible()
        r.locator("button.peek").click()
        expect(link).to_be_visible()  # once it has started sliding open
        assert r.locator("button.peek").get_attribute("aria-expanded") == "true"
        link.click()
        assert task_overlay(page) == step
        page.keyboard.press("Escape")
        expect(page.locator("dialog.overlay")).to_have_count(0)
        # A click beside the link, anywhere on the step's row, opens it too.
        li = r.locator(".steps > li", has=page.get_by_role("link", name=step, exact=True))
        box = li.bounding_box()
        page.mouse.click(box["x"] + box["width"] - 4, box["y"] + box["height"] / 2)
        assert task_overlay(page) == step
        page.keyboard.press("Escape")


def test_closing_a_changed_step_keeps_the_folded_out_steps(page, live, conn):
    """The reload that shows the change goes back to the same view: what was folded out stays, for that reload only."""
    page.goto(live + "/projects/active")
    peeks = page.locator("button.peek")
    row(page, "Kitchen").locator("button.peek").click()
    row(page, "Kitchen").get_by_role("link", name="lay tiles", exact=True).click()
    assert task_overlay(page) == "lay tiles"
    page.frame_locator("dialog.overlay iframe").locator("input.big").fill("lay the tiles")
    with page.expect_navigation():
        page.locator("dialog.overlay .close").click()
    assert db.get_task(conn, 3)["title"] == "lay the tiles"
    assert row(page, "Kitchen").locator("button.peek").get_attribute("aria-expanded") == "true"
    assert row(page, "Kitchen").get_by_role("link", name="lay the tiles", exact=True).is_visible()
    assert [b.get_attribute("aria-expanded") for b in peeks.all()].count("true") == 1
    # Expand all, then a change: every row stays open, and the header still offers Collapse all.
    page.locator("button.peek-all").click()
    page.locator("ul.steps a", has_text="lay the tiles").click()
    task_overlay(page)
    page.frame_locator("dialog.overlay iframe").locator("input.big").fill("lay tiles")
    with page.expect_navigation():
        page.keyboard.press("Escape")
    assert set(peeks.all_inner_texts()) == {"▾"} and page.locator("button.peek-all").inner_text() == "Collapse all"
    page.reload()  # any other reload starts folded, as before
    assert set(peeks.all_inner_texts()) == {"▸"}


def test_expand_all_folds_out_every_project(page, live):
    for url in ("/projects/active", "/projects/someday?tickled=1"):
        page.goto(live + url)
        steps, toggle = page.locator("ul.steps"), page.locator("button.peek-all")
        assert toggle.inner_text() == "Expand all" and not any(s.is_visible() for s in steps.all())
        toggle.click()
        assert toggle.inner_text() == "Collapse all"
        for s in steps.all():
            expect(s).to_be_visible()  # once it has started sliding open
        assert set(page.locator("button.peek").all_inner_texts()) == {"▾"}
        toggle.click()
        for s in steps.all():
            expect(s).to_be_hidden()  # once the fold has slid shut
        assert set(page.locator("button.peek").all_inner_texts()) == {"▸"}


def test_search_suggests_as_you_type_and_opens_the_pick(page, live):
    page.goto(live + "/next")
    page.fill(".search input", "lay tls")
    page.wait_for_selector(".suggest a")
    assert page.locator(".suggest a").first.inner_text().startswith("lay tiles")
    page.locator(".suggest a").first.click()
    assert task_overlay(page) == "lay tiles"


def test_search_suggestions_keyboard(page, live):
    page.goto(live + "/next")
    page.keyboard.press("f")
    page.keyboard.type("tiles")
    page.wait_for_selector(".suggest a")
    page.keyboard.press("ArrowDown")
    second = page.locator(".suggest a").nth(1)
    assert second.get_attribute("href").startswith("/tasks/")
    title = second.locator("span").first.inner_text()
    page.keyboard.press("Enter")
    assert task_overlay(page) == title  # a task opens in its overlay
    page.goto(live + "/next")
    page.fill(".search input", "tiles")
    page.wait_for_selector(".suggest a")
    page.keyboard.press("Escape")
    assert page.locator(".suggest").is_hidden()


@pytest.mark.parametrize("path", ["/projects/1?embed=overlay", "/inbox", "/calendar"])
def test_add_line_shows_every_field_once_focused(page, live, conn, path):
    page.goto(live + path)
    line = page.locator("main form.add")
    fields = (".md-editor", "[name=due_date]", "[name=recur]", "[name=remind_at]", "[name=new_context]", ".marks")
    assert all(line.locator(f).is_hidden() for f in fields)
    assert line.locator("[name=category]").is_hidden()  # no empty box on an idle line
    line.locator("[name=title]").click()  # a click on the idle line starts in the category box
    assert page.evaluate("document.activeElement.name") == "category"
    assert line.locator("[name=category]").is_visible()
    for field in fields:
        assert line.locator(field).is_visible(), field
    line.locator("[name=due_date]").fill("fri")  # previewed as typed, as on the task page
    line.locator(".hint.when[data-parse=due]", has_text="→").wait_for()
    line.locator("[name=due_date]").fill("")
    line.locator(".md-editor").click()
    page.keyboard.type("a note")
    page.locator("main h1, main .title-line").first.click()  # focus leaves the line: what's typed keeps it open
    assert line.locator("[name=recur]").is_visible()
    assert line.evaluate("""f => { const els = [...f.querySelectorAll('input, select, button, [contenteditable]')]
      .filter(el => el.checkVisibility() && el.tabIndex >= 0 && el.type !== 'hidden');
      return els[els.length - 1].matches('.primary'); }""")  # ⏎ is last in tab order, after every field
    line.locator("[name=title]").fill("by meta enter")
    line.locator("[name=due_date]").fill("today")  # required on the calendar
    with page.expect_navigation():
        line.locator("[name=due_date]").press("ControlOrMeta+Enter")
    assert conn.execute("SELECT 1 FROM tasks WHERE title = 'by meta enter'").fetchone()


def test_new_option_reveals_the_name_box(page, live):
    page.goto(live + "/tasks/2?embed=overlay")
    box = page.locator("input[name=new_project]")
    assert box.is_hidden()
    page.select_option("select[name=project_id]", "__new__")  # "＋ New project…": no save yet, it needs a name
    assert box.is_visible()
    page.fill("input[name=new_project]", "Garage")
    with page.expect_navigation():
        page.locator("input[name=new_project]").blur()
    assert page.locator("select[name=project_id] option:checked").inner_text() == "Garage"


def test_clicking_a_calendar_day_opens_an_add_dialog(page, live, conn):
    page.goto(live + "/calendar?m=2026-10")
    page.locator('.cal .day[data-date="2026-10-29"]').click()
    dialog = page.locator(".day-dialog")
    assert dialog.evaluate("d => d.open") and "29 Oct" in dialog.locator("h2").text_content()
    assert page.evaluate("document.activeElement.name") == "category"
    assert dialog.locator("[name=due_date]").input_value() == "2026-10-29"
    dialog.locator("[name=title]").fill("left over")  # cancelled: not on the next day's
    page.keyboard.press("Escape")
    page.locator('.cal .day[data-date="2026-10-28"]').click()
    assert dialog.locator("[name=title]").input_value() == "" and dialog.locator("[name=due_date]").input_value() == "2026-10-28"
    dialog.locator("[name=title]").fill("from the dialog")
    dialog.locator("[name=remind_at]").fill("tomorrow 3pm")
    dialog.locator("label", has_text="@morning").click()
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert page.url.endswith("/calendar?m=2026-10")  # stays on this month
    t = conn.execute("SELECT * FROM tasks WHERE title = 'from the dialog'").fetchone()
    assert t["due_date"] == "2026-10-28" and t["remind_at"] and "morning" in db.get_task(conn, t["id"])["contexts"]


def test_an_add_line_context_can_be_picked_before_typing(page, live, conn):
    """Its extra fields show only while the line is in use: pressing a word mustn't fold them away under the mouse."""
    page.goto(live + "/next")
    line = page.locator("main form.add")
    line.locator("[name=title]").focus()
    word = line.locator(".contexts label", has_text="@morning")
    word.click()
    assert word.locator("input").is_checked() and word.is_visible()
    line.locator("[name=title]").fill("picked first")
    with page.expect_navigation():
        line.locator("[name=title]").press("Enter")
    t = conn.execute("SELECT id FROM tasks WHERE title = 'picked first'").fetchone()
    assert "morning" in db.get_task(conn, t["id"])["contexts"]


def test_dragging_across_rows_or_days_selects_no_text(page, live):
    def sweep(start, end):
        a, b = start.bounding_box(), end.bounding_box()
        page.mouse.move(a["x"] + a["width"] - 4, a["y"] + a["height"] / 2)
        page.mouse.down()
        page.mouse.move(b["x"] + 4, b["y"] + b["height"] / 2, steps=8)
        page.mouse.up()
        return page.evaluate("getSelection().toString()")
    page.goto(live + "/next")
    rows = page.locator("main ul.tasks > li.task")
    assert sweep(rows.nth(0), rows.nth(1)) == ""
    page.goto(live + "/calendar?m=2026-10")
    cells = page.locator(".cal td")
    assert sweep(cells.nth(1), cells.nth(10)) == ""


def test_dragging_a_calendar_task_to_another_day_moves_its_due_date(page, live, conn):
    page.goto(live + "/calendar?m=2026-10")
    ev = page.locator(".cal .ev", has_text="buy tiles")  # due Oct 1, overdue
    assert "overdue" in ev.get_attribute("class")
    target = page.locator('.cal td', has=page.locator('.day[data-date="2026-10-29"]'))
    target.evaluate("e => e.scrollIntoView({block: 'center'})")  # below the add line: off the timer pill at the bottom
    with page.expect_response(lambda r: r.url.endswith("/due")) as saved:
        ev.drag_to(target, target_position={"x": 60, "y": 15})  # top of the cell, clear of the timer pill
    assert saved.value.status == 204
    assert target.locator(".ev", has_text="buy tiles").count() == 1  # moved in place, no reload
    assert "overdue" not in ev.get_attribute("class")
    assert db.get_task(conn, 2)["due_date"] == "2026-10-29"
    page.reload()
    assert target.locator(".ev", has_text="buy tiles").count() == 1


def test_question_mark_shows_shortcuts_and_letters_navigate(page, live):
    page.goto(live + "/next")
    page.keyboard.press("?")
    assert page.locator(".help-dialog").evaluate("d => d.open")
    # Focus starts on no field: just after closing it's still there for a moment, and a field would swallow the next key.
    assert page.evaluate("document.activeElement.tagName") == "H2"
    page.keyboard.press("n")  # ignored while the overlay is open
    page.keyboard.press("Escape")
    assert not page.locator(".help-dialog").evaluate("d => d.open")
    for key, path in (("i", "/inbox"), ("w", "/waiting"), ("s", "/someday"), ("p", "/projects/active"),
                      ("Shift+W", "/projects/waiting"), ("Shift+S", "/projects/someday"), ("n", "/next")):
        with page.expect_navigation():
            page.keyboard.press(key)
        assert page.url == live + path


def test_notes_render_markdown_and_save_what_you_typed(page, live, conn):
    pid = db.add_reference(conn, "Markdown", notes="# Head\nsee **bold** and https://example.com")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed = page.locator(".md-editor")
    assert page.locator("textarea[name=notes]").is_hidden()
    assert ed.locator(".h1").inner_text() == "Head"
    assert ed.locator("b").inner_text() == "bold"  # ** hidden while the caret is elsewhere
    assert ed.locator("a").get_attribute("href") == "https://example.com"
    ed.click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.press("Enter")
    page.keyboard.type("- *new* line")
    assert ed.locator("i").count() == 1
    page.keyboard.press("n")  # typing in the editor never fires a go-key
    with page.expect_response(lambda r: "/autosave/" in r.url):
        page.locator("input.big").focus()  # leaving the notes saves them
    assert db.get_reference(conn, pid)["notes"].replace("\r\n", "\n") == \
        "# Head\nsee **bold** and https://example.com\n- *new* linen"


def test_notes_caret_stays_after_a_typed_closing_marker_in_safari(webkit_page, live, conn):
    # Safari moves a caret placed in hidden text to the nearest visible spot, so a re-render must show the
    # markers before it puts the caret back, or "_a_" then "Z" gives "_aZ_". Chromium doesn't move it.
    pid = db.add_reference(conn, "Caret", notes="")
    pg = webkit_page
    pg.goto(live + f"/reference/{pid}?embed=overlay")
    ed = pg.locator(".md-editor")
    ed.click()
    pg.keyboard.type("_a_Z **b**Y")
    assert ed.text_content() == "_a_Z **b**Y"


def test_notes_editor_undo(page, live, conn):
    pid = db.add_reference(conn, "Undo", notes="abc")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed = page.locator(".md-editor")
    ed.click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type("def")
    assert ed.text_content() == "abcdef"
    page.keyboard.press("ControlOrMeta+z")
    assert ed.text_content() == "abc"
    assert page.locator("textarea[name=notes]").input_value() == "abc"


def test_title_and_notes_save_themselves(page, live, conn):
    pid = db.add_reference(conn, "Auto", notes="abc")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    page.locator(".md-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type("def")
    page.locator("form[data-autosave] input[name=title]").fill("Autosaved")
    page.locator("form[data-autosave] input[name=title]").blur()  # leaving a field saves at once, no 2 s wait
    page.wait_for_function("document.querySelector('.autosave').textContent === 'Saved'")
    r = db.get_reference(conn, pid)
    assert (r["title"], r["notes"]) == ("Autosaved", "abcdef")


def test_autosave_conflict_keep_mine_and_merge(page, live, conn):
    pid = db.add_reference(conn, "Clash", notes="one\ntwo")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    db.update_reference(conn, pid, notes="one\nTHEIRS")
    page.locator(".md-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(" mine")
    page.locator("form[data-autosave] input[name=title]").focus()
    page.get_by_role("button", name="merge").click()
    assert page.locator(".md-editor").text_content() == \
        "one\n<<<<<<< mine\ntwo mine\n=======\nTHEIRS\n>>>>>>> theirs"
    page.locator("form[data-autosave] input[name=title]").focus()  # the merge saves like any edit
    page.wait_for_function("document.querySelector('.autosave').textContent === 'Saved'")
    assert "<<<<<<< mine" in db.get_reference(conn, pid)["notes"]
    db.update_reference(conn, pid, title="Theirs")
    page.locator("form[data-autosave] input[name=title]").fill("Mine")
    page.locator(".md-editor").focus()
    page.get_by_role("button", name="keep mine").click()
    page.wait_for_function("document.querySelector('.autosave').textContent === 'Saved'")
    assert db.get_reference(conn, pid)["title"] == "Mine"
    page.errors[:] = [e for e in page.errors if "409 (CONFLICT)" not in e]  # the refusals were the point


def test_coming_back_to_the_tab_loads_edits_made_elsewhere(page, live, conn):
    pid = db.add_project(conn, "Elsewhere", notes="old")
    page.goto(live + f"/projects/{pid}?embed=overlay")
    db.update_project(conn, pid, notes="new from phone")
    page.evaluate("dispatchEvent(new Event('focus'))")
    page.wait_for_function("document.querySelector('.md-editor').textContent === 'new from phone'")
    assert page.locator(".autosave").text_content() == "Updated from elsewhere"
    assert db.get_project(conn, pid)["notes"] == "new from phone"


def drag_to_panel(page, source, target):
    """The drop panel only appears once a drag starts, so drag by hand: pick up the row's grip, then find the target."""
    source.locator(".grip").hover()
    page.mouse.down()
    page.mouse.move(100, 100, steps=5)
    target = page.locator(".drop-panel " + target)
    target.wait_for(state="visible")
    target.hover()
    page.mouse.up()


def test_dragging_an_inbox_row_onto_a_project_files_it(page, live, conn):
    page.goto(live + "/inbox")
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out
    drag_to_panel(page, row(page, "inbox item"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert page.url == live + "/inbox"
    assert db.get_task(conn, 1)["project_id"] == 1
    assert row(page, "legacy orphan").count() == 1
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out


def test_shift_clicked_rows_drag_together(page, live, conn):
    page.goto(live + "/inbox")
    orphan = conn.execute("SELECT id FROM tasks WHERE title = 'legacy orphan'").fetchone()[0]
    row(page, "inbox item").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    row(page, "legacy orphan").locator(".title").click(modifiers=["Shift"])  # the title too: selects, doesn't open
    assert page.url == live + "/inbox" and len(page.context.pages) == 1
    assert page.locator("li.task.selected").count() == 2
    row(page, "inbox item").click(modifiers=["Shift"], position={"x": 400, "y": 10})  # toggles off
    assert page.locator("li.task.selected").count() == 1
    page.keyboard.press("Escape")
    assert page.locator("li.task.selected").count() == 0
    row(page, "inbox item").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    row(page, "legacy orphan").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    drag_to_panel(page, row(page, "legacy orphan"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task.selected')")
    assert db.get_task(conn, 1)["project_id"] == db.get_task(conn, orphan)["project_id"] == 1
    assert page.locator("main li.task[data-id] .title").all_inner_texts() == ["sail"]  # the tickled someday task


def test_an_unselected_row_drags_alone(page, live, conn):
    page.goto(live + "/inbox")
    row(page, "legacy orphan").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    drag_to_panel(page, row(page, "inbox item"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_task(conn, 1)["project_id"] == 1 and row(page, "legacy orphan").count() == 1


def test_dropping_an_inbox_row_on_reference_files_it_there(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item"), '[data-project="reference"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_task(conn, 1) is None
    assert [(r["title"], r["notes"]) for r in db.references(conn) if r["title"] == "inbox item"] == [("inbox item", "from email")]


def test_dropping_an_inbox_row_on_new_asks_for_the_name_in_the_panel(page, live, conn):
    """Under Someday (as under Waiting), "＋ New project…" makes a project of that status."""
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item"), '[data-project="__new__someday"]')
    name = page.locator(".drop-panel .naming input[name=new_project]")
    name.wait_for(state="visible")
    assert page.locator(".drop-panel input[name=new_project]:visible").count() == 1
    assert page.evaluate("document.activeElement.name") == "new_project"
    page.keyboard.type("Dragged")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    p = db.get_project(conn, db.get_task(conn, 1)["project_id"])
    assert (p["title"], p["status"]) == ("Dragged", "someday")
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out
    # Every project list on the page has it at once, without a reload.
    assert page.locator(".drop-panel .target", has_text="Dragged").count() == 1
    drag_to_panel(page, row(page, "legacy orphan"), f'[data-project="{p["id"]}"]')
    page.wait_for_function("document.querySelectorAll('main li.task[data-id]').length == 1")  # the tickled "sail"
    assert [t["title"] for t in db.project_tasks(conn, p["id"])] == ["legacy orphan", "inbox item"]


def test_escape_cancels_naming_a_new_project(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item"), '[data-project="__new__"]')
    page.locator(".drop-panel .naming input[name=new_project]").wait_for(state="visible")
    page.keyboard.press("Escape")
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out
    assert db.get_task(conn, 1)["inbox"] and row(page, "inbox item").count() == 1


def test_pinned_project_panel_stays_open_until_hidden(page, live, conn):
    page.goto(live + "/inbox")
    panel = page.locator(".drop-panel")
    panel.wait_for(state="hidden")
    page.locator(".pin-panel").click()
    panel.wait_for(state="visible")
    assert panel.locator("h3").all_text_contents() == ["Tasks (no project)", "Active", "Waiting", "Someday"]
    drag_to_panel(page, row(page, "inbox item"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert panel.is_visible()  # still pinned after the drop
    page.reload()
    panel.wait_for(state="visible")  # and after a reload
    page.add_init_script("addEventListener('transitionrun', e => { if (e.target.matches('.drop-panel')) window.slid = true; })")
    page.route("**/move.js", lambda r: (page.wait_for_timeout(200), r.continue_()))  # the page paints before move.js runs
    page.goto(live + "/next")
    panel.wait_for(state="visible")  # and on every other page: one pin for the whole site
    page.wait_for_timeout(300)
    assert not page.evaluate("window.slid")  # open from the first paint, no slide-in
    panel.locator('[data-project="1"] a').click()  # a project's name opens its overlay
    page.locator("dialog.overlay[open] iframe[src^='/projects/1?']").wait_for()
    page.keyboard.press("Escape")
    page.locator("dialog.overlay").wait_for(state="detached")
    with page.expect_navigation():  # a task tab's name goes to its page
        panel.locator('[data-project="waiting"] a').click()
    assert page.url == live + "/waiting"
    with page.expect_navigation():  # a project group's heading goes to its project tab
        panel.locator("h3 a", has_text="Active").click()
    assert page.url == live + "/projects/active"
    panel.wait_for(state="visible")  # the project tabs have it too, for their steps
    page.go_back()  # Reference has none
    with page.expect_navigation():
        panel.locator('[data-project="reference"] a').click()
    assert page.url == live + "/reference"
    page.go_back()
    panel.locator(".unpin").click()  # the panel floats over the header's button
    panel.wait_for(state="hidden")
    page.goto(live + "/inbox")
    assert not panel.is_visible()


def test_dragging_a_next_action_onto_another_project_moves_it(page, live, conn):
    grout = db.add_task(conn, "order grout", step="next", project_id=1)  # undated: on Next
    page.goto(live + "/next")
    drag_to_panel(page, row(page, "order grout"), '[data-project="2"]')
    page.wait_for_function(f"!document.querySelector('li.task[data-id=\"{grout}\"]')")
    assert page.url == live + "/next"
    assert (db.get_task(conn, grout)["project_id"], db.get_task(conn, grout)["step"]) == (2, "later")  # next stays a conscious choice


def test_dragging_between_h0_and_a_project_on_next(page, live, conn):
    """An H0 task filed in a project leaves Next (a later step); a project's next action dropped on H0 stays on it."""
    measure = conn.execute("SELECT id FROM tasks WHERE title = ?", ("measure wall",)).fetchone()[0]
    grout = db.add_task(conn, "order grout", step="next", project_id=1)  # undated: on Next
    page.goto(live + "/next")
    page.locator(".pin-panel").click()
    drag_to_panel(page, row(page, "measure wall"), '[data-project="1"]')
    page.wait_for_function(f"!document.querySelector('li.task[data-id=\"{measure}\"]')")
    t = db.get_task(conn, measure)
    assert (t["project_id"], t["step"], t["ground"]) == (1, "later", 0)
    with page.expect_navigation():  # it stays on Next, now under H0: the page redraws
        drag_to_panel(page, row(page, "order grout"), '[data-project="h0"]')
    assert row(page, "order grout").locator(".proj").inner_text() == "H0"
    t = db.get_task(conn, grout)
    assert (t["project_id"], t["step"], t["ground"]) == (None, "next", 1)


def test_dragging_a_project_cards_step_files_it(page, live, conn):
    """On a project tab a folded-out step's ⠿ drags it to the drop panel; the page redraws (the project's next action
    may change), its fold-outs still open. The project's own order isn't touched."""
    page.goto(live + "/projects/active")
    order = [p["id"] for p in db.projects(conn, "active")]
    page.locator(".peek-all").click()
    step = page.locator('.steps > li[data-id="3"]')  # "lay tiles", in Kitchen
    with page.expect_navigation():
        drag_to_panel(page, step, '[data-project="2"]')
    t = db.get_task(conn, 3)
    assert (t["project_id"], t["step"]) == (2, "later")
    sync_api.expect(page.locator('li.task[data-id="2"] .steps > li[data-id="3"]')).to_be_visible()
    assert [p["id"] for p in db.projects(conn, "active")] == order
    with page.expect_navigation():  # shift-clicked steps go together, from any project
        page.locator('.steps > li[data-id="2"]').click(modifiers=["Shift"])  # "buy tiles"
        page.locator('.steps > li[data-id="3"]').click(modifiers=["Shift"])
        drag_to_panel(page, page.locator('.steps > li[data-id="3"]'), '[data-project="someday"]')
    assert [(db.get_task(conn, i)["ground"], db.get_task(conn, i)["step"]) for i in (2, 3)] == [(1, "someday")] * 2


def test_dragging_a_waiting_task_keeps_it_waiting(page, live, conn):
    page.goto(live + "/waiting")
    drag_to_panel(page, row(page, "quote from Bob"), '[data-project="2"]')
    page.wait_for_function("document.querySelector('li.task[data-id=\"4\"] .proj')?.textContent === 'Stalled'")
    assert (db.get_task(conn, 4)["project_id"], db.get_task(conn, 4)["step"]) == (2, "waiting")


def test_waiting_projects_and_tasks_have_their_own_tabs(page, live, conn):
    """Waiting projects drag to reorder on their tab; Waiting lists the waiting tasks, and the panel's Waiting files
    a task there."""
    db.set_project_outcome(conn, db.add_project(conn, "Visa", status="waiting"), "visa issued")
    page.goto(live + "/projects/waiting")
    assert page.locator("ul[data-reorder] > li.task > a.title").all_inner_texts() == ["Visa", "Waiting proj"]
    with page.expect_response(lambda r: r.url.endswith("/order/waiting")) as saved:
        row(page, "Waiting proj").locator(":scope > .grip").drag_to(row(page, "Visa"), target_position={"x": 40, "y": 4})
    assert saved.value.status == 204
    assert [p["title"] for p in db.projects(conn, "waiting")] == ["Waiting proj", "Visa"]
    measure = conn.execute("SELECT id FROM tasks WHERE title = ?", ("measure wall",)).fetchone()[0]
    page.goto(live + "/next")
    drag_to_panel(page, row(page, "measure wall"), '[data-project="waiting"]')
    page.wait_for_function(f"!document.querySelector('li.task[data-id=\"{measure}\"]')")
    assert (db.get_task(conn, measure)["step"], db.get_task(conn, measure)["ground"]) == ("waiting", 1)
    page.goto(live + "/waiting")
    assert page.locator("ul[data-reorder] > li.task > a.title").count() == 0  # no project rows here
    assert page.locator("ul[data-reorder] > li.task > .name > a.title").all_inner_texts() == [
        "measure wall", "parcel from Ann", "quote from Bob"]
    # An H0 waiting task dropped on a project stays waiting.
    drag_to_panel(page, row(page, "parcel from Ann"), '[data-project="2"]')
    page.wait_for_function("document.querySelector('li.task[data-id=\"14\"] .proj')?.textContent === 'Stalled'")
    t = db.get_task(conn, 14)
    assert (t["project_id"], t["step"], t["ground"]) == (2, "waiting", 0)


def test_review_arrow_keys_move_between_steps(page, live):
    page.goto(live + "/review?step=overdue")
    page.keyboard.press("ArrowRight")
    page.wait_for_url("**/review?step=nexts")
    page.keyboard.press("ArrowLeft")
    page.wait_for_url("**/review?step=overdue")
    page.locator("a[rel=next]").click()
    page.wait_for_url("**/review?step=nexts")


def test_review_step_list_sticks_below_the_shown_nav(page, live):
    """The step list is sticky; when the nav slides back on scrolling up, the list moves down under it, not behind it."""
    page.set_viewport_size({"width": 1200, "height": 500})
    page.goto(live + "/review?step=projects")
    page.evaluate("document.querySelector('.review-page').style.minHeight = '3000px'")  # a long card
    page.mouse.move(600, 300)
    page.mouse.wheel(0, 1000)
    page.wait_for_function("document.body.classList.contains('nav-hidden')")
    page.mouse.wheel(0, -100)
    page.wait_for_function("!document.body.classList.contains('nav-hidden')")
    page.wait_for_function("""document.querySelector('.review-steps').getBoundingClientRect().top
        >= document.querySelector('nav').getBoundingClientRect().bottom""")
    page.wait_for_timeout(300)  # past both transitions: still clear of the nav
    assert page.evaluate("""document.querySelector('.review-steps').getBoundingClientRect().top
        >= document.querySelector('nav').getBoundingClientRect().bottom""")


# --- review fixes (2026-10-27) ---------------------------------------------

def test_a_failed_tick_off_reloads_instead_of_faking_it(page, live, conn):
    page.goto(live + "/next")
    page.route("**/complete", lambda r: r.fulfill(status=500, body=""))
    with page.expect_navigation():
        row(page, "measure wall").locator("form.done button").click()
    assert row(page, "measure wall").count() == 1 and db.get_task(conn, conn.execute("SELECT id FROM tasks WHERE title = ?", ("measure wall",)).fetchone()[0])["state"] == "open"
    page.errors[:] = [e for e in page.errors if "500" not in e]  # the faked failure itself


def test_a_failed_calendar_drag_reloads(page, live, conn):
    page.goto(live + "/calendar?m=2026-10")
    page.route("**/due", lambda r: r.fulfill(status=500, body=""))
    ev = page.locator(".cal .ev", has_text="buy tiles")
    target = page.locator(".cal td", has=page.locator('.day[data-date="2026-10-29"]'))
    target.evaluate("e => e.scrollIntoView({block: 'center'})")  # below the add line: off the timer pill at the bottom
    with page.expect_navigation():
        ev.drag_to(target, target_position={"x": 60, "y": 15})  # clear of the timer pill
    assert page.locator('td[data-date="2026-10-01"] .ev', has_text="buy tiles").count() == 1
    page.errors[:] = [e for e in page.errors if "500" not in e]  # the faked failure itself


def test_tick_off_updates_the_calendar_dot(page, live):
    """The Calendar's dot counts what's due today or overdue; ticking one off in the Agenda lowers it in place."""
    page.goto(live + "/calendar?view=agenda")
    dot = "(document.querySelector('nav a[href=\"/calendar\"] .badge')?.ariaLabel ?? '0')"  # a zero shows no dot
    n = int(page.evaluate(dot))
    assert n == page.locator("main li.task").count() and page.locator("nav a[href='/next'] .badge").count() == 0
    row(page, "vacuum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    page.wait_for_function(f"{dot} === '{n - 1}'")
    # Two tick-offs whose /nav answers arrive out of order: the later refresh's counts stay.
    stale = []
    page.route("**/nav", lambda r: stale.append((r, r.fetch().text())) if not stale else r.continue_())
    row(page, "quote from Bob").locator("form.done button").click()
    while not stale:
        page.wait_for_timeout(50)
    row(page, "buy tiles").locator("form.done button").click()
    page.wait_for_function(f"{dot} === '{n - 3}'")
    route, body = stale[0]
    route.fulfill(body=body, content_type="text/html")
    page.wait_for_timeout(300)
    assert page.evaluate(dot) == str(n - 3)


def test_capture_keys_do_nothing_while_help_is_open(page, live):
    page.goto(live + "/next")
    page.keyboard.press("?")
    page.keyboard.press("/")
    page.keyboard.press("f")
    assert not page.locator(".capture-dialog").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.name") != "q"


def task_overlay(page):
    """The record open in the top overlay (app.js): its title, once its frame has loaded."""
    page.wait_for_selector("dialog.overlay[open]")
    return page.locator("dialog.overlay").last.frame_locator("iframe").locator("input.big").input_value()


def test_a_task_opens_in_an_overlay_over_the_page(page, live, conn):
    """Closing it is going back: nothing changed, the page stays as it was; changed, it reloads to show it.
    /tasks/<id> opens the task's list with it open."""
    measure = conn.execute("SELECT id FROM tasks WHERE title = ?", ("measure wall",)).fetchone()[0]
    grout = db.add_task(conn, "order grout", step="next", project_id=1)  # a project's undated step: on Next
    page.goto(live + "/next")
    page.evaluate("window.mark = 1")  # gone after a reload
    row(page, "measure wall").locator(".title").click()
    assert task_overlay(page) == "measure wall" and page.url == live + "/next"
    frame = page.frame_locator("dialog.overlay iframe")
    frame.locator("input.big").focus()
    page.keyboard.press("Escape")  # in the frame
    page.wait_for_selector("dialog.overlay[open]", state="detached")
    assert page.evaluate("window.mark") == 1
    row(page, "measure wall").locator(".title").click()
    task_overlay(page)
    frame.locator("input.big").fill("measure all")  # saved on closing, before the page reloads
    with page.expect_navigation():
        page.locator("dialog.overlay .close").click()
    assert db.get_task(conn, measure)["title"] == "measure all" and row(page, "measure all").count() == 1
    # /tasks/2, a step: Projects, its project's overlay, the step's over it. Closing each drops its part of the URL.
    page.goto(live + "/tasks/2")
    assert page.url == live + "/projects/active?project=1&task=2" and task_overlay(page) == "buy tiles"
    assert page.locator("dialog.overlay").count() == 2
    page.keyboard.press("Escape")
    page.wait_for_function("document.querySelectorAll('dialog.overlay').length === 1")
    assert page.url == live + "/projects/active?project=1"
    # A step opened from its project's overlay stacks over it; its ↗ goes back down to the project, not a second copy.
    project = page.frame_locator("dialog.overlay iframe")
    project.locator("li.task", has_text="lay tiles").locator(".title").click()
    page.wait_for_function("document.querySelectorAll('dialog.overlay').length === 2")
    page.locator("dialog.overlay").last.frame_locator("iframe").locator("a.open-project").click()
    page.wait_for_function("document.querySelectorAll('dialog.overlay').length === 1")
    # Ctrl+↑: a step's parent is its project (opened in its place), the project's is Projects (all closed).
    page.goto(live + "/next")
    row(page, "order grout").locator(".title").click()
    task_overlay(page)
    page.frame_locator("dialog.overlay iframe").locator("input.big").focus()  # typing in a field too
    page.keyboard.press("Control+ArrowUp")
    page.wait_for_function("document.querySelector('dialog.overlay iframe')?.src.includes('/projects/1?')")
    assert page.locator("dialog.overlay").count() == 1 and page.url == live + "/next"
    page.keyboard.press("Control+ArrowUp")  # at once, before its page is in: waits for it
    page.wait_for_url(live + "/projects/active")
    assert page.locator("dialog.overlay").count() == 0
    # ✕ abandons and stays, as ✓ does: the overlay shows it abandoned, with Revive; closing reloads the page under it.
    page.goto(live + "/next")
    row(page, "measure all").locator(".title").click()
    task_overlay(page)
    frame = page.frame_locator("dialog.overlay iframe")
    frame.get_by_role("button", name="Abandon").click()
    frame.get_by_role("button", name="Revive").wait_for()
    assert page.locator("dialog.overlay").count() == 1 and db.get_task(conn, measure)["state"] == "abandoned"
    with page.expect_navigation():
        page.keyboard.press("Escape")
    assert page.url == live + "/next" and row(page, "measure all").count() == 0
    # ✕ on a closed task deletes it for good and closes the overlay, back to the page under it (reloaded).
    page.goto(live + "/done")
    page.locator("li.task", has_text="measure all").locator(".title").click()
    task_overlay(page)
    page.once("dialog", lambda d: d.accept())
    with page.expect_navigation():
        page.frame_locator("dialog.overlay iframe").get_by_role("button", name="Delete for good").click()
    assert page.url == live + "/done" and page.locator("dialog.overlay").count() == 0 and db.get_task(conn, measure) is None
    # A toast in the frame (an unreadable box's warning) doesn't take Esc: it still closes the overlay.
    page.goto(live + "/next?task=6")  # "bins"
    task_overlay(page)
    frame = page.frame_locator("dialog.overlay iframe")
    frame.locator("input[name=due_date]").fill("gibberish")
    frame.locator("input[name=due_date]").press("Tab")
    frame.locator(".flashes").wait_for()
    page.keyboard.press("Escape")
    page.wait_for_selector("dialog.overlay", state="detached")
    # Esc on an open popover (Outcome reached) closes only the popover; the next Esc closes the overlay.
    page.goto(live + "/projects/active?project=1")
    page.wait_for_selector("dialog.overlay[open]")
    frame = page.frame_locator("dialog.overlay iframe")
    frame.get_by_role("button", name="Outcome reached").click()
    sync_api.expect(frame.locator("#reached")).to_be_visible()
    page.keyboard.press("Escape")
    sync_api.expect(frame.locator("#reached")).to_be_hidden()
    assert page.locator("dialog.overlay").count() == 1
    page.keyboard.press("Escape")
    page.wait_for_selector("dialog.overlay", state="detached")
    # Closed while a tick-off is still on its way, it waits for it, then reloads the page under it.
    page.goto(live + "/projects/active?project=1")
    page.wait_for_selector("dialog.overlay[open]")
    held = []
    page.route("**/tasks/2/complete", lambda r: held.append(r))
    frame = page.frame_locator("dialog.overlay iframe")
    frame.locator("li.task", has_text="buy tiles").locator("form.done button").click()
    while not held:
        page.wait_for_timeout(50)
    page.keyboard.press("Escape")
    page.wait_for_timeout(300)
    assert page.locator("dialog.overlay").count() == 1  # still waiting for the post
    with page.expect_navigation():
        held[0].continue_()
    assert page.locator("dialog.overlay").count() == 0 and db.get_task(conn, 2)["state"] == "done"


def test_an_overlay_opened_from_the_url_starts_focused_on_itself(page, live):
    """Not on ×, which would show its keyboard focus ring on a page just loaded; Esc still closes it."""
    page.goto(live + "/next?task=5")
    assert task_overlay(page) == "vacuum"
    assert page.evaluate("document.activeElement.matches('dialog.overlay')")
    page.keyboard.press("Escape")
    page.wait_for_selector("dialog.overlay", state="detached")


def test_a_narrow_nav_folds_the_review_tabs_behind_more(page, live):
    """Too narrow for every tab (a small window, or the pinned panel beside it): Review…Reference fold behind ⋯, and
    the nav doesn't scroll sideways."""
    page.goto(live + "/next")
    more = page.locator("nav .more")
    assert more.is_hidden() and page.locator("nav .aux a", has_text="Done").is_visible()
    page.set_viewport_size({"width": 800, "height": 600})
    assert more.is_visible() and page.locator("nav .aux").is_hidden()
    assert page.evaluate("(n => n.scrollWidth <= n.clientWidth)(document.querySelector('nav'))")
    more.click()
    page.locator("nav .aux a", has_text="Done").click()
    page.wait_for_url(live + "/done")
    assert page.evaluate("getComputedStyle(document.querySelector('nav .more')).borderBottomStyle") == "solid"  # its page


def test_clicking_anywhere_on_a_row_opens_it(page, live):
    page.goto(live + "/next")
    r = row(page, "measure wall")
    box = r.bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.7, box["y"] + box["height"] / 2)  # blank space right of the title
    assert task_overlay(page) == "measure wall" and page.url == live + "/next"
    page.keyboard.press("Escape")
    page.goto(live + "/projects/active")
    r = row(page, "Kitchen")
    box = r.bounding_box()
    page.mouse.click(box["x"] + box["width"] * 0.7, box["y"] + box["height"] / 2)
    page.wait_for_selector("dialog.overlay[open]")  # a project opens in its overlay too
    assert page.url == live + "/projects/active"


def test_clicking_a_rows_button_does_not_open_it(page, live):
    page.goto(live + "/projects/active")
    row(page, "Kitchen").locator("button.peek").click()
    assert page.url == live + "/projects/active"


def test_horizons_map(page, live, conn):
    """The overview draws every horizon as a graph: hovering lights a node's neighbours, a swatch hides a level (H0
    actions start hidden), a click opens a record's overlay, and the purpose & vision card's area lines save in place."""
    page.goto(live + "/horizons")
    node = lambda title: page.locator(".graph .node", has=page.locator(f"text={title}")).first
    sync_api.expect(node("Home")).to_be_visible()
    sync_api.expect(node("vacuum")).to_be_hidden()  # H0 starts hidden
    page.locator(".swatch[data-kind=task]").click()
    sync_api.expect(node("vacuum")).to_be_visible()
    node("Home").hover()
    sync_api.expect(node("Ship the kitchen")).to_have_class(re.compile(r"\blit\b"))
    assert "lit" not in node("Work").get_attribute("class")
    page.locator(".swatch[data-kind=goal]").click()
    sync_api.expect(node("Ship the kitchen")).to_be_hidden()
    page.reload()  # what's hidden is remembered
    sync_api.expect(node("Home")).to_be_visible()
    sync_api.expect(node("Ship the kitchen")).to_be_hidden()
    node("Home").locator("circle").click()
    sync_api.expect(page.locator("dialog.overlay iframe")).to_have_attribute("src", re.compile(r"/horizons/\d+\?embed=overlay"))
    assert page.url == live + "/horizons"
    # Panned or zoomed, a resize keeps the view's scale (a fit would change it).
    page.locator("dialog.overlay .close").click()
    page.wait_for_selector("dialog.overlay", state="detached")
    view = lambda: page.locator("svg.graph > g").first.get_attribute("transform")
    svg = page.locator("svg.graph").bounding_box()
    page.mouse.move(svg["x"] + svg["width"] / 2, svg["y"] + svg["height"] / 2)
    page.mouse.wheel(0, -300)
    zoomed = view()
    page.set_viewport_size({"width": 1000, "height": 700})
    page.wait_for_function(f"document.querySelector('svg.graph > g').getAttribute('transform') !== {zoomed!r}")
    assert re.search(r"scale\(([\d.]+)\)", view())[1] == re.search(r"scale\(([\d.]+)\)", zoomed)[1]
    page.goto(live + "/horizons?level=purpose")
    guide = page.locator("#guide-vision")
    sync_api.expect(guide).to_be_hidden()  # the guide waits behind its ?
    page.get_by_role("button", name="How to work H4 · Vision (3–5 years)").click()
    sync_api.expect(guide).to_be_visible()
    sync_api.expect(guide.get_by_text("Do this")).to_be_visible()
    page.keyboard.press("Escape")
    sync_api.expect(guide).to_be_hidden()
    box = page.locator(".visions textarea").nth(1)  # Work
    box.fill("Teaching what I love")
    with page.expect_response("**/vision"):
        box.press("Tab")  # saved as it's left
    assert page.url.endswith("?level=purpose")  # saved in place
    assert db.horizons(conn)["area"][1]["vision"] == "Teaching what I love"


def test_remind_box_previews_how_it_reads(page, live):
    page.goto(live + "/tasks/7?embed=overlay")  # "call mum": a reminder still to come
    hint = page.locator(".when[data-parse=remind]")
    page.fill("input[name=remind_at]", "oct 30 at 5pm")
    sync_api.expect(hint).to_have_text("→ Fri Oct 30, 17:00")
    page.fill("input[name=remind_at]", "yesterday")
    sync_api.expect(hint).to_have_text("→ Wed Oct 21, 09:00 · already passed")
    page.fill("input[name=remind_at]", "gibberish")
    sync_api.expect(hint).to_have_text("not understood")
    page.fill("input[name=remind_at]", "")
    sync_api.expect(hint).to_have_text("")


def test_a_saved_box_shows_how_it_was_read(page, live, conn):
    """Typed words would read differently if posted again later (a select's submit, the next box's save)."""
    page.goto(live + "/tasks/7?embed=overlay")
    due, remind = page.locator("input[name=due_date]"), page.locator("input[name=remind_at]")
    due.fill("saturday")
    remind.focus()
    sync_api.expect(due).to_have_value("2026-10-24")
    remind.fill("oct 30 at 5pm")
    due.focus()
    sync_api.expect(remind).to_have_value("2026-10-30 17:00")
    sync_api.expect(page.locator(".when[data-parse=remind]")).to_have_text("→ Fri Oct 30, 17:00")
    due.fill("gibberish")
    remind.focus()
    page.locator(".flashes").wait_for()
    assert due.input_value() == "gibberish" and db.get_task(conn, 7)["due_date"] == "2026-10-24"  # left to be fixed


def test_due_and_repeat_boxes_preview_how_they_read(page, live):
    page.goto(live + "/tasks/7?embed=overlay")
    due, rule = page.locator(".when[data-parse=due]"), page.locator(".when[data-parse=recur]")
    page.fill("input[name=due_date]", "tomorrow")
    sync_api.expect(due).to_have_text("→ Fri Oct 23")
    page.fill("input[name=recur]", "every 2 weeks")  # leaving the due box saved it, in the background: no reload
    sync_api.expect(rule).to_have_text("→ every 2 weeks · next Fri Nov 6")
    page.fill("input[name=due_date]", "oct 30")  # the repeat's next date follows the due box
    sync_api.expect(rule).to_have_text("→ every 2 weeks · next Fri Nov 13")
    page.fill("input[name=recur]", "fortnightly")
    sync_api.expect(rule).to_have_text("not understood")
    page.fill("input[name=due_date]", "")
    sync_api.expect(due).to_have_text("")
    page.locator("input.big").focus()  # (saves the empty due date; the bad rule is flashed, not saved)
    page.locator(".flashes", has_text="fortnightly").wait_for()


def test_remind_button_copies_the_picked_date_and_time_into_the_text_box(page, live, conn):
    page.goto(live + "/tasks/7?embed=overlay")  # "call mum": a reminder still to come
    field = page.locator(".date-field", has=page.locator("input[name=remind_at]"))
    field.locator(".pick").click()  # showPicker() needs this real click's user activation
    assert field.locator(".picker").input_value() == page.input_value("input[name=remind_at]").replace(" ", "T")
    field.locator(".picker").evaluate(
        "el => { el.value = '2026-11-05T17:30'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.input_value("input[name=remind_at]") == "2026-11-05 17:30"
    sync_api.expect(page.locator(".when[data-parse=remind]")).to_have_text("→ Thu Nov 5, 17:30")  # the preview follows
    for _ in range(50):  # and the pick saves itself in the background
        if db.get_task(conn, 7)["remind_at"] and datetime.fromisoformat(db.get_task(conn, 7)["remind_at"]).astimezone().strftime("%H:%M") == "17:30":
            break
        page.wait_for_timeout(50)
    assert datetime.fromisoformat(db.get_task(conn, 7)["remind_at"]).astimezone().strftime("%Y-%m-%d %H:%M") == "2026-11-05 17:30"


# --- decluttering (2026-10-29) ---------------------------------------------

def test_nav_hides_on_scrolling_down_and_returns_on_scrolling_up(page, live):
    page.set_viewport_size({"width": 1200, "height": 500})
    page.goto(live + "/next")
    page.evaluate("document.querySelector('main').style.minHeight = '3000px'")  # room to scroll
    hidden = lambda: page.evaluate("document.body.classList.contains('nav-hidden')")
    page.mouse.move(600, 300)
    page.mouse.wheel(0, 600)
    page.wait_for_function("document.body.classList.contains('nav-hidden')")
    page.mouse.wheel(0, -100)
    page.wait_for_function("!document.body.classList.contains('nav-hidden')")
    page.mouse.wheel(0, 300)
    page.wait_for_function("document.body.classList.contains('nav-hidden')")
    page.mouse.move(600, 10)  # pointing at the top edge brings it back
    assert not hidden()


def test_nav_hides_and_returns_with_touch_scrolling(browser, live):
    """iPad: no mouse, only scrolling. Scrolling up must bring the nav back."""
    ctx = browser.new_context(viewport={"width": 834, "height": 1194}, has_touch=True, is_mobile=True)  # iPad Pro 11
    pg = ctx.new_page()
    pg.goto(live + "/next")
    pg.evaluate("document.querySelector('main').style.minHeight = '5000px'")
    pg.evaluate("scrollTo(0, 800)")
    pg.wait_for_function("document.body.classList.contains('nav-hidden')")
    pg.wait_for_function("document.querySelector('nav').getBoundingClientRect().bottom <= 1")  # slid out of view
    pg.evaluate("scrollTo(0, 500)")
    pg.wait_for_function("!document.body.classList.contains('nav-hidden')")
    ctx.close()


def test_project_status_select_saves_on_pick(page, live, conn):
    page.goto(live + "/projects/1?embed=overlay")
    assert page.get_by_role("button", name="Abandon").count() == 0 and page.get_by_role("button", name="Someday").count() == 0
    with page.expect_navigation():
        page.get_by_label("Status").select_option("someday")
    assert db.get_project(conn, 1)["status"] == "someday" and page.url == live + "/projects/1?embed=overlay"
    with page.expect_navigation():
        page.get_by_label("Status").select_option("abandoned")
    assert db.get_project(conn, 1)["status"] == "abandoned"
    with page.expect_navigation():
        page.get_by_label("Status").select_option("active")
    assert db.get_project(conn, 1)["status"] == "active"


def test_task_step_select_saves_on_pick(page, live, conn):
    page.goto(live + "/tasks/3?embed=overlay")  # "lay tiles", a later step
    with page.expect_navigation():
        page.locator("select[name=step]").select_option("waiting")
    assert db.get_task(conn, 3)["step"] == "waiting" and page.url == live + "/tasks/3?embed=overlay"
    assert page.get_by_role("button", name="Save").count() == 0 or not page.get_by_role("button", name="Save").is_visible()


def test_project_time_split_folds_out(page, live):
    """A project page shows its total time; the per-step bars only after "Show split"."""
    page.goto(live + "/projects/1?embed=overlay")
    tally = page.locator("details.tally")
    assert tally.locator("h2 b").is_visible() and not tally.locator("ul").is_visible()
    assert tally.locator(".split-toggle").evaluate("e => getComputedStyle(e, '::after').content") == '"Show split"'
    tally.locator("summary").click()
    assert tally.locator("ul").is_visible()
    assert tally.locator(".split-toggle").evaluate("e => getComputedStyle(e, '::after').content") == '"Hide split"'
    assert page.get_by_text("Show completed").is_visible()  # completed steps start hidden


def test_a_select_saves_typed_notes_before_it_submits(page, live, conn):
    """The server leaves title and notes to autosave, so picking a status mustn't lose what was just typed."""
    page.goto(live + "/projects/1?embed=overlay")
    page.locator("form[data-autosave] .md-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(" typed")
    with page.expect_navigation():
        page.get_by_label("Status").select_option("someday")
    p = db.get_project(conn, 1)
    assert (p["status"], p["notes"]) == ("someday", "tiles\nand grout typed")


def test_enter_in_the_title_saves_without_reloading(page, live, conn):
    page.goto(live + "/reference/1?embed=overlay")
    page.evaluate("window.stayed = true")
    page.locator("form[data-autosave] input[name=title]").fill("Plumber Sam")
    page.locator("form[data-autosave] input[name=title]").press("Enter")
    page.wait_for_function("document.querySelector('.autosave').textContent === 'Saved'")
    assert page.evaluate("window.stayed") and db.get_reference(conn, 1)["title"] == "Plumber Sam"


def test_tick_off_refreshes_the_nav_and_keeps_the_tab_lit(page, live):
    page.goto(live + "/next")
    with page.expect_response(lambda r: r.url.endswith("/nav")):
        row(page, "measure wall").locator("form.done button").click()
    assert page.locator("nav a.on").get_attribute("href") == "/next"


# --- notes toolbar and checklists; someday / maybe --------------------------------------------------------------

def notes_text(page):
    return page.locator("textarea[name=notes]").input_value().replace("\r\n", "\n")


def test_notes_toolbar_shows_while_editing(page, live, conn):
    pid = db.add_reference(conn, "Tools", notes="abc")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    tools = page.locator(".md-tools")
    assert tools.evaluate("e => getComputedStyle(e).opacity") == "0"
    page.locator(".md-editor").click()
    page.wait_for_function("getComputedStyle(document.querySelector('.md-tools')).opacity === '1'")
    assert tools.locator("[data-tool=untick]").is_hidden()  # nothing ticked


def test_notes_toolbar_formats(page, live, conn):
    pid = db.add_reference(conn, "Tools", notes="milk")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed, tool = page.locator(".md-editor"), lambda k: page.locator(f".md-tools [data-tool={k}]").click()
    ed.click()
    page.keyboard.press("ControlOrMeta+a")
    tool("bold")
    assert notes_text(page) == "**milk**"
    tool("bold")  # off again
    assert notes_text(page) == "milk"
    page.keyboard.press("ControlOrMeta+i")
    assert notes_text(page) == "_milk_"
    page.keyboard.press("ControlOrMeta+z")
    assert notes_text(page) == "milk"
    tool("check")
    assert notes_text(page) == "- [ ] milk" and ed.locator(".box").count() == 1
    tool("bullet")  # a checklist line becomes a plain bullet
    assert notes_text(page) == "- milk"
    tool("heading")
    assert notes_text(page) == "# milk"
    page.keyboard.press("ControlOrMeta+a")
    tool("link")
    assert notes_text(page) == "[# milk]()"
    page.keyboard.type("https://x.com")
    assert notes_text(page) == "[# milk](https://x.com)"
    # Indent and outdent, on a list.
    pid = db.add_reference(conn, "Tools", notes="- a\n- b\n\n- c")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed.click()
    page.keyboard.press("ControlOrMeta+End")
    tool("indent")
    assert notes_text(page) == "- a\n- b\n\n  - c" and ed.locator(".ind").count() == 1
    tool("indent")
    page.keyboard.type("d")  # the caret stayed at the line's end
    assert notes_text(page) == "- a\n- b\n\n    - cd"
    page.keyboard.press("Enter")  # a new item keeps the indent
    page.keyboard.type("e")
    assert notes_text(page) == "- a\n- b\n\n    - cd\n    - e"
    for _ in range(3):  # the third has nothing left to take off
        tool("outdent")
    assert notes_text(page) == "- a\n- b\n\n    - cd\n- e"
    page.keyboard.press("ControlOrMeta+a")
    tool("indent")  # every selected line but the blank one
    assert notes_text(page) == "  - a\n  - b\n\n      - cd\n  - e"
    page.keyboard.press("ControlOrMeta+z")
    assert notes_text(page) == "- a\n- b\n\n    - cd\n- e"


def test_checklists_tick_continue_and_reset(page, live, conn):
    pid = db.add_reference(conn, "Packing", notes="- [ ] passport")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed = page.locator(".md-editor")
    ed.locator(".box").click()  # ticks without focusing the editor
    assert notes_text(page) == "- [x] passport" and ed.locator(".box.on").count() == 1
    with page.expect_response(lambda r: "/autosave/" in r.url):
        page.locator("input.big").focus()
    assert db.get_reference(conn, pid)["notes"] == "- [x] passport"
    ed.click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.press("Enter")  # a new item, unticked
    page.keyboard.type("charger")
    assert notes_text(page) == "- [x] passport\n- [ ] charger"
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")  # Enter on an empty item ends the list
    assert notes_text(page) == "- [x] passport\n- [ ] charger\n"
    page.locator(".md-tools [data-tool=untick]").click()
    assert notes_text(page) == "- [ ] passport\n- [ ] charger\n"
    assert page.locator(".md-tools [data-tool=untick]").is_hidden()
    # ⌘Enter cycles a line, or every selected line, through the list states.
    pid = db.add_reference(conn, "Tools", notes="milk\n\n  - [x] eggs\n1. tea")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed.click()
    page.keyboard.press("ControlOrMeta+Home")
    page.keyboard.press("End")
    cycle = lambda: page.keyboard.press("ControlOrMeta+Enter")
    states = ["- milk", "- [ ] milk", "- [x] milk", "milk"]
    for want in states:  # one line: plain → bullet → box → ticked → plain; the page doesn't submit
        cycle()
        assert notes_text(page).split("\n")[0] == want
    page.keyboard.type("!")  # the caret stayed at the line's end
    assert notes_text(page).startswith("milk!\n")
    page.keyboard.press("ControlOrMeta+a")
    cycle()  # every line takes the first one's next state, keeping its indent and number; blank lines stay
    assert notes_text(page) == "- milk!\n\n  - eggs\n1. tea"
    cycle()
    assert notes_text(page) == "- [ ] milk!\n\n  - [ ] eggs\n1. [ ] tea"
    cycle()
    cycle()
    assert notes_text(page) == "milk!\n\n  eggs\ntea"
    page.keyboard.press("ControlOrMeta+z")
    assert notes_text(page) == "- [x] milk!\n\n  - [x] eggs\n1. [x] tea"


def test_dragging_an_inbox_row_onto_someday(page, live, conn):
    page.goto(live + "/inbox")
    with page.expect_response(lambda r: r.url.endswith("/tasks/move")) as saved:
        drag_to_panel(page, row(page, "inbox item"), '[data-project="someday"]')
    assert saved.value.status == 204
    page.wait_for_function("![...document.querySelectorAll('li.task a.title')].some(a => a.textContent === 'inbox item')")
    t = db.someday_tasks(conn)[0]  # on top: the drag's way over the Inbox's rows didn't save an order
    assert (t["notes"], t["ground"], t["step"]) == ("from email", 1, "someday")


def test_dragging_a_someday_task_onto_h0_commits_to_it(page, live, conn):
    tid = next(t["id"] for t in db.someday_tasks(conn) if t["title"] == "learn Italian")
    page.goto(live + "/someday")
    with page.expect_response(lambda r: r.url.endswith("/tasks/move")) as saved:
        drag_to_panel(page, row(page, "learn Italian"), '[data-project="h0"]')
    assert saved.value.status == 204
    page.wait_for_function("![...document.querySelectorAll('li.task a.title')].some(a => a.textContent === 'learn Italian')")
    assert (db.get_task(conn, tid)["step"], db.get_task(conn, tid)["due_date"]) == ("next", None)


def test_a_someday_projects_tickler_box(page, live, conn):
    page.goto(live + "/projects/4?embed=overlay")  # "Someday proj"
    box = page.locator(".title-line input[name=tickle_on]")  # a quiet control on the title line
    assert not page.locator(".tickler .hint").is_visible()  # its reading shows only while editing
    box.fill("nov 15")
    page.wait_for_function("document.querySelector('.tickler .hint.when').textContent === '→ Sun Nov 15'")
    assert page.locator(".tickler .hint").is_visible()
    with page.expect_navigation():
        box.press("Tab")  # leaving the box saves it
    assert db.get_project(conn, 4)["tickle_on"] == "2026-11-15"


def test_enter_in_a_tickler_box_saves_once(page, live, conn):
    page.goto(live + "/projects/4?embed=overlay")
    posts = []
    page.on("request", lambda r: r.method == "POST" and "/projects/4?" in r.url and posts.append(r))
    page.locator("input[name=tickle_on]").fill("nov 15")
    with page.expect_navigation():
        page.locator("input[name=tickle_on]").press("Enter")
    page.wait_for_timeout(300)
    assert len(posts) == 1 and db.get_project(conn, 4)["tickle_on"] == "2026-11-15"


PASTE = """([html, plain]) => {
  const dt = new DataTransfer();
  if (html) dt.setData("text/html", html);
  dt.setData("text/plain", plain);
  document.querySelector(".md-editor").dispatchEvent(new InputEvent("beforeinput",
    {inputType: "insertFromPaste", dataTransfer: dt, bubbles: true, cancelable: true}));
}"""


def test_notes_paste_rich_text_as_markdown(page, live, conn):
    pid = db.add_reference(conn, "Paste", notes="")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    page.locator(".md-editor").click()
    html = ('<meta charset="utf-8"><h2>Plan</h2><p>Some <b>bold </b>and <em>italic</em>, '
            '<a href="https://example.com/a b">a <b>link</b></a>, <code>x</code>, <s>gone</s>.</p>'
            '<ul><li>one</li><li><p>two</p><ol><li>nested</li></ol></li></ul>'
            '<ul><li><input type="checkbox" checked>done</li></ul>'
            '<blockquote><p>quoted<br>twice</p></blockquote><hr><pre><code>a  b\n  c</code></pre>'
            '<p><b style="font-weight:normal"><span style="font-weight:700">Docs</span> '
            '<span style="font-style:italic">style</span></b></p><p><br></p>'
            '<p><a href="javascript:alert(1)">bad</a> <a href="https://x.org">https://x.org</a></p>')
    page.evaluate(PASTE, [html, "ignored"])
    assert notes_text(page) == (
        "## Plan\nSome **bold** and _italic_, [a link](https://example.com/a%20b), `x`, ~~gone~~.\n"
        "- one\n- two\n  1. nested\n- [x] done\n> quoted\n> twice\n---\n```\na  b\n  c\n```\n"
        "**Docs** _style_\nbad https://x.org")
    page.evaluate(PASTE, ['<div style="color:red">  keep  spacing</div>', "\n  keep  spacing"])
    assert notes_text(page).endswith("https://x.org\n  keep  spacing")  # no rich markup: the plain text
    page.evaluate(PASTE, [None, " https://example.com/?q=1"])  # no HTML at all (a URL from the address bar)
    assert notes_text(page).endswith("  keep  spacing https://example.com/?q=1")
    copied = page.evaluate("""() => {
      const ed = document.querySelector(".md-editor"), r = document.createRange();
      r.selectNodeContents(ed.querySelector(".h"));
      getSelection().removeAllRanges(); getSelection().addRange(r);
      const e = new ClipboardEvent("copy", {clipboardData: new DataTransfer(), bubbles: true, cancelable: true});
      ed.dispatchEvent(e);
      return [e.clipboardData.getData("text/plain"), e.clipboardData.getData("text/html")];
    }""")
    assert copied == ["## Plan", ""]  # copying gives the Markdown, so pasting it back adds no marks


def test_notes_editor_edge_cases(page, live, conn):
    pid = db.add_reference(conn, "Edges", notes="")
    page.goto(live + f"/reference/{pid}?embed=overlay")
    ed = page.locator(".md-editor")
    ed.click()
    page.keyboard.type("- - -")
    page.keyboard.press("Enter")  # a rule, not a list
    assert notes_text(page) == "- - -\n"
    page.keyboard.type("- [ ]")  # a box with nothing after it yet
    assert ed.locator(".box").count() == 1
    page.keyboard.press("Enter")
    page.keyboard.press("Enter")
    page.keyboard.type("__bold__")
    for _ in range(2):
        page.keyboard.press("ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")
    page.keyboard.press("Shift+ArrowLeft")  # "bold" selected
    page.keyboard.press("ControlOrMeta+i")  # wraps it in italics; doesn't eat bold's underscore
    assert notes_text(page).endswith("___bold___")


def test_overlay_arrows_walk_the_list_it_was_opened_from(page, live, conn):
    """‹ › go through the list the task was opened from, even after it's filed elsewhere; ← → too, unless typing."""
    orphan = conn.execute("SELECT id FROM tasks WHERE title = 'legacy orphan'").fetchone()[0]
    page.goto(live + "/inbox")
    dialog, frame = page.locator("dialog.overlay"), page.frame_locator("dialog.overlay iframe")
    prev, nxt = dialog.get_by_role("button", name="Previous"), dialog.get_by_role("button", name="Next")
    title = frame.locator("main input[name=title]")
    row(page, "legacy orphan").locator(".title").click()  # an Inbox row opens its task page
    expect(title).to_have_value("legacy orphan")
    frame.locator("main select[name=project_id]").select_option("1")  # filed by hand: the frame reloads, a project's step
    frame.locator("main select[name=step]").wait_for()
    assert db.get_task(conn, orphan)["project_id"] == 1
    nxt.click()  # on down the Inbox, not the project's steps
    expect(title).to_have_value("inbox item")
    expect(nxt).to_be_disabled()  # the list's end
    title.fill("inbox item, clarified")
    prev.click()  # saved on the way, and back to the one just filed
    expect(title).to_have_value("legacy orphan")
    assert db.get_task(conn, 1)["title"] == "inbox item, clarified"
    frame.locator("body").click(position={"x": 1, "y": 1})  # focus in the frame, not a field
    page.keyboard.press("ArrowRight")
    expect(title).to_have_value("inbox item, clarified")
    title.focus()
    page.keyboard.press("ArrowLeft")  # typing: the caret moves, the page doesn't
    page.wait_for_timeout(100)
    expect(title).to_have_value("inbox item, clarified")
    with page.expect_navigation():  # closing after a change redraws the list
        dialog.get_by_role("button", name="Close").click()
    assert row(page, "legacy orphan").count() == 0 and row(page, "inbox item, clarified").count() == 1
    # Filed to Reference: the task is gone, its item shows in the frame, and the arrows carry on without it.
    db.add_task(conn, "third item")
    page.goto(live + "/inbox")
    row(page, "inbox item, clarified").locator(".title").click()
    expect(title).to_have_value("inbox item, clarified")
    frame.get_by_label("File to").select_option("reference")
    frame.locator("main form[data-autosave*='/reference/']").wait_for()
    assert db.get_task(conn, 1) is None
    prev.click()  # past the gone one, from where it stood: the item above it
    expect(title).to_have_value("third item")
    expect(nxt).to_be_disabled()
    # Next: its one list, in page order.
    page.goto(live + "/next")
    titles = [a.strip() for a in page.locator("main li.task .name a.title").all_text_contents()]
    assert len(titles) >= 2
    page.locator("main li.task .name a.title").first.evaluate("a => a.click()")
    expect(prev).to_be_disabled()
    for t in titles:
        expect(title).to_have_value(t.split(": ")[-1])  # a row shows its category before the title
        if t != titles[-1]:
            nxt.click()
    expect(nxt).to_be_disabled()
    dialog.get_by_role("button", name="Close").click()
    # A project has no arrows; a step opened over its project's overlay walks the project's steps.
    page.goto(live + "/projects/active?project=1")
    dialog.get_by_role("button", name="Close").wait_for()
    assert dialog.locator(".walk:visible").count() == 0
    page.frame_locator("dialog.overlay iframe").locator("main li.task .name a.title").first.click()
    expect(dialog).to_have_count(2)
    expect(dialog.last.get_by_role("button", name="Next")).to_be_enabled()
    # Opened by URL, as a reminder's tap does: the same list, and the URL stops naming it once it's moved on.
    first = db.project_tasks(conn, 1)[0]["id"]
    page.goto(live + f"/projects/active?project=1&task={first}")
    dialog.last.get_by_role("button", name="Next").click()
    page.wait_for_url(lambda u: f"task={first}" not in u and "project=1" in u)


def test_a_disabled_repeat_box_isnt_saved_with_the_other_fields(page, live, conn):
    """A repeating someday task's Tickler cleared: the dormant rule mustn't bring a date of today with it."""
    tid = db.add_task(conn, "idea", ground=1, step="someday", recur="weekly", due_date="2026-12-01")
    conn.commit()
    page.goto(live + f"/someday?task={tid}")
    due = page.frame_locator("dialog.overlay iframe").locator("main input[name=due_date]")
    due.fill("")
    with page.expect_response(lambda r: r.request.method == "POST" and f"/tasks/{tid}" in r.url):
        due.press("Tab")
    t = db.get_task(conn, tid)
    assert (t["due_date"], t["recur"]) == (None, "weekly")


def test_an_areas_balance_slider_saves_in_place_on_its_page_and_h2(page, live, conn):
    area = db.horizons(conn)["area"][0]["id"]
    page.goto(live + f"/horizons/{area}?embed=overlay")
    slider = page.locator(".balance input")
    page.evaluate("window.stay = true")  # gone if the page reloads
    assert db.horizons(conn)["area"][0]["balance"] is None
    with page.expect_response(lambda r: r.url.endswith("/balance")):
        slider.click()  # its middle, where an unset one already sits: that sets it
    assert page.locator(".balance output").text_content() == "Balanced"
    assert db.horizons(conn)["area"][0]["balance"] == 0
    slider.focus()
    for _ in range(2):
        with page.expect_response(lambda r: r.url.endswith("/balance")):
            slider.press("ArrowLeft")
    assert page.locator(".balance output").text_content() == "Too little"
    assert db.horizons(conn)["area"][0]["balance"] == -2 and page.evaluate("window.stay")
    page.goto(live + "/horizons?level=area")  # H2's sliders move it too
    page.evaluate("window.stay = true")
    with page.expect_response(lambda r: r.url.endswith("/balance")):
        row(page, "Home").locator(".balance input").press("ArrowRight")
    assert row(page, "Home").locator(".balance output").text_content() == "A bit too little"
    assert db.horizons(conn)["area"][0]["balance"] == -1 and page.evaluate("window.stay")


def contexts_row(page, name):
    return page.locator(f".context-list li:has(input[name=context_name][value={name}])")


def test_site_info_filters_every_working_list_but_not_the_inbox(page, live, conn):
    page.goto(live + "/calendar?m=2026-10")
    assert page.locator(".ev", has_text="call mum").count() == 1
    page.evaluate("window.stay = 1")  # a filter redraws in place: the page is never reloaded
    page.keyboard.press("?")
    assert page.locator(".help-dialog h2").text_content() == "Site info"
    tick = lambda name: contexts_row(page, name).locator("input[type=checkbox]")
    tick("morning").check()  # at once, under Site info, which stays open on the same tick
    page.locator(".ev", has_text="call mum").wait_for(state="detached")
    assert page.locator(".help-dialog").evaluate("d => d.open") and tick("morning").is_checked()
    assert tick("morning").evaluate("c => c === document.activeElement")
    assert page.locator(".ev", has_text="buy tiles").count() == 1
    assert page.locator(".ctx-open").text_content() == "@morning"
    tick("errands").check()  # another: any of them
    page.wait_for_function("document.querySelector('.ctx-open').textContent.includes('@errands')")
    assert page.locator(".help-dialog").evaluate("d => d.open")
    assert tick("morning").is_checked() and tick("errands").is_checked()
    page.locator(".help-dialog .ctx-clear").click()  # "Show all" too
    page.locator(".ev", has_text="call mum").wait_for()
    assert page.locator(".help-dialog").evaluate("d => d.open")
    tick("morning").check()
    page.locator(".ev", has_text="call mum").wait_for(state="detached")
    page.keyboard.press("Escape")  # closing it reloads nothing more, and it stays closed
    page.locator(".cal .day").first.click()  # the redrawn grid still works: a day number opens its add dialog
    assert page.locator(".day-dialog [name=due_date]").input_value() == page.locator(".cal .day").first.get_attribute("data-date")
    page.keyboard.press("Escape")
    assert page.evaluate("window.stay") == 1
    page.reload()
    assert not page.locator(".help-dialog").evaluate("d => d.open")
    page.goto(live + "/next")
    assert page.locator(".ctx-open").text_content() == "@morning"
    assert page.locator("main li.task", has_text="sort mail").count() == 0
    assert row(page, "measure wall").locator(".ctx").text_content() == "@morning"
    page.goto(live + "/someday")
    assert row(page, "learn Italian").count() == 1
    page.goto(live + "/inbox")
    assert row(page, "inbox item").count() == 1 and page.locator(".ctx-open").count() == 0
    page.goto(live + "/waiting")  # the filter leaves nothing here, on load
    assert row(page, "quote from Bob").count() == 0
    page.evaluate("window.stay = 1")
    page.locator(".ctx-filter .ctx-clear").click()  # the header's × clears it, everywhere
    row(page, "quote from Bob").wait_for()
    assert page.locator(".ctx-open").count() == 0 and page.evaluate("window.stay") == 1
    # The rows the filter brought back drag like any: to reorder, and out to the drop panel.
    with page.expect_response(lambda r: r.url.endswith("/tasks/order")):
        row(page, "quote from Bob").locator(".grip").drag_to(row(page, "parcel from Ann"), target_position={"x": 40, "y": 2})
    assert page.locator("main li.task a.title").first.text_content() == "quote from Bob"
    drag_to_panel(page, row(page, "parcel from Ann"), '[data-project="someday"]')
    page.wait_for_function("!document.body.classList.contains('dragging-tasks')")
    page.locator(".drop-panel").wait_for(state="hidden")
    assert conn.execute("SELECT step FROM tasks WHERE title = 'parcel from Ann'").fetchone()[0] == "someday"
    page.keyboard.press("?")
    assert not contexts_row(page, "morning").locator("input[type=checkbox]").is_checked()
    page.keyboard.press("Escape")


def test_site_info_renames_deletes_and_reorders_contexts(page, live, conn):
    answers = []
    page.on("dialog", lambda d: (answers.append(d.message), d.accept() if "Delete" in d.message else d.dismiss()))
    page.goto(live + "/next")
    page.keyboard.press("?")
    assert [x["name"] for x in db.contexts(conn)] == ["gone", "errands", "morning"]
    ids = {x["name"]: x["id"] for x in db.contexts(conn)}
    morning = page.locator(f'.context-list li[data-id="{ids["morning"]}"]')  # by id: its name is about to change
    with page.expect_response(lambda r: r.url.endswith("/contexts/order")):
        morning.locator(".grip").drag_to(contexts_row(page, "gone"))  # a real drag on the grip
    assert [x["name"] for x in db.contexts(conn)][0] == "morning"
    # Rename, with the Enter key; the rows follow once the overlay closes.
    box = morning.locator("input[name=context_name]")
    box.fill("deep work")
    with page.expect_response(lambda r: r.url.endswith("/rename")):
        box.press("Enter")
    sync_api.expect(box).to_have_value("deep-work")
    with page.expect_navigation():
        page.keyboard.press("Escape")
    assert row(page, "measure wall").locator(".ctx").text_content() == "@deep-work"
    # Closed while still typing, the name saves as the box is left, and the page redraws once it has.
    page.keyboard.press("?")
    box = page.locator(f'.context-list li[data-id="{ids["morning"]}"] input[name=context_name]')
    box.fill("focus")
    with page.expect_navigation():
        page.keyboard.press("Escape")
    assert row(page, "measure wall").locator(".ctx").text_content() == "@focus"
    page.keyboard.press("?")
    box.fill("deep work")
    with page.expect_response(lambda r: r.url.endswith("/rename")):
        box.press("Enter")
    # Onto another's name: it asks; turned down, nothing changes.
    page.keyboard.press("?")
    morning = page.locator(f'.context-list li[data-id="{ids["morning"]}"]')
    box = morning.locator("input[name=context_name]")
    box.fill("errands")
    box.press("Enter")
    sync_api.expect(box).to_have_value("deep-work")
    assert "Merge" in answers[-1]
    # Delete asks first; its tasks stay.
    with page.expect_response(lambda r: r.url.endswith("/delete")):
        morning.hover()
        morning.locator(".delete").click()
    assert "Delete @deep-work" in answers[-1]
    assert [x["name"] for x in db.contexts(conn)] == ["gone", "errands"]
    with page.expect_navigation():
        page.keyboard.press("Escape")
    assert row(page, "measure wall").locator(".ctx").count() == 0


def test_at_offers_contexts_and_the_task_page_picks_them(page, live, conn):
    page.goto(live + "/inbox")
    title = page.locator("main form.capture input[name=title]")
    title.click()
    title.type("ring vet @mo")
    offer = page.locator(".ctx-suggest:not([hidden]) a")
    sync_api.expect(offer).to_have_text(["@morning"])
    title.press("Enter")  # takes the offer, doesn't add the task
    assert title.input_value() == "ring vet @morning "
    assert offer.count() == 0
    with page.expect_navigation():
        title.press("Enter")
    t = next(t for t in db.inbox_tasks(conn) if t["title"] == "ring vet")
    assert t["contexts"] == "morning"
    title = page.locator("main form.capture input[name=title]")
    title.type("x @new")  # nothing to offer: Enter adds it
    assert offer.count() == 0
    title.fill("y @errands")
    title.dispatch_event("input")
    assert offer.count() == 0  # typed in full
    # In the capture dialog, over its top layer, right under the box; Esc leaves what's typed, in the open dialog.
    page.goto(live + "/projects/active")
    page.keyboard.press("/")
    page.keyboard.press("Tab")  # from the category box to the title
    page.keyboard.type("buy @")
    sync_api.expect(offer).to_have_count(3)
    box = page.locator(".capture-dialog input[name=title]").bounding_box()
    at = page.locator(".ctx-suggest").bounding_box()
    assert abs(at["y"] - (box["y"] + box["height"])) < 8 and abs(at["x"] - box["x"]) < 1
    page.keyboard.press("Escape")
    assert offer.count() == 0 and page.locator(".capture-dialog").evaluate("d => d.open")
    page.keyboard.press("Escape")
    # The task page: a click saves, without touching what's typed elsewhere; a typed name adds and ticks it.
    page.goto(live + f"/tasks/{t['id']}?embed=overlay")
    picks = page.locator("form.contexts")
    with page.expect_response(lambda r: r.url.endswith("/contexts")):
        picks.get_by_text("@errands").click()
    assert sorted(db.get_task(conn, t["id"])["contexts"].split()) == ["errands", "morning"]
    box = picks.locator("input[name=new_context]")
    box.fill("deep work")
    with page.expect_response(lambda r: r.url.endswith("/contexts")):
        box.press("Enter")
    assert "deep-work" in db.get_task(conn, t["id"])["contexts"].split()
    assert box.input_value() == "" and picks.get_by_text("@deep-work").locator("input").is_checked()


def test_a_context_typed_on_a_tasks_own_title(page, live, conn):
    """Typed on an existing task's title, a @mark comes off when the title is left, not on autosave's pause."""
    tid = db.add_task(conn, "Journal")
    page.goto(live + f"/tasks/{tid}?embed=overlay")
    title = page.locator("input[name=title]")
    title.click()
    title.press("End")
    offer = page.locator(".ctx-suggest:not([hidden]) a")
    with page.expect_response(lambda r: "/autosave/" in r.url and r.request.method == "POST"):  # the half-typed text, which makes no context
        title.type(" @mor")
        sync_api.expect(offer).to_have_text(["@morning"])
    assert [x["name"] for x in db.contexts(conn)] == ["gone", "errands", "morning"]
    title.press("Tab")  # takes the offer
    assert title.input_value() == "Journal @morning "
    with page.expect_response(lambda r: r.url.endswith("/contexts")):
        page.locator("textarea[name=notes] + .md-editor, .md-editor").first.click()  # leaves the title
    assert title.input_value() == "Journal"
    assert page.locator("form.contexts").get_by_text("@morning").locator("input").is_checked()
    assert db.get_task(conn, tid)["contexts"] == "morning"
    page.wait_for_timeout(300)
    assert db.get_task(conn, tid)["title"] == "Journal"
    # A new one, several, and with Enter; what it had stays. Words that aren't a mark stay in the title.
    title.fill("Journal @deep @quiet")
    with page.expect_response(lambda r: r.url.endswith("/contexts")):
        title.press("Enter")
    page.wait_for_load_state()
    assert sorted(db.get_task(conn, tid)["contexts"].split()) == ["deep", "morning", "quiet"]
    assert page.locator("input[name=title]").input_value() == "Journal"
    page.wait_for_timeout(300)
    assert db.get_task(conn, tid)["title"] == "Journal"


def test_dragging_a_project_onto_a_nav_tab_moves_it(page, live, conn):
    """A project row dropped on the nav's Active, Waiting or Someday (projects) tab gets that status; its own tab is no target."""
    pid = db.add_project(conn, "Another dream", status="someday")
    page.goto(live + "/projects/someday?tickled=1")
    tab = lambda s: page.locator(f'nav a[data-status="{s}"]')
    with page.expect_response(lambda r: r.url.endswith(f"/projects/{pid}/move")) as saved:
        row(page, "Another dream").locator(":scope > .grip").drag_to(tab("active"))
    assert saved.value.status == 204
    page.wait_for_function(f"!document.querySelector('ul[data-reorder] > li.task[data-id=\"{pid}\"]')")
    assert db.get_project(conn, pid)["status"] == "active"
    assert db.projects(conn, "active")[0]["id"] == pid  # on top of its new list
    assert [p["title"] for p in db.projects(conn, "someday")] == ["Someday proj"]  # the rest kept their order
    row(page, "Someday proj").locator(":scope > .grip").drag_to(tab("someday"))  # its own tab: nothing happens
    assert db.projects(conn, "someday")[0]["title"] == "Someday proj"
    assert row(page, "Someday proj").count() == 1


def test_the_category_box_offers_categories(page, live, conn):
    """Typing in a task's category box offers every category used (closed tasks' too), by a word's start; Enter takes
    one and moves on to the title. On the task page it saves in the background, as it's left."""
    page.goto(live + "/inbox")
    cat = page.locator("main form.capture input[name=category]")
    page.locator("main form.capture input[name=title]").click()  # the idle line opens in the category box
    cat.type("m")  # a later word's start, then a name containing it
    offer = page.locator(".ctx-suggest:not([hidden]) a")
    sync_api.expect(offer).to_have_text(["Discrete Math", "Home"])
    cat.press("Enter")  # takes the first, adds nothing
    assert cat.input_value() == "Discrete Math" and offer.count() == 0
    title = page.locator("main form.capture input[name=title]")
    assert title.evaluate("el => el === document.activeElement")
    title.type("grade hw")
    with page.expect_navigation():
        title.press("Enter")
    t = db.inbox_tasks(conn)[0]
    assert t["label"] == "Discrete Math: grade hw"
    assert page.locator("main li.task .title", has_text="Discrete Math: grade hw").count() == 1
    # Capture starts in the category box; Tab goes on to the title. A new one is offered from then on (the dialog
    # adds in the background, no reload).
    page.goto(live + "/projects/active")
    page.keyboard.press("/")
    page.keyboard.type("prob stats")  # stored, and offered from then on, title-cased
    page.keyboard.press("Tab")
    page.keyboard.type("lecture")
    with page.expect_response(lambda r: r.url.endswith("/capture")):
        page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('.capture-dialog').open")
    assert db.inbox_tasks(conn)[0]["label"] == "Prob Stats: lecture"
    page.click(".add-inbox")  # opened again, it starts in the category box too
    page.keyboard.type("st")
    sync_api.expect(offer).to_have_text(["Prob Stats"])
    page.keyboard.press("Escape")
    # The task page.
    page.goto(live + f"/tasks/{t['id']}?embed=overlay")
    cat = page.locator("input[name=category]")
    assert cat.input_value() == "Discrete Math"
    cat.fill("home")
    with page.expect_response(lambda r: r.url.endswith(f"/tasks/{t['id']}?embed=overlay") and r.request.method == "POST"):
        cat.press("Enter")
    sync_api.expect(cat).to_have_value("Home")  # the spelling it's used with
    assert db.get_task(conn, t["id"])["label"] == "Home: grade hw"


def test_an_empty_category_box_shows_on_the_task_page(page, live, conn):
    """A task with no category still shows its category box before the title, so one can be added."""
    t = next(t for t in db.inbox_tasks(conn) if not t["category"])
    page.goto(live + f"/tasks/{t['id']}?embed=overlay")
    cat = page.locator(".title-line input[name=category]")
    expect(cat).to_be_visible()
    expect(cat).to_have_attribute("placeholder", "Category")
    assert cat.bounding_box()["width"] > 40


def test_titles_read_on_a_phone(browser, live, conn):
    """At phone width every list row's title gets most of a line to read across, and no page scrolls sideways.
    Only reading matters there (the phone is for capture), so a row's controls may wrap below its title."""
    kitchen = next(p["id"] for p in db.projects(conn) if p["title"] == "Kitchen")
    pg = browser.new_page(viewport={"width": 375, "height": 800}, is_mobile=True, has_touch=True)
    pg.goto(live + "/")
    paths = pg.eval_on_selector_all("nav a", "els => els.map(e => e.getAttribute('href'))")
    for path in paths + [f"/projects/{kitchen}", f"/projects/{kitchen}?done=1", "/next?done=1", "/search?q=tiles"]:
        pg.goto(live + path)
        pg.eval_on_selector_all("li.task .peek", "bs => bs.forEach(b => b.click())")  # a project row's steps too
        assert pg.evaluate("document.documentElement.scrollWidth <= innerWidth"), path
        narrow = pg.eval_on_selector_all(
            "li.task > :is(.title, .name), li.task .steps a",
            """els => els.filter(e => e.offsetParent && e.getBoundingClientRect().width < 200)
                         .map(e => e.textContent.trim() + ' ' + Math.round(e.getBoundingClientRect().width))""")
        assert narrow == [], path
    for path in [f"/projects/{kitchen}", "/tasks/1"]:  # a record opens in its overlay: its title box across it
        pg.goto(live + path)
        pg.wait_for_selector("dialog.overlay[open]")
        big = pg.locator("dialog.overlay").last.frame_locator("iframe").locator("input.big")
        assert big.evaluate("e => e.getBoundingClientRect().width") >= 250, path
    # The add line in use: its title and category are framed boxes of their own, wherever they wrap to.
    pg.goto(live + "/inbox")
    pg.focus("main > form.capture input[name=title]")
    pg.keyboard.type("Buy milk")
    pg.locator("main > form.capture input[name=title]").blur()
    borders = pg.eval_on_selector_all("main > form.capture :is(input.category, input[name=title])",
                                      "els => els.map(e => getComputedStyle(e).borderTopColor)")
    assert len(borders) == 2 and "rgba(0, 0, 0, 0)" not in borders, borders
    pg.close()
