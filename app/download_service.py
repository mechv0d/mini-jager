import asyncio
import re
import shutil
import tempfile
from pathlib import Path

from yt_dlp import YoutubeDL
from yt_dlp.utils import DownloadError

from app.cache_service import (
    find_cached_by_url,
    find_cached_by_video_id,
    get_video_lock,
    put_cache,
)
from app.config import settings
from app.models import PublicBotError, VideoResult
from app.url_utils import is_tiktok_url, parse_video_id_from_url, url_hash


def classify_download_error(exc: Exception) -> str:
    text = str(exc).strip()
    lower = text.lower()

    if "your ip address is blocked" in lower:
        return (
            "TikTok заблокировал текущий IP-адрес. "
            "Проверь TIKTOK_PROXY в .env или выбери другой proxy/VPN endpoint."
        )

    http_match = re.search(r"HTTP Error\s+(\d{3})|status code\s+(\d{3})|HTTP\s+(\d{3})", text)
    if http_match:
        code = next(group for group in http_match.groups() if group)
        return f"HTTP {code}: {text[:500]}"

    if "timed out" in lower or "timeout" in lower:
        return f"таймаут: {text[:500]}"

    return f"внутренняя ошибка скачивания: {text[:500]}"


def ytdlp_base_options() -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": settings.download_timeout_seconds,
        "retries": 1,
        "fragment_retries": 1,
        "format": "bv*[ext=mp4]+ba[ext=m4a]/b[ext=mp4]/best",
        "merge_output_format": "mp4",
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            )
        },
    }

    if settings.tiktok_proxy:
        options["proxy"] = settings.tiktok_proxy

    return options


def probe_info_sync(url: str) -> dict:
    options = ytdlp_base_options()
    options.update({"skip_download": True})

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)
        return ydl.sanitize_info(info)


def download_video_sync(url: str, output_dir: Path) -> tuple[dict, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)

    options = ytdlp_base_options()
    options.update(
        {
            "outtmpl": str(output_dir / "%(id)s.%(ext)s"),
        }
    )

    with YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        clean_info = ydl.sanitize_info(info)

        filepath = None
        for item in clean_info.get("requested_downloads") or []:
            if item.get("filepath"):
                filepath = item["filepath"]
                break

        if not filepath:
            guessed = Path(ydl.prepare_filename(info))
            candidates = list(output_dir.glob(f"{clean_info.get('id', '*')}.*"))
            if guessed.exists():
                filepath = str(guessed)
            elif candidates:
                filepath = str(candidates[0])

        if not filepath:
            raise RuntimeError("yt-dlp did not return downloaded filepath")

        path = Path(filepath)
        if not path.exists():
            raise RuntimeError(f"downloaded file does not exist: {path}")

        return clean_info, path


async def to_thread_ytdlp(func, *args):
    try:
        return await asyncio.to_thread(func, *args)
    except DownloadError as exc:
        raise PublicBotError(classify_download_error(exc)) from exc


async def get_or_download_video(url: str) -> VideoResult:
    if not is_tiktok_url(url):
        raise PublicBotError("невалидная ссылка: поддерживаются только ссылки TikTok")

    cached = await find_cached_by_url(url)
    if cached:
        return cached

    parsed_video_id = parse_video_id_from_url(url)
    if parsed_video_id:
        cached = await find_cached_by_video_id(parsed_video_id)
        if cached:
            return cached

    info = await to_thread_ytdlp(probe_info_sync, url)
    video_id = str(info.get("id") or parsed_video_id or url_hash(url))
    title = info.get("title")

    cached = await find_cached_by_video_id(video_id)
    if cached:
        return cached

    lock = get_video_lock(video_id)
    async with lock:
        cached = await find_cached_by_video_id(video_id)
        if cached:
            return cached

        if settings.enable_cache:
            output_dir = settings.cache_dir
        else:
            output_dir = Path(tempfile.mkdtemp(prefix="ttbot_"))

        info, path = await to_thread_ytdlp(download_video_sync, url, output_dir)

        if path.stat().st_size > settings.max_video_size_bytes:
            if not settings.enable_cache:
                shutil.rmtree(output_dir, ignore_errors=True)
            raise PublicBotError(
                f"видео слишком большое: {path.stat().st_size // 1024 // 1024} MB, "
                f"лимит {settings.max_video_size_bytes // 1024 // 1024} MB"
            )

        result = VideoResult(
            video_id=str(info.get("id") or video_id),
            path=path,
            source_url=url,
            title=info.get("title") or title,
            temp_dir=None if settings.enable_cache else output_dir,
        )

        await put_cache(result, url)
        return result
