"""Due dates are computed in the customer's calendar (Europe/Moscow)."""
from datetime import datetime, timedelta, timezone

MSK = timezone(timedelta(hours=3))


def due_date(issued_utc: datetime, days: int):
    return (issued_utc + timedelta(days=days)).date()
