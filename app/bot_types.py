from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Dict, List, Optional, Protocol


@dataclass
class BotAttachment:
    path: str
    filename: Optional[str] = None
    delete_after_send: bool = False


@dataclass
class BotResponse:
    text: Optional[str] = None
    attachment: Optional[BotAttachment] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class IncomingMessage:
    content: str
    channel_id: str
    author_id: str
    author_name: str
    author_display_name: str
    created_at: datetime
    author_is_bot: bool = False
    guild_name: Optional[str] = None
    channel_name: Optional[str] = None
    is_dm: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)


class PlatformAdapter(Protocol):
    async def send_channel_message(self, channel_id: str, content: str) -> Any:
        ...

    async def edit_message(self, native_message: Any, content: str) -> None:
        ...

    async def delete_message(self, native_message: Any) -> None:
        ...

    async def send_direct_message(self, user_reference: Any, content: str) -> None:
        ...

    def create_background_task(self, coroutine: Awaitable[Any]) -> Any:
        ...


HandlerResult = List[BotResponse]
