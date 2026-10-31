"""Drive the real JS in real Chromium. Slow, so excluded from the default run: `uv run pytest -m browser`.

Run these when a change touches templates/, static/ or static/app.js. The Stop hook and pre-commit
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


def test_a_ticked_row_opens_its_task_and_cant_be_ticked_again(page, live):
    page.goto(live + "/horizons?level=ground")
    row(page, "vacuum").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    gone = row(page, "vacuum")
    assert gone.locator("form").count() == 0 and gone.locator(".check").inner_text() == "✓"
    gone.locator(".meta").click()
    page.wait_for_url(live + "/tasks/5")


def test_finishing_a_projects_next_action_stays_on_the_page(page, live):
    page.goto(live + "/next")
    row(page, "buy tiles").locator("form.done button").click()
    page.wait_for_selector("li.task.gone")
    assert page.url == live + "/next"


def test_done_then_project_opens_the_project_to_pick_next(page, live, conn):
    page.goto(live + "/tasks/2")
    page.get_by_role("button", name="Done and open project").click()
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


def test_on_a_touch_screen_the_calendar_button_is_the_native_picker(browser, live, conn):
    """iPad Safari doesn't reliably open showPicker(): there a tap lands on the native date input itself."""
    ctx = browser.new_context(has_touch=True, is_mobile=True, viewport={"width": 1024, "height": 768})
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(live + "/tasks/2")
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


def test_calendar_button_copies_the_picked_date_into_the_text_box(page, live, conn):
    page.goto(live + "/tasks/2")
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


def test_running_timer_clock_ticks(page, live):
    page.clock.install(time=NOW)
    page.goto(live + "/next")
    before = page.inner_text(".running-timer .clock")
    page.clock.run_for(3000)
    assert page.inner_text(".running-timer .clock") != before


def test_task_page_timer_is_by_the_title_and_ticks(page, live):
    page.clock.install(time=NOW)
    page.goto(live + "/tasks/7")  # "call mum", running
    timer = page.locator(".title-line .task-timer")
    stop, title = timer.get_by_role("button", name="Stop the timer"), page.locator("input.big").bounding_box()
    box = stop.bounding_box()
    assert stop.is_visible() and title["y"] <= box["y"] + box["height"] / 2 <= title["y"] + title["height"]  # same line
    before = timer.locator(".clock").inner_text()
    page.clock.run_for(3000)
    assert timer.locator(".clock").inner_text() != before
    page.goto(live + "/tasks/2")  # "buy tiles", not running
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
    page.locator(".theme-toggle").click()
    assert bg() == light


def test_drag_reorders_project_steps(page, live, conn):
    page.goto(live + "/projects/1")
    titles = lambda: page.locator("ul[data-reorder] li.task a.title").all_inner_texts()
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
    page.goto(live + "/someday?tickled=1")
    page.locator("button.peek-all").click()  # open steps mustn't confuse the drag
    titles = lambda: page.locator("ul[data-reorder] > li.task > a.title").all_inner_texts()  # not the someday items
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


