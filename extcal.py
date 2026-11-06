"""External calendars: read-only iCal (ICS) feeds shown on the calendar beside the tasks.

`GTD_CALENDARS` lists them as whitespace-separated `name=url` pairs (`_` in a name reads as a space): a Google
calendar's "secret address in iCal format", an Outlook calendar's published ICS link. A feed is fetched the first
time it's asked for, then refreshed in the background once older than FRESH, so a page waits on it only the first
time. A failed fetch keeps the last good copy and reports its error (retried after RETRY)."""
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta

import icalendar
import recurring_ical_events

FRESH = 15 * 60  # seconds a fetched feed is used before it's refreshed
RETRY = 60  # seconds before a failed feed is tried again
TIMEOUT = 10


def parse_config(text):
    """[(name, url)] from `GTD_CALENDARS`."""
    feeds = []
    for pair in (text or "").split():
        name, sep, url = pair.partition("=")
        if sep and name and url:
            if url.startswith("webcal://"):
                url = "https://" + url[len("webcal://"):]
            feeds.append((name.replace("_", " "), url))
    return feeds


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "GTD calendar"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return r.read()


class Feed:
    def __init__(self, url):
        self.url = url
        self.cal = None  # the parsed feed, queryable by date range
        self.error = None
        self.fetched = None  # time.monotonic() of the last attempt
        self.busy = False  # a fetch is under way
        self.lock = threading.Lock()

    def load(self):
        try:
            self.cal, self.error = recurring_ical_events.of(icalendar.Calendar.from_ical(fetch(self.url))), None
        except Exception as e:  # a bad feed must never break the calendar page
            self.error = f"{type(e).__name__}: {e}"
        self.fetched, self.busy = time.monotonic(), False

    def current(self):
        """The feed, fetching it now the first time and in the background once stale."""
        with self.lock:
            if self.fetched is None:
                self.load()  # under the lock, so concurrent first requests wait for the one fetch
                return self
            age = time.monotonic() - self.fetched
            if not self.busy and age > (RETRY if self.error else FRESH):
                self.busy = True
                threading.Thread(target=self.load, daemon=True).start()
        return self


_feeds = {}  # url -> Feed
_feeds_lock = threading.Lock()


def feed(url):
    with _feeds_lock:
        return _feeds.setdefault(url, Feed(url))


def local(dt):
    """A datetime as naive local time; a floating (naive) one already is."""
    return dt.astimezone().replace(tzinfo=None) if dt.tzinfo else dt


def occurrences(cal, start, end):
    """{iso date: [event]} for the events of a parsed feed on the days start..end (inclusive), each day's in order:
    all-day ones first, then by start time. A multi-day event shows on each of its days, its time on the first."""
    by_day = defaultdict(list)
    # A day wider each side: the library reads naive bounds in each event's own zone. The loop below clips to local days.
    for ev in cal.between(start - timedelta(days=1), end + timedelta(days=2)):
        if str(ev.get("STATUS", "")).upper() == "CANCELLED":
            continue
        s = ev["DTSTART"].dt
        e = ev["DTEND"].dt if "DTEND" in ev else s + (ev["DURATION"].dt if "DURATION" in ev else timedelta())
        all_day = not isinstance(s, datetime)
        if all_day:
            first, last = s, max(s, e - timedelta(days=1))  # an all-day DTEND is the day after
        else:
            s, e = local(s), local(e)
            first, last = s.date(), max(s, e - timedelta(microseconds=1)).date()  # ending at midnight stays put
        when = "all day" if all_day else f"{s:%-H:%M}–{e:%-H:%M}"
        if first != last:
            when = (f"{first:%a %-d %b} – {last:%a %-d %b}" if all_day
                    else f"{s:%a %-d %b %-H:%M} – {e:%a %-d %b %-H:%M}")
        title = str(ev.get("SUMMARY", "")).strip() or "(busy)"
        loc = str(ev.get("LOCATION", "")).strip()
        d = max(first, start)
        while d <= min(last, end):
            by_day[d.isoformat()].append({
                "title": title, "all_day": all_day, "sort": "" if all_day else f"{s:%H:%M}",
                "time": f"{s:%-H:%M}" if not all_day and d == first else "",
                "tip": " · ".join(x for x in (title, when, loc) if x)})
            d += timedelta(days=1)
    for evs in by_day.values():
        evs.sort(key=lambda x: (not x["all_day"], x["sort"]))
    return by_day


def events(feeds, start, end):
    """({iso date: [event]}, [(name, error)]) for every configured feed; each event knows its feed's index (`cal`)."""
    by_day, errors = defaultdict(list), []
    for i, (name, url) in enumerate(feeds):
        f = feed(url).current()
        if f.error:
            errors.append((name, f.error))
        if f.cal is not None:
            for day, evs in occurrences(f.cal, start, end).items():
                by_day[day] += [dict(e, cal=i, name=name) for e in evs]
    for evs in by_day.values():
        evs.sort(key=lambda x: (not x["all_day"], x["sort"]))
    return by_day, errors
