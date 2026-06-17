from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

MediaKind = Literal["video", "photo_album"]


@dataclass
class MediaAsset:
    path: Path
    telegram_file_id: str | None = None
    media_type: str = "photo"
    # For fast inline photo results: Telegram shows thumbnail_url immediately
    # and downloads photo_url only when the user selects this inline result.
    remote_url: str | None = None
    thumbnail_url: str | None = None


@dataclass
class DownloadResult:
    media_id: str
    kind: MediaKind
    source_url: str
    title: str | None = None
    assets: list[MediaAsset] = field(default_factory=list)
    from_cache: bool = False
    temp_dir: Path | None = None


VideoResult = DownloadResult


class PublicBotError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message
