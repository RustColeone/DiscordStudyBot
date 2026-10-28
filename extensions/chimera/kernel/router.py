"""Routing: pick the simplest workflow likely to be enough for a request.

A cheap model sorts the request (question or change, its size and risk, a title); simple rules then pick the
workflow. If no model answers, keywords decide. Every decision and its result go to outcomes.jsonl, so the rules
can later be tuned on what actually happened (did it pass, was it landed) instead of set by hand.

    question                         → single    one classmate answers, read-only
    change, tiny                     → single    one classmate changes it; tests check it
    change, small, low risk          → cascade   cheap draft → tests + a cheap judge → a strong model only if needed
    change, medium or large, or high → critique  draft → a reviewer from another model family → one revision
"""

from __future__ import annotations

import re

from .context import Context, SeatUnavailable
from . import prompts

WORKFLOWS = ("single", "cascade", "critique")
QUESTION = re.compile(r"^\s*(what|why|how|where|when|which|who|is|are|does|do|can|could|should|explain|describe|tell me|"
                      r"show me|list)\b|[?？]\s*$|什么|为什么|怎么|如何|吗[?？]?\s*$|哪", re.I)
CHANGE = re.compile(r"\b(add|fix|implement|change|update|rename|refactor|remove|delete|write|create|make|support|"
                    r"replace|move|bump|convert|migrate|optimi[sz]e|improve|clean ?up|test)\b|添加|修复|实现|修改|更新|重命名|重构|"
                    r"删除|写|创建|支持|替换|优化|改", re.I)
RISKY = re.compile(r"security|auth|password|token|secret|payment|migrat|database|schema|concurren|thread|lock|"
                   r"delete|remove|drop|deploy|安全|密码|数据库|迁移|并发|删除", re.I)


def heuristic(text: str) -> dict:
    question = bool(QUESTION.search(text)) and not CHANGE.search(text.split("?")[0][:200])
    size = "tiny" if len(text) < 60 else "small" if len(text) < 280 else "medium" if len(text) < 900 else "large"
    first = text.strip().splitlines()[0] if text.strip() else "New task"
    return {"kind": "question" if question else "change", "size": size,
            "risk": "high" if RISKY.search(text) else "low", "title": first[:60],
            "reason": "sorted by keywords (no model answered)", "by": "keywords"}


async def classify(ctx: Context, key: str, text: str, context_line: str) -> dict:
    """The router seat's reading of the request, or the keywords' when no router model answers."""
    seats = ctx.team.candidates("router", ctx.present)
    for seat in sorted(seats, key=lambda seat: not ctx.able(seat)):
        try:
            answer = await ctx.model(f"{key}.{seat.persona}", seat, "cheap", [
                {"role": "system", "content": prompts.ROUTER},
                {"role": "user", "content": f"{context_line}\n\nRequest:\n{text[:6000]}"}],
                schema=prompts.ROUTE_SCHEMA, schema_name="chimera_route", effort="low", max_tokens=400,
                timeout_s=float(ctx.settings.get("route_timeout_s", 45)), step=key, role="router")
        except SeatUnavailable:
            continue
        except Exception:
            break
        return {**answer["data"], "by": seat.persona}
    return heuristic(text)


ROUTING = {"question": "single", "tiny": "single", "small": "cascade", "medium": "critique", "large": "critique",
           "risky": "critique"}


def choose(signals: dict, *, override: str | None = None, followup: bool = False, routing=None,
           known: set[str] | None = None) -> tuple[str, str]:
    """(workflow, why) by the rules above, or `routing` from config.yml where it points a case at another workflow
    (keys: question, tiny, small, medium, large, risky); `override` (the person's pick) wins."""
    known = set(known or WORKFLOWS)
    if override in known:
        return override, "chosen by you"
    table = {**ROUTING, **{key: value for key, value in dict(routing or {}).items() if value in known}}
    kind, size, risk = signals.get("kind"), signals.get("size"), signals.get("risk")
    if kind == "question":
        return table["question"], "a question: one classmate looks it up"
    if risk == "high":
        return table["risky"], " and ".join(item for item in (f"a {size} change" if size in ("medium", "large") else "",
                                                              "high risk") if item)
    if size == "tiny":
        return table["tiny"], "a tiny change"
    if size == "small":
        return table["small"], "a small, low-risk change" + (" on top of the current one" if followup else "")
    if size in ("medium", "large"):
        return table[size], f"a {size} change"
    return table["medium"], "worth a second pair of eyes"
