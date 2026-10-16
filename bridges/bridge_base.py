"""
Base class for Discord bridge implementations.
Users should inherit from BridgedObject to create custom bridges.
"""
from abc import ABC, abstractmethod
from typing import Optional

class BridgedObject(ABC):
    """
    Abstract base class for bridging Discord to external applications.
    
    Users should extend this class and implement the required methods
    to create custom bridges (e.g., game chat, Slack, webhooks, etc.)
    """
    
    def __init__(self, channel_id: str):
        """
        Initialize the bridge for a specific Discord channel.
        
        Args:
            channel_id: The Discord channel ID this bridge is for
        """
        self.channel_id = channel_id
        self.listen_mode = False
    
    @abstractmethod
    async def send_message(self, message: str) -> Optional[str]:
        """
        Send a message to the bridged application.
        
        Args:
            message: The message to send
            
        Returns:
            Optional reply from the bridged application, or None
        """
        pass
    
    @abstractmethod
    async def initialize(self) -> bool:
        """
        Initialize/connect to the bridged application.
        
        Returns:
            True if initialization succeeded, False otherwise
        """
        pass
    
    @abstractmethod
    async def disconnect(self) -> bool:
        """
        Disconnect from the bridged application.
        
        Returns:
            True if disconnection succeeded, False otherwise
        """
        pass
    
    @abstractmethod
    def get_status(self) -> str:
        """
        Get current bridge status.
        
        Returns:
            Status string describing the bridge state
        """
        pass
    
    def set_listen_mode(self, enabled: bool):
        """
        Enable/disable listen mode (auto-forward Discord messages).
        
        Args:
            enabled: True to enable, False to disable
        """
        self.listen_mode = enabled
    
    def is_listening(self) -> bool:
        """
        Check if listen mode is enabled.
        
        Returns:
            True if listening, False otherwise
        """
        return self.listen_mode

    async def execute(self, command, app, message):
        """Default command adapter; richer bridges can add account/inbox/event capabilities."""
        from app.bot_types import BotResponse
        action = command.action
        if action == "status":
            text = self.get_status()
        elif action == "init":
            text = "Bridge connected." if await self.initialize() else "Bridge connection failed."
        elif action == "disconnect":
            text = "Bridge disconnected." if await self.disconnect() else "Bridge disconnection failed."
        elif action == "send":
            text = await self.send_message(command.message) or "Message sent."
        elif action == "listen":
            if command.mode != "status":
                self.set_listen_mode(not self.is_listening() if command.mode == "toggle" else command.mode == "on")
            text = f"Bridge forwarding: {'on' if self.is_listening() else 'off'}."
        else:
            raise ValueError(f"This bridge does not support {action}")
        return [BotResponse(text=text)]
