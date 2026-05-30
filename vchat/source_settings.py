from celery.schedules import crontab


DEFAULT_CRAWLER_CONCURRENT_REQUESTS = 2
DEFAULT_CRAWLER_DOWNLOAD_DELAY = 1
DEFAULT_CRAWLER_DOWNLOAD_TIMEOUT = 30
DEFAULT_IGNORED_PARAMS = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "gclsrc",
    "fbclid",
    "msclkid",
    "twclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "gtm_debug",
    "session_id",
    "page",
    "sort",
    "yclid",
)

DEFAULT_REINDEX_CRON = "0 3 * * 1"
MANUAL_REINDEX_MODE = "manual"


def is_manual_reindex(value: str | None) -> bool:
    raw = (value or "").strip().lower()
    return raw in {"", MANUAL_REINDEX_MODE}


def normalize_reindex_cron(value: str | None) -> str:
    if is_manual_reindex(value):
        return MANUAL_REINDEX_MODE
    raw = (value or "").strip()
    return " ".join(raw.split())


def validate_reindex_cron(value: str | None) -> bool:
    if is_manual_reindex(value):
        return True

    normalized = normalize_reindex_cron(value)
    parts = normalized.split(" ")
    if len(parts) != 5:
        return False

    minute, hour, day_of_month, month_of_year, day_of_week = parts
    try:
        crontab(
            minute=minute,
            hour=hour,
            day_of_month=day_of_month,
            month_of_year=month_of_year,
            day_of_week=day_of_week,
        )
    except Exception:
        return False
    return True
