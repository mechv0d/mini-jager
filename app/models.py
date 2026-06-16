from dataclasses import dataclass
from pathlib import Path


@dataclass
class VideoResult:
    video_id: str
    path: Path
    source_url: str
    title: str | None = None
    telegram_file_id: str | None = None
    from_cache: bool = False
    temp_dir: Path | None = None


class PublicBotError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
