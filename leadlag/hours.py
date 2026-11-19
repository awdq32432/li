"""US market-hours tag for a UTC timestamp."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")

# Full-day US stock market closures. Early closes (1pm) are not modelled.
HOLIDAYS = {
    date(2026, 11, 26), date(2026, 12, 25),
    date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
    date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
    date(2027, 11, 25), date(2027, 12, 24),
}


def hours_tag(ts_ms: float) -> str:
    """us_regular 09:30-16:00 ET, us_pre 04:00-09:30, us_post 16:00-20:00,
    overnight otherwise on trading days, weekend from Fri 20:00 to Sun 20:00 ET,
    holiday on full-day closures."""
    d = datetime.fromtimestamp(ts_ms / 1000.0, NY)
    wd, m = d.weekday(), d.hour * 60 + d.minute
    if wd == 5 or (wd == 4 and m >= 1200) or (wd == 6 and m < 1200):
        return "weekend"
    if wd == 6:
        return "overnight"
    if d.date() in HOLIDAYS:
        return "holiday"
    if 570 <= m < 960:
        return "us_regular"
    if 240 <= m < 570:
        return "us_pre"
    if 960 <= m < 1200:
        return "us_post"
    return "overnight"
