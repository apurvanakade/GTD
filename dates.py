"""Natural-language dates: "tomorrow", "saturday", "next week", "in 3 days", "oct 30", or ISO "2026-10-30".
Reminder times add a time of day: "tomorrow 9am", "fri 14:00", "in 2 hours"."""
import calendar
import re
from datetime import date, datetime, time, timedelta

from recur import add_months

WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})
WEEKDAYS["tues"], WEEKDAYS["thur"], WEEKDAYS["thurs"] = 1, 3, 3
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
IN = re.compile(r"^in\s+(\d+)\s+(day|week|month|year)s?$")
IN_TIME = re.compile(r"^in\s+(\d+)\s+(minute|min|hour|hr)s?$")
# A time of day: "9am", "9:15 pm", "14:00", "noon". Bare "9" isn't one, so "oct 30" stays a date.
CLOCK = r"(noon|midnight|(\d{1,2})(?::(\d{2}))?\s*(am|pm)|(\d{1,2}):(\d{2}))"
TIME_LAST = re.compile(rf"(?:^|\s)(?:at\s+)?{CLOCK}$")
TIME_FIRST = re.compile(rf"^{CLOCK}(?:\s|$)")
DEFAULT_TIME = time(9, 0)  # a reminder given only a date
MONTH_DAY = re.compile(r"^(?:([a-z]+)\s+(\d{1,2})|(\d{1,2})\s+([a-z]+))(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$")


def parse(text, today):
    """Return a date, or None if the text isn't understood. Weekdays mean the next one after today."""
    s = " ".join((text or "").lower().replace("next ", "next_").split())
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except ValueError:
        pass
    if s == "today":
        return today
    if s == "tomorrow":
        return today + timedelta(days=1)
    if s == "yesterday":
        return today - timedelta(days=1)
    if s == "next_week":
        return today + timedelta(days=7 - today.weekday())  # next Monday
    if s == "next_month":
        return add_months(today, 1)
    if s.startswith("next_") and s[5:] in WEEKDAYS:
        s = s[5:]
    if s in WEEKDAYS:
        return today + timedelta(days=(WEEKDAYS[s] - today.weekday() - 1) % 7 + 1)
    m = IN.match(s)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        if unit == "day":
            return today + timedelta(days=n)
        if unit == "week":
            return today + timedelta(weeks=n)
        return add_months(today, n * (12 if unit == "year" else 1))
    m = MONTH_DAY.match(s)
    if m:
        month = MONTHS.get(m.group(1) or m.group(4))
        day = int(m.group(2) or m.group(3))
        year = int(m.group(5)) if m.group(5) else None
        if month:
            try:
                d = date(year or today.year, month, day)
                if not year and d < today:  # "jan 5" in October means next January
                    d = date(today.year + 1, month, day)
                return d
            except ValueError:
                return None
    return None


def _clock(m):
    if m.group(1) in ("noon", "midnight"):
        return time(12 if m.group(1) == "noon" else 0, 0)
    if m.group(4):
        h, mi = int(m.group(2)), int(m.group(3) or 0)
        if not 1 <= h <= 12 or mi > 59:
            return None
        return time(h % 12 + (12 if m.group(4) == "pm" else 0), mi)
    h, mi = int(m.group(5)), int(m.group(6))
    return time(h, mi) if h < 24 and mi < 60 else None


def parse_when(text, now):
    """Return a naive local datetime for a reminder, or None. `now` is naive local time. A date alone means
    DEFAULT_TIME; a time alone means the next one; "in N minutes|hours" counts from now."""
    s = " ".join(re.sub(r"(\d)t(\d)", r"\1 \2", (text or "").lower()).split())  # ISO "2026-10-30T09:00"
    if not s:
        return None
    m = IN_TIME.match(s)
    if m:
        return now + timedelta(minutes=int(m.group(1)) * (60 if m.group(2) in ("hour", "hr") else 1))
    m = TIME_LAST.search(s) or TIME_FIRST.match(s)
    if not m:
        day = parse(s, now.date())
        return datetime.combine(day, DEFAULT_TIME) if day else None
    t = _clock(m)
    rest = (s[:m.start()] + " " + s[m.end():]).strip()
    if t is None:
        return None
    if not rest:
        when = datetime.combine(now.date(), t)
        return when if when > now else when + timedelta(days=1)
    day = parse(rest, now.date())
    return datetime.combine(day, t) if day else None
