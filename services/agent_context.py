from datetime import datetime, timezone

import pytz

from services.reminder_service import resolve_timezone_name


def build_agent_context(
    request_time: datetime,
    configured_timezone: str | None,
    configured_locale: str | None = None,
) -> str:
    timezone_name = resolve_timezone_name(configured_timezone)
    local_timezone = pytz.timezone(timezone_name)
    if request_time.tzinfo is None:
        request_time = request_time.replace(tzinfo=timezone.utc)
    utc_time = request_time.astimezone(timezone.utc)
    local_time = request_time.astimezone(local_timezone)
    locale_name = (configured_locale or "").strip() or (
        "follow the user's current language; no separate regional locale is configured"
    )
    return (
        "Authoritative request-time context from the bot host (do not replace it with the provider, "
        "server, or model locale/clock):\n"
        f"- Request instant (UTC): {utc_time:%Y-%m-%d %H:%M:%S %Z}\n"
        f"- Configured local datetime: {local_time:%Y-%m-%d %H:%M:%S %A %Z%z}\n"
        f"- Configured IANA timezone: {timezone_name}\n"
        f"- Configured locale policy: {locale_name}\n"
        "Interpret relative and absolute date/time expressions in the user's language from this request instant "
        "and configured timezone. Express reminder schedules using the tool's structured fields rather than "
        "copying natural-language time text or using another locale or clock."
    )