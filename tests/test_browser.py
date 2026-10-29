"""Drive the real JS in real Chromium. Slow, so excluded from the default run: `uv run pytest -m browser`.

Run these when a change touches templates/, static/ or the JS in base.html. The Stop hook and pre-commit
hook do that automatically. Everything else in the suite checks only the HTML contract with the JS.
"""
import threading
from datetime import datetime, timedelta

import pytest
from werkzeug.serving import make_server

import db
from app import app, local_input
from conftest import NOW, seed

pytestmark = pytest.mark.browser
sync_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        b = p.chromium.launch()
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


def test_finishing_a_projects_next_action_stays_on_the_page(page, live):
    page.goto(live + "/next")
    row(page, "buy tiles").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.url == live + "/next"


def test_done_then_project_opens_the_project_to_pick_next(page, live, conn):
    page.goto(live + "/next")
    tile = row(page, "buy tiles")
    tile.hover()
    tile.locator("form.then button").click()
    page.wait_for_url("**/projects/*")
    assert page.get_by_role("button", name="Make next").count() >= 1  # "lay tiles" can take over
    assert db.get_task(conn, 2)["state"] == "done"


def test_plus_inbox_dialog_captures(page, live, conn):
    page.goto(live + "/next")
    page.click(".add-inbox")
    assert page.locator(".capture-dialog").evaluate("d => d.open")
    page.fill(".capture-dialog input[name=title]", "from the dialog")
    with page.expect_navigation():
        page.keyboard.press("Enter")
    assert any(t["title"] == "from the dialog" for t in db.inbox_tasks(conn))


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
    field = page.locator(".date-field", has=page.locator("input[name=due_date]"))
    field.locator(".pick").click()  # showPicker() needs this real click's user activation
    field.locator(".picker").evaluate(
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


def test_task_page_timer_is_by_the_title_and_ticks(page, live):
    page.clock.install(time=NOW)
    page.goto(live + "/tasks/7")  # "call mum", running
    timer = page.locator("header.page .task-timer")
    stop, title = timer.get_by_role("button", name="Stop the timer"), page.locator("input.big")
    assert stop.is_visible() and stop.bounding_box()["y"] < title.bounding_box()["y"]
    before = timer.locator(".clock").inner_text()
    page.clock.run_for(3000)
    assert timer.locator(".clock").inner_text() != before
    page.goto(live + "/tasks/2")  # "buy tiles", not running
    with page.expect_navigation():
        page.locator("header.page .task-timer").get_by_role("button", name="Start the timer").click()
    assert page.locator("header.page .task-timer").get_by_role("button", name="Stop the timer").is_visible()


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


def test_hidden_buttons_stay_hidden(page, live):
    page.goto(live + "/timer")
    assert page.locator("form.entry button[hidden]").count()
    assert not page.locator("form.entry button[hidden]").first.is_visible()


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
    page.locator(".theme-toggle").click()
    assert bg() == light


def test_drag_reorders_project_steps(page, live, conn):
    page.goto(live + "/projects/1")
    titles = lambda: page.locator("ul.tasks li.task a.title").all_inner_texts()
    assert titles() == ["buy tiles", "quote from Bob", "lay tiles"]
    with page.expect_response(lambda r: r.url.endswith("/order")) as saved:
        row(page, "lay tiles").locator(".grip").drag_to(row(page, "quote from Bob"), target_position={"x": 5, "y": 2})
    assert saved.value.status == 204
    assert titles() == ["buy tiles", "lay tiles", "quote from Bob"]
    assert [t["title"] for t in db.project_tasks(conn, 1)] == ["buy tiles", "lay tiles", "quote from Bob"]
    page.reload()
    assert titles() == ["buy tiles", "lay tiles", "quote from Bob"]
    # Nothing goes above the next action ("buy tiles").
    with page.expect_response(lambda r: r.url.endswith("/order")):
        row(page, "quote from Bob").locator(".grip").drag_to(row(page, "buy tiles"), target_position={"x": 5, "y": 2})
    assert titles() == ["buy tiles", "quote from Bob", "lay tiles"]


def test_drag_reorders_someday_projects(page, live, conn):
    db.add_project(conn, "Another dream", status="someday")  # Projects shares the code; this covers both
    page.goto(live + "/someday")
    page.locator("button.peek-all").click()  # open steps mustn't confuse the drag
    titles = lambda: page.locator("ul.tasks > li.task > a.title").all_inner_texts()
    assert titles() == ["Another dream", "Someday proj"]
    with page.expect_response(lambda r: r.url.endswith("/order/someday")) as saved:
        row(page, "Someday proj").locator(".grip").drag_to(row(page, "Another dream"), target_position={"x": 40, "y": 4})
    assert saved.value.status == 204
    assert [p["title"] for p in db.projects(conn, "someday")] == ["Someday proj", "Another dream"]
    page.reload()
    assert titles() == ["Someday proj", "Another dream"]


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


def test_dragging_a_calendar_task_to_another_day_moves_its_due_date(page, live, conn):
    page.goto(live + "/calendar?m=2026-10")
    ev = page.locator(".cal .ev", has_text="buy tiles")  # due Oct 1, overdue
    assert "overdue" in ev.get_attribute("class")
    target = page.locator('.cal td', has=page.locator('.day[data-date="2026-10-29"]'))
    with page.expect_response(lambda r: r.url.endswith("/due")) as saved:
        ev.drag_to(target, target_position={"x": 60, "y": 15})  # top of the cell, clear of the timer pill
    assert saved.value.status == 204
    assert target.locator(".ev", has_text="buy tiles").count() == 1  # moved in place, no reload
    assert "overdue" not in ev.get_attribute("class")
    tid = next(t["id"] for t in db.next_list(conn) if t["title"] == "buy tiles")
    assert db.get_task(conn, tid)["due_date"] == "2026-10-29"
    page.reload()
    assert target.locator(".ev", has_text="buy tiles").count() == 1


def test_row_dates_line_up_with_or_without_a_repeat(page, live, conn):
    db.add_task(conn, "dentist", step="next", ground=1, due_date="2026-10-22")
    page.goto(live + "/h0")
    edges = {t: row(page, t).locator(".date").evaluate("e => Math.round(e.getBoundingClientRect().right)")
             for t in ("vacuum", "dentist")}
    assert edges["vacuum"] == edges["dentist"], edges


def test_project_page_columns_line_up_whatever_the_step_and_date(page, live, conn):
    pid = db.add_project(conn, "Columns")
    rows = {"n": ("next", "2026-10-20"), "wd": ("waiting", "2026-10-30"), "w": ("waiting", None),
            "ld": ("later", "2026-11-11"), "l": ("later", None)}  # next is overdue, so its date is bold
    for title, (step, due) in rows.items():
        db.add_task(conn, title, step=step, project_id=pid, due_date=due)
    page.goto(f"{live}/projects/{pid}")
    for sel, side in ((".step", "left"), (".date", "left"), (".date", "right"), (".recur", "right")):
        edges = {t: row(page, t).locator(sel).evaluate(f"e => Math.round(e.getBoundingClientRect().{side})") for t in rows}
        assert len(set(edges.values())) == 1, (sel, side, edges)


def test_question_mark_shows_shortcuts_and_letters_navigate(page, live):
    page.goto(live + "/next")
    page.keyboard.press("?")
    assert page.locator(".help-dialog").evaluate("d => d.open")
    page.keyboard.press("n")  # ignored while the overlay is open
    page.keyboard.press("Escape")
    assert not page.locator(".help-dialog").evaluate("d => d.open")
    for key, path in (("i", "/inbox"), ("w", "/waiting"), ("p", "/projects"), ("h", "/h0")):
        with page.expect_navigation():
            page.keyboard.press(key)
        assert page.url == live + path


def test_notes_render_markdown_and_save_what_you_typed(page, live, conn):
    pid = db.add_reference(conn, "Markdown", notes="# Head\nsee **bold** and https://example.com")
    page.goto(live + f"/reference/{pid}")
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
    page.get_by_role("button", name="Save", exact=True).click()
    page.wait_for_load_state()
    assert db.get_reference(conn, pid)["notes"].replace("\r\n", "\n") == \
        "# Head\nsee **bold** and https://example.com\n- *new* linen"


def test_notes_editor_undo(page, live, conn):
    pid = db.add_reference(conn, "Undo", notes="abc")
    page.goto(live + f"/reference/{pid}")
    ed = page.locator(".md-editor")
    ed.click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type("def")
    assert ed.text_content() == "abcdef"
    page.keyboard.press("ControlOrMeta+z")
    assert ed.text_content() == "abc"
    assert page.locator("textarea[name=notes]").input_value() == "abc"


def test_due_bands_fold_and_remember(page, live, conn):
    db.add_task(conn, "wk", step="next", ground=1, due_date="2026-10-25")
    page.goto(live + "/h0")
    assert not row(page, "wk").is_visible()  # This week starts folded
    page.locator("details[data-band=week] summary").click()
    assert row(page, "wk").is_visible()
    page.wait_for_function("localStorage.getItem('fold:/h0:week') === '1'")
    page.reload()
    assert row(page, "wk").is_visible()  # remembered
    page.locator("details[data-band=now] summary").click()
    page.goto(live + "/next")
    assert page.locator("details[data-band=now]").get_attribute("open") is not None  # per page


def test_due_bands_remember_only_clicks(page, live, conn):
    page.goto(live + "/h0")
    page.wait_for_timeout(100)  # any toggle events from parsing have fired
    assert page.evaluate("Object.keys(localStorage).filter(k => k.includes('/h0:'))") == []  # defaults aren't frozen


def test_row_dates_show_on_hover(page, live, conn):
    db.add_task(conn, "late", step="next", ground=1, due_date="2026-10-20")
    page.goto(live + "/h0")
    opacity = lambda t: row(page, t).locator(".date").evaluate("e => getComputedStyle(e).opacity")
    page.mouse.move(0, 0)
    assert opacity("vacuum") == "0" and opacity("late") == "1"  # overdue stays visible
    row(page, "vacuum").hover()
    assert opacity("vacuum") == "1"
def test_title_and_notes_save_themselves(page, live, conn):
    pid = db.add_reference(conn, "Auto", notes="abc")
    page.goto(live + f"/reference/{pid}")
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
    page.goto(live + f"/reference/{pid}")
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
    page.goto(live + f"/projects/{pid}")
    db.update_project(conn, pid, notes="new from phone")
    page.evaluate("dispatchEvent(new Event('focus'))")
    page.wait_for_function("document.querySelector('.md-editor').textContent === 'new from phone'")
    assert page.locator(".autosave").text_content() == "Updated from elsewhere"
    assert db.get_project(conn, pid)["notes"] == "new from phone"



def drag_to_panel(page, source, target):
    """The drop panel only appears once a drag starts, so drag by hand: pick up, then find the target."""
    source.hover()
    page.mouse.down()
    page.mouse.move(100, 100, steps=5)
    target = page.locator(".drop-panel " + target)
    target.wait_for(state="visible")
    target.hover()
    page.mouse.up()


def test_dragging_an_inbox_row_onto_a_project_files_it(page, live, conn):
    page.goto(live + "/inbox")
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert page.url == live + "/inbox"
    assert db.get_task(conn, 1)["project_id"] == 1
    assert row(page, "legacy orphan").count() == 1
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out


def test_dropping_an_inbox_row_on_reference_files_it_there(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="reference"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_task(conn, 1) is None
    assert [(r["title"], r["notes"]) for r in db.references(conn) if r["title"] == "inbox item"] == [("inbox item", "from email")]


def test_dropping_an_inbox_row_on_new_asks_for_the_name_in_the_panel(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="__new__"]')
    name = page.locator(".drop-panel input[name=new_project]")
    name.wait_for(state="visible")
    assert page.evaluate("document.activeElement.name") == "new_project"
    page.keyboard.type("Dragged")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_task(conn, 1)["project_title"] == "Dragged"
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out


def test_escape_cancels_naming_a_new_project(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="__new__"]')
    page.locator(".drop-panel input[name=new_project]").wait_for(state="visible")
    page.keyboard.press("Escape")
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out
    assert db.get_task(conn, 1)["inbox"] and row(page, "inbox item").count() == 1


def test_pinned_project_panel_stays_open_until_hidden(page, live, conn):
    page.goto(live + "/inbox")
    panel = page.locator(".drop-panel")
    panel.wait_for(state="hidden")
    page.locator(".pin-panel").click()
    panel.wait_for(state="visible")
    assert panel.locator("h3").all_text_contents() == ["Active", "Waiting", "Someday", "Done"]
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert panel.is_visible()  # still pinned after the drop
    page.reload()
    panel.wait_for(state="visible")  # and after a reload
    page.locator(".pin-panel").click()
    panel.wait_for(state="hidden")
    page.reload()
    assert not panel.is_visible()


def test_review_arrow_keys_move_between_steps(page, live):
    page.goto(live + "/review?step=overdue")
    page.keyboard.press("ArrowRight")
    page.wait_for_url("**/review?step=stalled")
    page.keyboard.press("ArrowLeft")
    page.wait_for_url("**/review?step=overdue")
    page.locator("a[rel=next]").click()
    page.wait_for_url("**/review?step=stalled")


def test_long_title_wraps_instead_of_pushing_buttons_down(page, live, conn):
    long = "ask AI to download all the mir publication math physics and other science books " * 2
    tid = db.add_task(conn, long.strip(), step="next", project_id=db.add_project(conn, "Mir publications archive"),
                      due_date="2026-10-22")  # today, so its band is open
    db.timer_start(conn, tid, NOW)
    page.set_viewport_size({"width": 1000, "height": 800})
    page.goto(live + "/next")
    r = page.locator("li.task", has=page.locator("form.timing"))
    mid = lambda sel: r.locator(sel).evaluate("e => { const b = e.getBoundingClientRect(); return b.left + b.width / 2 }")
    top = lambda sel: r.locator(sel).evaluate("e => e.getBoundingClientRect().top")
    title = r.locator(".title").evaluate("e => e.getBoundingClientRect()")
    assert top("form.timing") < title["bottom"]  # ■ sits beside the title, not on a line below it
    assert mid("form.timing") > title["right"]


def test_project_sits_next_to_the_task_title(page, live, conn):
    page.set_viewport_size({"width": 1000, "height": 800})
    page.goto(live + "/next")
    r = row(page, "buy tiles")
    box = lambda sel: r.locator(sel).evaluate("e => e.getBoundingClientRect().toJSON()")
    title, proj = box(".title"), box(".proj")
    assert 0 < proj["left"] - title["right"] < 30 and abs(proj["top"] - title["top"]) < 8, (title, proj)
    assert r.locator(".meta .proj").count() == 0  # not out in the details column


def test_project_counts_show_on_hover(page, live):
    page.goto(live + "/projects")
    r = page.locator("li.task", has=page.get_by_role("link", name="Kitchen", exact=True))
    opacity = lambda: r.locator(":scope > .meta").evaluate("e => getComputedStyle(e).opacity")
    page.mouse.move(0, 0)
    assert opacity() == "0"
    r.hover()
    assert opacity() == "1"


def test_project_steps_are_quiet_until_hover(page, live, conn):
    page.goto(live + "/projects/1")
    page.mouse.move(0, 0)
    opacity = lambda t, sel: row(page, t).locator(sel).evaluate("e => getComputedStyle(e).opacity")
    assert opacity("lay tiles", ".step") == "0" and opacity("lay tiles", ".make-next button") == "0"
    row(page, "lay tiles").hover()
    assert opacity("lay tiles", ".step") == "1" and opacity("lay tiles", ".make-next button") == "1"


def test_timer_button_always_shows(page, live):
    for url in ("/next", "/h0", "/projects/1", "/waiting", "/inbox"):
        page.goto(live + url)
        page.mouse.move(0, 0)
        b = page.locator("li.task form.start button").first
        assert b.evaluate("e => getComputedStyle(e).opacity") == "1", url


def test_all_clear_lists_show_a_big_check(page, live, conn):
    conn.execute("DELETE FROM time_entries"); conn.execute("DELETE FROM tasks")
    conn.execute("UPDATE projects SET status = 'active' WHERE status = 'waiting'"); conn.commit()  # nothing waiting either
    for url in ("/inbox", "/next", "/h0", "/waiting"):
        page.goto(live + url)
        e = page.locator(".empty.clear")
        assert e.count() == 1, url
        assert e.evaluate("e => getComputedStyle(e, '::before').content") == '"✓"', url
        assert e.evaluate("e => getComputedStyle(e).fontSize") == "0px", url  # the sentence stays for screen readers only
    page.goto(live + "/search?q=zzz")
    assert page.locator(".empty.clear").count() == 0  # "Nothing matches" isn't good news


def test_task_rows_have_no_lines_and_projects_are_pills(page, live, conn):
    db.add_project(conn, "Deck", status="waiting")
    page.goto(live + "/next")
    style = lambda loc, prop: loc.evaluate(f"e => getComputedStyle(e).{prop}")
    assert style(page.locator("li.task").first, "borderBottomWidth") == "0px"
    for url in ("/projects", "/someday", "/waiting", "/horizons?level=projects"):
        page.goto(live + url)
        rows = page.locator("ul.cards > li.task")
        assert rows.count() >= 1, url
        assert float(style(rows.first, "borderTopLeftRadius")[:-2]) >= 16, url
        assert style(rows.first, "borderTopWidth") == "1px", url
    page.goto(live + "/projects")
    a, b = (page.locator("ul.cards > li.task").nth(i).bounding_box() for i in (0, 1))
    assert b["y"] - (a["y"] + a["height"]) >= 4  # a gap between pills


# --- review fixes (2026-10-27) ---------------------------------------------

def test_a_failed_tick_off_reloads_instead_of_faking_it(page, live, conn):
    page.goto(live + "/next")
    page.route("**/complete", lambda r: r.fulfill(status=500, body=""))
    with page.expect_navigation():
        row(page, "buy tiles").locator("form.done button").click()
    assert row(page, "buy tiles").count() == 1 and db.get_task(conn, 2)["state"] == "open"
    page.errors[:] = [e for e in page.errors if "500" not in e]  # the faked failure itself


def test_a_failed_calendar_drag_reloads(page, live, conn):
    page.goto(live + "/calendar?m=2026-10")
    page.route("**/due", lambda r: r.fulfill(status=500, body=""))
    ev = page.locator(".cal .ev", has_text="buy tiles")
    with page.expect_navigation():
        ev.drag_to(page.locator(".cal td", has=page.locator('.day[data-date="2026-10-29"]')),
                   target_position={"x": 60, "y": 15})  # clear of the timer pill
    assert page.locator('td[data-date="2026-10-01"] .ev', has_text="buy tiles").count() == 1
    page.errors[:] = [e for e in page.errors if "500" not in e]  # the faked failure itself


def test_tick_off_updates_the_band_count_and_nav_dot(page, live):
    page.goto(live + "/h0")
    assert page.locator("details[data-band=now] summary b").inner_text() == "1"
    assert page.locator("nav a", has_text="H0").locator(".badge").count() == 1
    row(page, "vacuum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    page.wait_for_function("document.querySelector('details[data-band=now] summary b').textContent === '0'")
    page.wait_for_function("!document.querySelector('nav a[href=\"/h0\"] .badge')")


def test_capture_keys_do_nothing_while_help_is_open(page, live):
    page.goto(live + "/next")
    page.keyboard.press("?")
    page.keyboard.press("/")
    page.keyboard.press("f")
    assert not page.locator(".capture-dialog").evaluate("d => d.open")
    assert page.evaluate("document.activeElement.name") != "q"


def test_clicking_anywhere_on_a_row_opens_it(page, live):
    page.goto(live + "/next")
    r = row(page, "buy tiles")
    box = r.bounding_box()
    with page.expect_navigation():
        page.mouse.click(box["x"] + box["width"] * 0.7, box["y"] + box["height"] / 2)  # blank space right of the title
    assert page.url == live + "/tasks/2"
    page.goto(live + "/projects")
    r = row(page, "Kitchen")
    box = r.bounding_box()
    with page.expect_navigation():
        page.mouse.click(box["x"] + box["width"] * 0.7, box["y"] + box["height"] / 2)
    assert page.url == live + "/projects/1"


def test_clicking_a_rows_button_does_not_open_it(page, live):
    page.goto(live + "/projects")
    row(page, "Kitchen").locator("button.peek").click()
    assert page.url == live + "/projects"


def test_due_today_date_is_not_amber(page, live):
    """Amber means stalled; only an overdue date gets a color."""
    page.goto(live + "/waiting")
    r = row(page, "quote from Bob")
    style = "e => [getComputedStyle(e).color, getComputedStyle(e).fontWeight]"
    assert r.locator(".date").evaluate(style) == r.locator(".meta").evaluate(style)


def test_help_dialog_uses_the_theme(page, live):
    page.goto(live + "/next")
    page.keyboard.press("?")
    bg = page.locator(".help-dialog").evaluate("e => getComputedStyle(e).backgroundColor")
    assert bg == page.evaluate("getComputedStyle(document.body).backgroundColor")


def test_review_dots_are_easy_to_click(page, live):
    page.goto(live + "/review")
    boxes = [d.bounding_box() for d in page.locator(".rail .dot").all()]
    assert all(b["width"] >= 24 and b["height"] >= 24 for b in boxes)
    assert all(b2["x"] >= b1["x"] + b1["width"] for b1, b2 in zip(boxes, boxes[1:]))


def test_horizons_arrow_keys_move_between_levels(page, live):
    page.goto(live + "/horizons?level=goal")
    page.keyboard.press("ArrowRight")
    page.wait_for_url("**/horizons?level=area")
    page.locator(".rail .dot").first.click()
    page.wait_for_url("**/horizons?level=purpose")


def test_remind_box_previews_how_it_reads(page, live):
    page.goto(live + "/tasks/7")  # "call mum": a reminder still to come
    hint = page.locator("label .when")
    page.fill("input[name=remind_at]", "oct 30 at 5pm")
    sync_api.expect(hint).to_have_text("→ Fri Oct 30, 17:00")
    page.fill("input[name=remind_at]", "yesterday")
    sync_api.expect(hint).to_have_text("→ Wed Oct 21, 09:00 · already passed")
    page.fill("input[name=remind_at]", "gibberish")
    sync_api.expect(hint).to_have_text("not understood")
    page.fill("input[name=remind_at]", "")
    sync_api.expect(hint).to_have_text("")


def test_remind_button_copies_the_picked_date_and_time_into_the_text_box(page, live, conn):
    page.goto(live + "/tasks/7")  # "call mum": a reminder still to come
    field = page.locator(".date-field", has=page.locator("input[name=remind_at]"))
    field.locator(".pick").click()  # showPicker() needs this real click's user activation
    assert field.locator(".picker").input_value() == page.input_value("input[name=remind_at]").replace(" ", "T")
    field.locator(".picker").evaluate(
        "el => { el.value = '2026-11-05T17:30'; el.dispatchEvent(new Event('change', {bubbles: true})); }")
    assert page.input_value("input[name=remind_at]") == "2026-11-05 17:30"
    sync_api.expect(page.locator("label .when")).to_have_text("→ Thu Nov 5, 17:30")  # the preview follows
    with page.expect_navigation():
        page.get_by_role("button", name="Save", exact=True).click()
    assert datetime.fromisoformat(db.get_task(conn, 7)["remind_at"]).astimezone().strftime("%Y-%m-%d %H:%M") == "2026-11-05 17:30"
