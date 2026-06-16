import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
    bot_username: str = os.getenv("BOT_USERNAME", "ttsavefrom_bot").lstrip("@")
    enable_cache: bool = (
    os.getenv("ENABLE_CACHE", os.getenv("ENABLE_CASH", os.getenv("ENABLE_CASHE", "0"))) == "1"
        )
    dump_chat_id: str | None = os.getenv("DUMP_CHAT_ID")
    cache_dir: Path = Path(os.getenv("CACHE_DIR", "./.cache"))
    cache_ttl_seconds: int = int(os.getenv("CACHE_TTL_SECONDS", "86400"))
    cache_clean_interval_seconds: int = int(os.getenv("CACHE_CLEAN_INTERVAL_SECONDS", "60"))
    download_timeout_seconds: int = int(os.getenv("DOWNLOAD_TIMEOUT_SECONDS", "15"))
    max_video_size_bytes: int = int(os.getenv("MAX_VIDEO_SIZE_MB", "48")) * 1024 * 1024
    telegram_upload_timeout_seconds: int = int(os.getenv("TELEGRAM_UPLOAD_TIMEOUT_SECONDS", "120"))
    telegram_upload_retries: int = int(os.getenv("TELEGRAM_UPLOAD_RETRIES", "3"))
    tiktok_proxy = os.getenv("TIKTOK_PROXY", "").strip() or None
    telegram_proxy = os.getenv("TELEGRAM_PROXY", "").strip() or None

    @property
    def cache_index_file(self) -> Path:
        return self.cache_dir / "index.json"


settings = Settings()

print("ENABLE_CACHE =", settings.enable_cache)
print("TIKTOK_PROXY =", settings.tiktok_proxy)