"""Serialization of contract objects for durable storage."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from contracts import Address, Display, FileRef, Request, Sender


def request_to_dict(request: Request) -> dict[str, Any]:
    value = asdict(request)
    value["address"] = str(request.address)
    value["extra"] = dict(request.extra)
    return value


def request_from_dict(value: Mapping[str, Any]) -> Request:
    return Request(
        id=value["id"], address=Address.parse(value["address"]), sender=Sender(**value["sender"]),
        text=value["text"], received_at=value["received_at"], origin=value.get("origin", "person"),
        is_direct=value.get("is_direct", True), mentions_me=value.get("mentions_me"),
        reply_to=value.get("reply_to"), quoted_text=value.get("quoted_text", ""), kind=value.get("kind", "text"),
        files=tuple(FileRef(**item) for item in value.get("files", ())),
        display=Display(**value.get("display", {})), extra=value.get("extra", {}))
