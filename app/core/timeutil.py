from datetime import date, datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=3))  # Iraq has no daylight saving time (fixed until a company setting exists)


def local_today(now: datetime | None = None) -> date:
    """Today's date in the company's local time."""
    return (now or datetime.now(timezone.utc)).astimezone(LOCAL_TZ).date()