def test_project_row_arrow_folds_out_its_steps(page, live):
    for url, project, step in (("/projects", "Kitchen", "lay tiles"), ("/someday?tickled=1", "Someday proj", "dream step")):
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
    for url in ("/projects", "/someday?tickled=1"):
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
    page.select_option("select[name=project_id]", "__new__")  # "＋ New project…": no save yet, it needs a name
    assert box.is_visible()
    page.fill("input[name=new_project]", "Garage")
    with page.expect_navigation():
        page.locator("input[name=new_project]").blur()
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
    with page.expect_response(lambda r: "/autosave/" in r.url):
        page.locator("input.big").focus()  # leaving the notes saves them
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
    drag_to_panel(page, row(page, "legacy orphan").locator(".title"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task.selected')")
    assert db.get_task(conn, 1)["project_id"] == db.get_task(conn, orphan)["project_id"] == 1
    assert page.locator("main li.task[data-id]").count() == 0


def test_an_unselected_row_drags_alone(page, live, conn):
    page.goto(live + "/inbox")
    row(page, "legacy orphan").click(modifiers=["Shift"], position={"x": 400, "y": 10})
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_task(conn, 1)["project_id"] == 1 and row(page, "legacy orphan").count() == 1


def test_dropping_an_inbox_row_on_reference_files_it_there(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="reference"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_task(conn, 1) is None
    assert [(r["title"], r["notes"]) for r in db.references(conn) if r["title"] == "inbox item"] == [("inbox item", "from email")]


def test_dropping_an_inbox_row_on_new_asks_for_the_name_in_the_panel(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="__new__"]')
    name = page.locator(".drop-panel .naming input[name=new_project]")
    name.wait_for(state="visible")
    assert page.locator(".drop-panel input[name=new_project]:visible").count() == 1
    assert page.evaluate("document.activeElement.name") == "new_project"
    page.keyboard.type("Dragged")
    page.keyboard.press("Enter")
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert db.get_project(conn, db.get_task(conn, 1)["project_id"])["title"] == "Dragged"
    page.locator(".drop-panel").wait_for(state="hidden")  # after its slide-out


def test_escape_cancels_naming_a_new_project(page, live, conn):
    page.goto(live + "/inbox")
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="__new__"]')
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
    assert panel.locator("h3").all_text_contents() == ["Projects", "Done"]
    drag_to_panel(page, row(page, "inbox item").locator(".title"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"1\"]')")
    assert panel.is_visible()  # still pinned after the drop
    page.reload()
    panel.wait_for(state="visible")  # and after a reload
    panel.locator(".unpin").click()  # the panel floats over the header's button
    panel.wait_for(state="hidden")
    page.reload()
    assert not panel.is_visible()


def test_dragging_a_next_action_onto_another_project_moves_it(page, live, conn):
    page.goto(live + "/next")
    now = page.locator("details[data-band=now] summary b")
    before = int(now.text_content())
    drag_to_panel(page, row(page, "buy tiles").locator(".title"), '[data-project="2"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"2\"]')")
    assert page.url == live + "/next" and int(now.text_content()) == before - 1
    assert (db.get_task(conn, 2)["project_id"], db.get_task(conn, 2)["step"]) == (2, "later")  # next stays a conscious choice


def test_dragging_an_h0_task_files_it_in_a_project(page, live, conn):
    page.goto(live + "/h0")
    page.locator(".pin-panel").click()
    assert page.locator('.drop-panel [data-project="h0"]').count() == 0  # it's H0 already
    drag_to_panel(page, row(page, "vacuum").locator(".title"), '[data-project="1"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"5\"]')")
    t = db.get_task(conn, 5)
    assert (t["project_id"], t["step"], t["ground"]) == (1, "later", 0)


def test_dragging_a_waiting_task_keeps_it_waiting(page, live, conn):
    page.goto(live + "/waiting")
    drag_to_panel(page, row(page, "quote from Bob").locator(".title"), '[data-project="2"]')
    page.wait_for_function("document.querySelector('li.task[data-id=\"4\"] .proj')?.textContent === 'Stalled'")
    assert (db.get_task(conn, 4)["project_id"], db.get_task(conn, 4)["step"]) == (2, "waiting")


def test_waiting_lists_projects_then_tasks_and_drags_projects(page, live, conn):
    """Waiting projects (dragged to reorder), then the waiting tasks, H0's first."""
    db.add_project(conn, "Visa", status="waiting")
    page.goto(live + "/waiting")
    assert page.locator("ul[data-reorder] > li.task > a.title").all_inner_texts() == ["Visa", "Waiting proj"]
    assert page.locator("main ul.tasks:not([data-reorder]) > li.task a.title").all_inner_texts() == ["parcel from Ann", "quote from Bob"]
    with page.expect_response(lambda r: r.url.endswith("/order/waiting")) as saved:
        row(page, "Waiting proj").locator(".grip").drag_to(row(page, "Visa"), target_position={"x": 40, "y": 4})
    assert saved.value.status == 204
    assert [p["title"] for p in db.projects(conn, "waiting")] == ["Waiting proj", "Visa"]
    # An H0 waiting task dropped on a project stays waiting.
    drag_to_panel(page, row(page, "parcel from Ann").locator(".title"), '[data-project="2"]')
    page.wait_for_function("document.querySelector('li.task[data-id=\"14\"] .proj')?.textContent === 'Stalled'")
    t = db.get_task(conn, 14)
    assert (t["project_id"], t["step"], t["ground"]) == (2, "waiting", 0)


def test_dragging_a_project_step_by_its_grip_moves_it_to_another_project(page, live, conn):
    page.goto(live + "/projects/1")
    assert page.locator('.drop-panel [data-project="1"]').count() == 0  # not its own project
    drag_to_panel(page, row(page, "lay tiles").locator(".grip"), '[data-project="2"]')
    page.wait_for_function("!document.querySelector('li.task[data-id=\"3\"]')")
    assert (db.get_task(conn, 3)["project_id"], db.get_task(conn, 3)["step"]) == (2, "later")
    # The next action can't be reordered, but it can go: the page redraws, the project now stalled.
    drag_to_panel(page, row(page, "buy tiles").locator(".grip"), '[data-project="2"]')
    page.locator(".title-line > .warn").wait_for()
    assert row(page, "buy tiles").count() == 0 and db.get_task(conn, 2)["project_id"] == 2


def test_review_arrow_keys_move_between_steps(page, live):
    page.goto(live + "/review?step=overdue")
    page.keyboard.press("ArrowRight")
    page.wait_for_url("**/review?step=nexts")
    page.keyboard.press("ArrowLeft")
    page.wait_for_url("**/review?step=overdue")
    page.locator("a[rel=next]").click()
    page.wait_for_url("**/review?step=nexts")


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


def test_horizons_arrow_keys_move_between_levels(page, live):
    page.goto(live + "/horizons?level=goal")
    page.keyboard.press("ArrowRight")
    page.wait_for_url("**/horizons?level=area")
    page.locator(".rail .dot").first.click()
    page.wait_for_url("**/horizons?level=purpose")


def test_remind_box_previews_how_it_reads(page, live):
    page.goto(live + "/tasks/7")  # "call mum": a reminder still to come
    hint = page.locator(".when[data-parse=remind]")
    page.fill("input[name=remind_at]", "oct 30 at 5pm")
    sync_api.expect(hint).to_have_text("→ Fri Oct 30, 17:00")
    page.fill("input[name=remind_at]", "yesterday")
    sync_api.expect(hint).to_have_text("→ Wed Oct 21, 09:00 · already passed")
    page.fill("input[name=remind_at]", "gibberish")
    sync_api.expect(hint).to_have_text("not understood")
    page.fill("input[name=remind_at]", "")
    sync_api.expect(hint).to_have_text("")


def test_due_and_repeat_boxes_preview_how_they_read(page, live):
    page.goto(live + "/tasks/7")
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
    page.goto(live + "/tasks/7")  # "call mum": a reminder still to come
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
    pg.wait_for_timeout(300)  # the slide
    nav = pg.locator("nav").bounding_box()
    assert nav["y"] + nav["height"] <= 1  # slid out of view
    pg.evaluate("scrollTo(0, 500)")
    pg.wait_for_function("!document.body.classList.contains('nav-hidden')")
    ctx.close()


@pytest.mark.parametrize("url", ["/projects/1", "/tasks/2"])
def test_a_record_page_opens_centered_past_the_nav(page, live, url):
    page.set_viewport_size({"width": 1200, "height": 700})
    page.goto(live + url)
    page.wait_for_function("document.body.classList.contains('nav-hidden')")
    top = page.locator("input.big").bounding_box()["y"]
    bottom = page.evaluate("Math.max(...[...document.querySelector('main').children].filter(e => e.tagName !== 'SCRIPT'"
                           " && getComputedStyle(e).position !== 'fixed').map(e => e.getBoundingClientRect().bottom))")
    assert page.locator("div.bar").bounding_box()["y"] + page.locator("div.bar").bounding_box()["height"] <= 0  # the search bar too
    if bottom - top < 700 - 48:  # it fits: centered, as much room above as below
        assert abs(top - (700 - bottom)) <= 6, (top, bottom)
    else:  # taller: from near the top
        assert 0 <= top <= 30, top
    page.mouse.wheel(0, -50)  # scrolling up shows the nav again
    page.wait_for_function("!document.body.classList.contains('nav-hidden')")


def test_project_status_select_saves_on_pick(page, live, conn):
    page.goto(live + "/projects/1")
    assert page.get_by_role("button", name="Abandon").count() == 0 and page.get_by_role("button", name="Someday").count() == 0
    with page.expect_navigation():
        page.get_by_label("Status").select_option("someday")
    assert db.get_project(conn, 1)["status"] == "someday" and page.url == live + "/projects/1"
    with page.expect_navigation():
        page.get_by_label("Status").select_option("abandoned")
    assert db.get_project(conn, 1)["status"] == "abandoned"
    with page.expect_navigation():
        page.get_by_label("Status").select_option("active")
    assert db.get_project(conn, 1)["status"] == "active"


def test_task_step_select_saves_on_pick(page, live, conn):
    page.goto(live + "/tasks/3")  # "lay tiles", a later step
    with page.expect_navigation():
        page.locator("select[name=step]").select_option("waiting")
    assert db.get_task(conn, 3)["step"] == "waiting" and page.url == live + "/tasks/3"
    assert page.get_by_role("button", name="Save").count() == 0 or not page.get_by_role("button", name="Save").is_visible()


def test_project_time_split_folds_out(page, live):
    """A project page shows its total time; the per-step bars only after "Show split"."""
    page.goto(live + "/projects/1")
    tally = page.locator("details.tally")
    assert tally.locator("h2 b").is_visible() and not tally.locator("ul").is_visible()
    assert tally.locator(".split-toggle").evaluate("e => getComputedStyle(e, '::after').content") == '"Show split"'
    tally.locator("summary").click()
    assert tally.locator("ul").is_visible()
    assert tally.locator(".split-toggle").evaluate("e => getComputedStyle(e, '::after').content") == '"Hide split"'
    assert page.get_by_text("Show completed").is_visible()  # completed steps start hidden


def test_a_select_saves_typed_notes_before_it_submits(page, live, conn):
    """The server leaves title and notes to autosave, so picking a status mustn't lose what was just typed."""
    page.goto(live + "/projects/1")
    page.locator(".md-editor").click()
    page.keyboard.press("ControlOrMeta+End")
    page.keyboard.type(" typed")
    with page.expect_navigation():
        page.get_by_label("Status").select_option("someday")
    p = db.get_project(conn, 1)
    assert (p["status"], p["notes"]) == ("someday", "tiles\nand grout typed")


def test_enter_in_the_title_saves_without_reloading(page, live, conn):
    page.goto(live + "/reference/1")
    page.evaluate("window.stayed = true")
    page.locator("form[data-autosave] input[name=title]").fill("Plumber Sam")
    page.locator("form[data-autosave] input[name=title]").press("Enter")
    page.wait_for_function("document.querySelector('.autosave').textContent === 'Saved'")
    assert page.evaluate("window.stayed") and db.get_reference(conn, 1)["title"] == "Plumber Sam"


def test_tick_off_refreshes_the_nav_and_keeps_the_tab_lit(page, live):
    page.goto(live + "/next")
    with page.expect_response(lambda r: r.url.endswith("/nav")):
        row(page, "buy tiles").locator("form.done button").click()
    assert page.locator("nav a.on").get_attribute("href") == "/next"


# --- notes toolbar and checklists; someday / maybe --------------------------------------------------------------

def notes_text(page):
    return page.locator("textarea[name=notes]").input_value().replace("\r\n", "\n")


def test_notes_toolbar_shows_while_editing(page, live, conn):
    pid = db.add_reference(conn, "Tools", notes="abc")
    page.goto(live + f"/reference/{pid}")
    tools = page.locator(".md-tools")
    assert tools.evaluate("e => getComputedStyle(e).opacity") == "0"
    page.locator(".md-editor").click()
    page.wait_for_function("getComputedStyle(document.querySelector('.md-tools')).opacity === '1'")
    assert tools.locator("[data-tool=untick]").is_hidden()  # nothing ticked


def test_notes_toolbar_formats(page, live, conn):
    pid = db.add_reference(conn, "Tools", notes="milk")
    page.goto(live + f"/reference/{pid}")
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


def test_checklists_tick_continue_and_reset(page, live, conn):
    pid = db.add_reference(conn, "Packing", notes="- [ ] passport")
    page.goto(live + f"/reference/{pid}")
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


def test_dragging_an_inbox_row_onto_someday(page, live, conn):
    page.goto(live + "/inbox")
    with page.expect_response(lambda r: r.url.endswith("/tasks/move")) as saved:
        drag_to_panel(page, row(page, "inbox item"), '[data-project="someday"]')
    assert saved.value.status == 204
    page.wait_for_function("![...document.querySelectorAll('li.task a.title')].some(a => a.textContent === 'inbox item')")
    t = next(t for t in db.someday_tasks(conn) if t["title"] == "inbox item")
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
    page.goto(live + "/projects/4")  # "Someday proj"
    box = page.locator(".title-line input[name=tickle_on]")  # a quiet control on the title line
    assert not page.locator(".tickler .hint").is_visible()  # its reading shows only while editing
    box.fill("nov 15")
    page.wait_for_function("document.querySelector('.tickler .hint.when').textContent === '→ Sun Nov 15'")
    assert page.locator(".tickler .hint").is_visible()
    with page.expect_navigation():
        box.press("Tab")  # leaving the box saves it
    assert db.get_project(conn, 4)["tickle_on"] == "2026-11-15"


def test_enter_in_a_tickler_box_saves_once(page, live, conn):
    page.goto(live + "/projects/4")
    posts = []
    page.on("request", lambda r: r.method == "POST" and r.url.endswith("/projects/4") and posts.append(r))
    page.locator("input[name=tickle_on]").fill("nov 15")
    with page.expect_navigation():
        page.locator("input[name=tickle_on]").press("Enter")
    page.wait_for_timeout(300)
    assert len(posts) == 1 and db.get_project(conn, 4)["tickle_on"] == "2026-11-15"


def test_notes_editor_edge_cases(page, live, conn):
    pid = db.add_reference(conn, "Edges", notes="")
    page.goto(live + f"/reference/{pid}")
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
