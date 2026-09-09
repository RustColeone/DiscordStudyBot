from dataclasses import dataclass
from datetime import datetime, timedelta
import re
from typing import Optional

import dateparser
import pytz
from tzlocal import get_localzone_name


@dataclass(frozen=True)
class ParsedReminder:
    remind_at: datetime
    message: str


def resolve_timezone_name(configured_name: Optional[str]) -> str:
    return configured_name or get_localzone_name()


def parse_natural_reminder(text: str, now: datetime, timezone_name: str) -> Optional[ParsedReminder]:
    cleaned = re.sub(r"^\s*(?:\$remind(?:er)?|remind\s+me)\s+", "", text, flags=re.IGNORECASE)
    match = re.match(r"(?P<when>.+?)\s+to\s+(?P<message>.+)$", cleaned, flags=re.IGNORECASE)
    if not match:
        return None

    timezone = pytz.timezone(timezone_name)
    local_now = now.astimezone(timezone)
    when_text = match.group("when").strip()
    if when_text.lower() in {"tomorrow at this time", "tomorrow this time"}:
        remind_at = local_now + timedelta(days=1)
    else:
        when_text = re.sub(
            r"\bat\s+([01]\d|2[0-3])([0-5]\d)\b",
            lambda value: f"at {value.group(1)}:{value.group(2)}",
            when_text,
            flags=re.IGNORECASE,
        )
        remind_at = dateparser.parse(
            when_text,
            settings={
                "RELATIVE_BASE": local_now,
                "TIMEZONE": timezone_name,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
            },
        )
    if remind_at is None or remind_at <= local_now:
        return None
    return ParsedReminder(remind_at=remind_at, message=match.group("message").strip())