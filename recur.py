"""Recurrence rules: "daily", "weekly", "monthly", "yearly", or "every N days|weeks|months|years"."""
import calendar
import re
from datetime import date, timedelta

ALIASES = {"daily": "every 1 day", "weekly": "every 1 week",
           "monthly": "every 1 month", "yearly": "every 1 year"}
PATTERN = re.compile(r"^every\s+(?:(\d+)\s+)?(day|week|month|year)s?$")


def parse(rule):
    """Return (n, unit) or None if the rule is not understood."""
    if not rule:
        return None
    rule = ALIASES.get(rule.strip().lower(), rule.strip().lower())
    m = PATTERN.match(rule)
    if not m:
        return None
    n = int(m.group(1) or 1)
    return (n, m.group(2)) if n > 0 else None


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
    """Next due date after completing: step from the old due date until it is in the future."""
    n, unit = parse(rule)
    d = step(due or today, n, unit)
    while d <= today:
        d = step(d, n, unit)
    return d


def occurrences(rule, due, end):
    """Future dates after `due` up to `end`, each stepped from `due` so month-end days don't drift."""
    n, unit = parse(rule)
    k = 1
    while (d := step(due, n * k, unit)) <= end:
        yield d
        k += 1
