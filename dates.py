"""Natural-language dates: "tomorrow", "saturday", "next week", "in 3 days", "oct 30", or ISO "2026-10-30"."""
import calendar
import re
from datetime import date, timedelta

WEEKDAYS = {name.lower(): i for i, name in enumerate(calendar.day_name)}
WEEKDAYS.update({name.lower(): i for i, name in enumerate(calendar.day_abbr)})
WEEKDAYS["tues"], WEEKDAYS["thur"], WEEKDAYS["thurs"] = 1, 3, 3
MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name}
MONTHS.update({name.lower(): i for i, name in enumerate(calendar.month_abbr) if name})
MONTHS["sept"] = 9
IN = re.compile(r"^in\s+(\d+)\s+(day|week|month|year)s?$")
MONTH_DAY = re.compile(r"^(?:([a-z]+)\s+(\d{1,2})|(\d{1,2})\s+([a-z]+))(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?$")


def _add_months(d, n):
    m = d.month - 1 + n
    year, month = d.year + m // 12, m % 12 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


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
        return _add_months(today, 1)
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
        return _add_months(today, n * (12 if unit == "year" else 1))
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
