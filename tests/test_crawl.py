"""Click everything: follow every link and submit every form the app renders, the way a browser would.

Catches the bugs no targeted test thinks of: a template that breaks on some data, a form field a route
doesn't expect, a redirect to a page that no longer exists. Each form submission runs against a fresh
copy of the seeded DB, and every page is requested with itself as the Referer (as after a save).
"""
import re
import shutil
from html.parser import HTMLParser
from urllib.parse import urlsplit

import db
from app import app
from conftest import TODAY, seed


class Page(HTMLParser):
    """Links and forms of one HTML page."""

    def __init__(self):
        super().__init__()
        self.links, self.forms = [], []
        self._form = self._select = self._option = self._textarea = None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "a" and "href" in a:
            self.links.append(a["href"])
        elif tag == "form":
            self._form = {"method": (a.get("method") or "get").lower(), "action": a.get("action"),
                          "fields": [], "selects": [], "buttons": []}
            self.forms.append(self._form)
        elif self._form is None:
            return
        elif tag == "input" and a.get("type") not in ("submit", "button"):
            self._form["fields"].append(a)
        elif tag == "textarea":
            self._textarea = {"name": a.get("name"), "value": "", "required": "required" in a}
            self._form["fields"].append(self._textarea)
        elif tag == "select":
            self._select = {"name": a.get("name"), "options": []}
            self._form["selects"].append(self._select)
        elif tag == "option" and self._select is not None:
            self._option = {"value": a.get("value"), "text": "", "selected": "selected" in a}
            self._select["options"].append(self._option)
        elif tag == "button" and a.get("type", "submit") == "submit" and a.get("formmethod") != "dialog":
            self._form["buttons"].append(a)

    def handle_endtag(self, tag):
        if tag == "form":
            self._form = None
        elif tag == "select":
            self._select = None
        elif tag == "option":
            self._option = None
        elif tag == "textarea":
            self._textarea = None

    def handle_data(self, data):
        if self._option is not None:
            self._option["text"] += data
        elif self._textarea is not None:
            self._textarea["value"] += data


def parse(html):
    p = Page()
    p.html = html
    p.feed(html)
    for f in p.forms:
        for s in f["selects"]:
            for o in s["options"]:
                o["value"] = o["value"] if o["value"] is not None else o["text"]
    return p


def submissions(page_url, form):
    """Every (method, url, data) a person could send with this form: each submit button, each select option."""
    base = {}
    for f in form["fields"]:
        if not f.get("name"):
            continue
        value = f.get("value") or ""
        if not value and "required" in f and f["required"] is not False:
            value = TODAY.isoformat() if f.get("type") == "date" else "x"  # the browser won't send it blank
        base[f["name"]] = value
    defaults = {}
    for s in form["selects"]:
        chosen = next((o for o in s["options"] if o["selected"]), s["options"][0] if s["options"] else None)
        defaults[s["name"]] = chosen["value"] if chosen else ""
    variants = [defaults] + [{**defaults, s["name"]: o["value"]}
                             for s in form["selects"] for o in s["options"] if o["value"] != defaults[s["name"]]]
    out = []
    for b in form["buttons"] or [{}]:
        url = b.get("formaction") or form["action"] or page_url
        method = (b.get("formmethod") or form["method"]).lower()
        for v in variants:
            data = {**base, **v}
            if b.get("name"):
                data[b["name"]] = b.get("value", "")
            out.append((method, url, data))
    return out


CALENDAR_MONTHS = {"", "m=2026-09", "m=2026-10", "m=2026-11",
                   "w=2026-10-19", "w=2026-10-12", "w=2026-10-26"}  # ← and → go on forever


def internal(href):
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return None
    if parts.path == "/calendar":  # only within the listed months
        if parts.query not in CALENDAR_MONTHS:
            return None
    return parts.path + (f"?{parts.query}" if parts.query else "")


def get(client, url):
    return client.get(url, headers={"Referer": f"http://localhost{url}"})


def crawl(client):
    """Every page reachable from / by links. Returns {url: parsed page} and the broken links."""
    pages, broken, todo = {}, [], ["/"]
    while todo:
        url = todo.pop()
        if url in pages:
            continue
        r = get(client, url)
        if r.status_code in (301, 302, 303):
            pages[url] = None
            todo.append(internal(r.headers["Location"]) or "/")
            continue
        if r.status_code != 200:
            broken.append((url, r.status_code))
            pages[url] = None
            continue
        if not r.mimetype == "text/html":
            pages[url] = None
            continue
        pages[url] = parse(r.get_data(as_text=True))
        todo += [u for u in map(internal, pages[url].links) if u and u not in pages]
    return pages, broken


def test_every_link_works(client, conn):
    seed(conn)
    pages, broken = crawl(client)
    assert broken == []
    html_pages = [u for u, p in pages.items() if p]
    assert len(html_pages) > 25, html_pages  # the crawl really reached the app
    for must in ("/inbox", "/process", "/next", "/waiting", "/someday", "/review", "/calendar", "/horizons",
                 "/projects/1", "/projects/4", "/tasks/1", "/horizons/3"):
        assert must in pages, must


