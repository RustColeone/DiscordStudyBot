"""A common result envelope for local feature tools and MCP tools."""
from dataclasses import dataclass, field
from pathlib import Path

from app.bot_types import BotResponse


@dataclass
class ToolOutcome:
    status: str
    responses: list[BotResponse]
    summary: str
    error_code: str | None = None
    retryable: bool = False
    sources: list[dict] = field(default_factory=list)

    def context(self):
        return {"status": self.status, "summary": self.summary,
                "error_code": self.error_code, "retryable": self.retryable, "sources": self.sources}


def normalize_outcome(result):
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, BotResponse):
        responses = [result]
    else:
        responses = [BotResponse(text=result)] if isinstance(result, str) else list(result or [])
    status = "success"
    code = None
    retryable = False
    sources = []
    for response in responses:
        for source in response.metadata.get("sources", []):
            if source not in sources and len(sources) < 12:
                sources.append(source)
        explicit = response.metadata.get("tool_status")
        # Compatibility for older feature handlers; new tools should set tool_status explicitly.
        if explicit == "failed" or (response.text or "").lstrip().startswith("❌"):
            status = "failed"
            code = response.metadata.get("error_code", "tool_failed")
            retryable = bool(response.metadata.get("retryable", False))
        elif explicit == "needs_input" and status != "failed":
            status = "needs_input"
        if response.attachment is not None:
            path = Path(response.attachment.path)
            if not path.is_file() or path.stat().st_size == 0:
                status, code, retryable = "failed", "missing_artifact", False
    if not responses:
        status, code = "failed", "empty_tool_result"
    summary = "\n\n".join(
        (response.text or "") + (f"\nPrepared file: {response.attachment.filename or 'attachment'}"
                                if response.attachment is not None else "")
        for response in responses
    )[:6000]
    return ToolOutcome(status, responses, summary, code, retryable, sources)
