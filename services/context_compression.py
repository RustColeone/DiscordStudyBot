import math
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from services import database as db


CONTEXT_LIMITS = {
    "deepseek": 1_000_000,
    "gemini": 1_000_000,
    "chatgpt": 128_000,
}


def context_limit_for_model(ai_model: str, model: str) -> int:
    normalized_model = model.lower()
    if ai_model == "chatgpt":
        if normalized_model.startswith("gpt-3.5"):
            return 16_000
        if normalized_model == "gpt-4":
            return 8_000
    return CONTEXT_LIMITS[ai_model]


@dataclass
class CompressionResult:
    compressed: bool
    removed_messages: int = 0
    estimated_tokens_before: int = 0


def estimate_tokens(text: str) -> int:
    ascii_count = sum(1 for character in text if ord(character) < 128)
    non_ascii_count = len(text) - ascii_count
    return max(1, math.ceil(ascii_count * 0.3 + non_ascii_count * 0.6))


def estimate_messages_tokens(messages: List[Dict]) -> int:
    return sum(estimate_tokens(message.get("content", "")) + 4 for message in messages)


def compact_history_if_needed(
    channel_id: str,
    ai_model: str,
    summarize: Callable[[str], str],
    output_reserve: int,
    context_limit: Optional[int] = None,
    force: bool = False,
) -> CompressionResult:
    records = db.load_chat_history_records(channel_id, ai_model)
    existing_summary = db.get_chat_context_summary(channel_id, ai_model) or ""
    limit = context_limit or CONTEXT_LIMITS[ai_model]
    usable_input = max(1, limit - output_reserve)
    trigger = int(usable_input * 0.8)
    estimated_before = estimate_messages_tokens(records) + estimate_tokens(existing_summary)
    if not force and estimated_before < trigger:
        return CompressionResult(False, estimated_tokens_before=estimated_before)

    recent_budget = int(usable_input * 0.3)
    if force:
        recent_budget = min(recent_budget, max(1, estimated_before // 3))
    recent_tokens = 0
    keep_ids = set()
    non_system_records = [record for record in records if record["role"] != "system"]
    for record in reversed(non_system_records):
        record_tokens = estimate_tokens(record["content"]) + 4
        if keep_ids and recent_tokens + record_tokens > recent_budget:
            break
        keep_ids.add(record["id"])
        recent_tokens += record_tokens

    compact_records = [record for record in non_system_records if record["id"] not in keep_ids]
    if not compact_records:
        return CompressionResult(False, estimated_tokens_before=estimated_before)

    transcript = "\n".join(
        f"{record['role'].upper()}: {record['content']}" for record in compact_records
    )
    source = (
        "Update the durable conversation summary below. Preserve user preferences, names, "
        "decisions, commitments, dates, unresolved tasks, and facts needed for future turns. "
        "Remove repetition and obsolete conversational filler. Do not invent information.\n\n"
        f"EXISTING SUMMARY:\n{existing_summary or '(none)'}\n\n"
        f"OLDER CONVERSATION:\n{transcript}"
    )
    try:
        summary = summarize(source).strip()
    except Exception as error:
        print(f"Context compression failed for {ai_model}/{channel_id}: {error}")
        return CompressionResult(False, estimated_tokens_before=estimated_before)
    if not summary:
        return CompressionResult(False, estimated_tokens_before=estimated_before)

    db.replace_chat_history_with_summary(
        channel_id,
        ai_model,
        summary,
        [record["id"] for record in compact_records],
    )
    return CompressionResult(True, len(compact_records), estimated_before)