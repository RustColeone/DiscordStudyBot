from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List
from uuid import uuid4


@dataclass
class TaskEvent:
    sequence: int
    event_type: str
    actor: str
    data: Dict[str, Any]
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "sequence": self.sequence,
            "type": self.event_type,
            "actor": self.actor,
            "created_at": self.created_at,
            "data": self.data,
        }


@dataclass
class AgentTask:
    goal: str
    task_id: str = field(default_factory=lambda: uuid4().hex)
    status: str = "running"
    response_depth: str = "standard"
    response_mode: str = "conversation"
    participants: List[str] = field(default_factory=list)
    reasoning_effort: str | None = None
    conversation_only: bool = False
    approach: str = ""
    capabilities: List[Dict[str, Any]] = field(default_factory=list)
    events: List[TaskEvent] = field(default_factory=list)

    def record(self, event_type: str, actor: str, **data: Any) -> TaskEvent:
        event = TaskEvent(len(self.events) + 1, event_type, actor, data)
        self.events.append(event)
        return event

    def context(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "goal": self.goal,
            "status": self.status,
            "response_depth": self.response_depth,
            "response_mode": self.response_mode,
            "participants": self.participants,
            "reasoning_effort": self.reasoning_effort,
            "approach": self.approach,
            "capabilities": self.capabilities,
            "events": [event.as_dict() for event in self.events],
        }
