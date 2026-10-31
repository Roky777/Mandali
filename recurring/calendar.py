import calendar
from datetime import date, timedelta


def advance(day, period, anchor):
    if period == "daily":
        return day + timedelta(days=1)
    if period == "weekly":
        return day + timedelta(days=7)
    if period == "monthly":
        month = day.month % 12 + 1
        year = day.year + (day.month == 12)
        return date(year, month, min(anchor.day, calendar.monthrange(year, month)[1]))
    if period == "yearly":
        year = day.year + 1
        return date(
            year,
            anchor.month,
            min(anchor.day, calendar.monthrange(year, anchor.month)[1]),
        )
    raise ValueError("Unsupported billing period.")
