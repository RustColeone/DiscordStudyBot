from contextlib import asynccontextmanager


@asynccontextmanager
async def show_generation_typing(message):
    channel = message.metadata.get("native_channel")
    typing = getattr(channel, "typing", None)
    if typing is None:
        yield
        return

    async with typing():
        yield