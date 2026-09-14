from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedImage:
    path: str
    filename: str
    mime_type: str
    text: str = ""