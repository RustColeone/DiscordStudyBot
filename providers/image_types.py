from dataclasses import dataclass


@dataclass(frozen=True)
class GeneratedImage:
    path: str
    filename: str
    mime_type: str
    text: str = ""


@dataclass(frozen=True)
class LocatedImage:
    path: str
    filename: str
    mime_type: str
    title: str
    source_url: str
    image_url: str