def test_every_page_has_the_same_skeleton(client, conn):
    """One layout everywhere: header (title + actions), then the add line, then the list."""
    seed(conn)
    pages, _ = crawl(client)
    checked = 0
    for url, page in pages.items():
        if not page or "<main>" not in page.html:
            continue
        main = re.sub(r'<p class="flash">.*?</p>', "", page.html.split("<main>")[1]).strip()
        assert main.startswith('<header class="page">'), url
        header = main[:main.index("</header>")]
        assert "<h1>" in header or 'class="big"' in main, url  # edit pages: the big title input is the heading
        assert 'class="chips"' not in main, url
        adds = [m.start() for m in re.finditer(r'<form[^>]*class="add', main)]
        assert len(adds) <= 1, url
        if adds:
            between = main[main.index("</header>") + len("</header>"):adds[0]]
            assert between.strip() == "" or 'class="big"' in between, url  # a record page: its own form comes first
            for later in ('class="tasks', '<table class="cal'):
                if later in main:
                    assert adds[0] < main.index(later), (url, later)
        checked += 1
    assert checked > 25


def test_every_form_submits(client, conn, tmp_path):
    seed(conn)
    template = tmp_path / "seeded.db"
    shutil.copy(app.config["DB_PATH"], template)
    pages, _ = crawl(client)
    seen, failures = set(), []
    for page_url, page in pages.items():
        for form in (page.forms if page else []):
            for method, url, data in submissions(page_url, form):
                key = (method, url, tuple(sorted(data.items())))
                if key in seen:
                    continue
                seen.add(key)
                fresh = tmp_path / "fresh.db"
                shutil.copy(template, fresh)
                app.config["DB_PATH"] = str(fresh)
                headers = {"Referer": f"http://localhost{page_url}"}
                if method == "post":
                    r = client.post(url, data=data, headers=headers, follow_redirects=True)
                else:
                    r = client.get(url, query_string=data, headers=headers, follow_redirects=True)
                if r.status_code != 200:
                    failures.append((r.status_code, page_url, method, url, data))
                c = db.connect(str(fresh))
                assert c.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
                c.close()
    assert len(seen) > 300  # the forms really were found
    assert failures == [], "\n".join(map(str, failures))


def submit_unchanged(client, url):
    """Post a page's edit form exactly as rendered."""
    form = next(f for f in parse(get(client, url).get_data(as_text=True)).forms
                if f["method"] == "post" and f["action"] in (None, url))  # the form that posts to the page itself
    _, action, data = submissions(url, form)[0]  # its defaults, pressing the first button (Save)
    return client.post(action, data=data, headers={"Referer": f"http://localhost{url}"})


def test_saving_an_edit_page_unchanged_changes_nothing(client, conn):
    """Each edit form must render the record's current values, or saving it quietly rewrites the record."""
    seed(conn)
    columns = lambda row: {k: row[k] for k in row.keys()}
    orphans = [t["id"] for t in db.orphans(conn)]
    for (tid,) in conn.execute("SELECT id FROM tasks WHERE status != 'done'").fetchall():
        before = columns(db.get_task(conn, tid))
        submit_unchanged(client, f"/tasks/{tid}")
        if tid in orphans:  # by design, saving an orphan sends it to the inbox
            before["status"] = "inbox"
        assert columns(db.get_task(conn, tid)) == before, before["title"]
    for (pid,) in conn.execute("SELECT id FROM projects").fetchall():
        before = columns(db.get_project(conn, pid))
        submit_unchanged(client, f"/projects/{pid}")
        assert columns(db.get_project(conn, pid)) == before, before["title"]
    for (hid,) in conn.execute("SELECT id FROM horizons").fetchall():
        before = columns(db.get_horizon(conn, hid))
        submit_unchanged(client, f"/horizons/{hid}")
        assert columns(db.get_horizon(conn, hid)) == before, before["title"]


def test_task_rows_match_the_tick_off_script(client, conn):
    """base.html's script ticks off `form.done` via fetch and expects 204, or a redirect it follows."""
    seed(conn)
    base = get(client, "/next").get_data(as_text=True)
    assert "classList.contains('done')" in base and "'X-Requested-With': 'fetch'" in base
    ticks = [f for f in parse(base).forms if f["action"] and f["action"].endswith("/complete")]
    assert ticks and base.count('class="done"') == len(ticks)
    r = client.post("/tasks/7/complete", headers={"X-Requested-With": "fetch"})  # "call mum", H0
    assert r.status_code == 204
    r = client.post("/tasks/7/complete")  # without the script: back to where you were
    assert r.status_code == 302


def test_process_someday_choices(client, conn):
    seed(conn)
    page = parse(get(client, "/process").get_data(as_text=True))
    (select,) = [s for f in page.forms for s in f["selects"] if s["name"] == "someday_target"]
    by_text = {o["text"]: o["value"] for o in select["options"]}
    assert by_text["a new someday project"] == "new"
    assert by_text["Kitchen"] == "1" and by_text["Someday proj (someday)"] == "4" and "Done proj" not in by_text
    client.post("/process/1", data={"action": "someday", "someday_target": by_text["a new someday project"]})
    assert [p["title"] for p in db.projects(conn, "someday")] == ["inbox item", "Someday proj"]
