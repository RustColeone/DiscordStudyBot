"""An executable local bridge example: lifecycle, forwarding, replies and status.

Register a factory with BridgeFeature.register(name, factory) to add an integration.
Factories receive the Discord channel ID; account transports can be shared.
Override execute() for richer commands or add a separate background event receiver.
This demo never contacts another application.
"""
from bridges.bridge_base import BridgedObject


class ExampleBridge(BridgedObject):
    def __init__(self, channel_id):
        super().__init__(channel_id)
        self.connected = False
        self.message_count = 0

    async def initialize(self):
        self.connected = True
        return True

    async def send_message(self, message):
        if not self.connected:
            raise ValueError("Connect the example first: $bridge example --init")
        self.message_count += 1
        return f"Example received #{self.message_count}: {message}"

    async def disconnect(self):
        self.connected = False
        self.listen_mode = False
        return True

    def get_status(self):
        return (f"Example bridge: {'connected' if self.connected else 'disconnected'}\n"
                f"Messages received: {self.message_count}\n"
                f"Discord forwarding: {'on' if self.listen_mode else 'off'}")
