"""Recurrence rules: "daily", "weekly", "monthly", "yearly", or "every N days|weeks|months|years".
Add " after done" (e.g. "every 2 weeks after done") to count from the completion date instead of the due date."""
import calendar
import re
from datetime import date, timedelta

UNITS = {"daily": "day", "weekly": "week", "monthly": "month", "yearly": "year"}
PATTERN = re.compile(r"^(?:every (?:(\d+) )?(day|week|month|year)s?|(daily|weekly|monthly|yearly))( after (?:done|completion))?$")


def _match(rule):
    return PATTERN.match(" ".join((rule or "").lower().split()))


def parse(rule):
    """Return (n, unit) or None if the rule is not understood."""
    m = _match(rule)
    if not m:
        return None
    n = int(m.group(1) or 1)
    return (n, m.group(2) or UNITS[m.group(3)]) if n > 0 else None


def after_done(rule):
    m = _match(rule)
    return bool(m and m.group(4))


def add_months(d, n):
    y, m = divmod(d.month - 1 + n, 12)
    year, month = d.year + y, m + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def step(d, n, unit):
    if unit == "day":
        return d + timedelta(days=n)
    if unit == "week":
        return d + timedelta(weeks=n)
    return add_months(d, n * (12 if unit == "year" else 1))


def next_due(rule, due, today):
    """Next due date after completing: step from the old due date until it is in the future.
    Each step is taken from the old due date, so skipping several periods doesn't drift month-end days."""
    n, unit = parse(rule)
    if after_done(rule):
        return step(today, n, unit)
    k = 1
    while (d := step(due or today, n * k, unit)) <= today:
        k += 1
    return d

