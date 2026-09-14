import asyncio
import hashlib
import logging
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from app.cache_service import (
    find_cached_by_url,
    find_cached_by_media_id,
    get_media_lock,
    put_cache,
)
from app.config import settings
from app.lang import t
from app.models import DownloadResult, MediaAsset, PublicBotError
from app.tiktok_api import fetch_tiktok_media_data, TikTokAPIError
from app.url_utils import (
    is_tiktok_url,
    normalize_url,
    url_hash,
)

logger = logging.getLogger("ttsavefrom_bot.download")


def classify_api_error(exc: Exception) -> str:
    text = str(exc).strip()
    lower = text.lower()
    if "timeout" in lower or "timed out" in lower:
        return t.download_timeout.format(error=text[:500])
    return t.download_internal_error.format(error=text[:500])


def extension_from_url(url: str) -> str:
    suffix = Path(urlparse(url).path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".webp", ".png"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    if suffix == ".mp4":
        return ".mp4"
    return ".jpg"


async def download_file(session: aiohttp.ClientSession, url: str, dest: Path) -> None:
    async with session.get(url) as response:
        if response.status != 200:
            raise RuntimeError(f"Failed to download {url}: status {response.status}")
        with open(dest, "wb") as f:
            async for chunk in response.content.iter_chunked(1024 * 64):
                f.write(chunk)


async def probe_inline_media(url: str) -> DownloadResult:
    """Fast inline path: fetch metadata and remote URLs without downloading."""
    if not is_tiktok_url(url):
        raise RuntimeError("not a TikTok URL")

    effective_url = normalize_url(url)

    try:
        data = await fetch_tiktok_media_data(effective_url)
    except TikTokAPIError as e:
        raise RuntimeError(str(e))

    media_id = str(data.get("id") or url_hash(effective_url))
    title = data.get("title") or ""
    images = data.get("images")

    assets: list[MediaAsset] = []

    if images:
        # Photo album logic
        music = data.get("music") or {}
        if isinstance(music, dict):
            audio_url = music.get("play") or music.get("play_url")
            if audio_url:
                assets.append(
                    MediaAsset(
                        path=Path(""),
                        media_type="audio",
                        remote_url=audio_url,
                        title=music.get("title") or title or "TikTok audio",
                        performer=music.get("authorName") or music.get("author"),
                        duration_seconds=(
                            int(float(music.get("duration", 0)))
                            if music.get("duration")
                            else None
                        ),
                    )
                )

        for img_url in images[: settings.max_photo_count]:
            if not img_url:
                continue
            assets.append(
                MediaAsset(
                    path=Path(""),
                    media_type="photo",
                    remote_url=img_url,
                    thumbnail_url=img_url,
                )
            )

        kind = "photo_album"
    else:
        # Video logic
        video_url = data.get("hdplay") or data.get("play")
        cover_url = data.get("cover") or data.get("origin_cover") or video_url
        if not video_url:
            raise RuntimeError("API did not return video URL")

        assets.append(
            MediaAsset(
                path=Path(""),
                media_type="video",
                remote_url=video_url,
                thumbnail_url=cover_url,
            )
        )
        kind = "video"

    if not assets:
        raise RuntimeError("No media URLs returned by API")

    return DownloadResult(
        media_id=media_id,
        kind=kind,
        source_url=effective_url,
        title=title,
        assets=assets,
    )


async def get_or_download_media(url: str) -> DownloadResult:
    if not is_tiktok_url(url):
        raise PublicBotError(t.invalid_tiktok_url)

    original_url = url
    effective_url = normalize_url(url)

    cached = await find_cached_by_url(original_url)
    if cached:
        return cached

    cached = await find_cached_by_url(effective_url)
    if cached:
        return cached

    try:
        data = await fetch_tiktok_media_data(effective_url)
    except Exception as e:
        raise PublicBotError(classify_api_error(e))

    media_id = str(data.get("id") or url_hash(effective_url))
    title = data.get("title") or ""

    cached = await find_cached_by_media_id(media_id)
    if cached:
        return cached

    images = data.get("images")
    is_photo_album = bool(images)
    kind = "photo_album" if is_photo_album else "video"

    lock = get_media_lock(media_id)
    async with lock:
        cached = await find_cached_by_media_id(media_id)
        if cached:
            return cached

        if settings.enable_cache:
            output_dir = settings.cache_dir
        else:
            # Используем постоянную директорию внутри проекта, а не /tmp
            output_dir = Path.cwd() / ".temp_downloads"
            output_dir.mkdir(exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        timeout = aiohttp.ClientTimeout(total=settings.download_timeout_seconds)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            if is_photo_album:
                paths = []
                seen_hashes = set()

                for idx, img_url in enumerate(images[: settings.max_photo_count]):
                    if not img_url:
                        continue

                    ext = extension_from_url(img_url)
                    dest = output_dir / f"{media_id}_{idx + 1:03d}{ext}"

                    await download_file(session, img_url, dest)

                    if dest.stat().st_size > settings.max_photo_size_bytes:
                        if not settings.enable_cache:
                            shutil.rmtree(output_dir, ignore_errors=True)
                        raise PublicBotError(
                            t.photo_too_large.format(
                                size_mb=dest.stat().st_size // 1024 // 1024,
                                limit_mb=settings.max_photo_size_bytes // 1024 // 1024,
                            )
                        )

                    content_hash = hashlib.sha256(dest.read_bytes()).hexdigest()
                    if content_hash in seen_hashes:
                        dest.unlink()
                        continue
                    seen_hashes.add(content_hash)

                    paths.append(dest)

                if not paths:
                    if not settings.enable_cache:
                        shutil.rmtree(output_dir, ignore_errors=True)
                    raise PublicBotError("API не вернул уникальные файлы фото")

                result = DownloadResult(
                    media_id=media_id,
                    kind="photo_album",
                    source_url=effective_url,
                    title=title,
                    assets=[MediaAsset(path=p, media_type="photo") for p in paths],
                    temp_dir=None if settings.enable_cache else output_dir,
                )

                await put_cache(result, original_url)
                if effective_url != original_url:
                    await put_cache(result, effective_url)
                return result

            else:
                video_url = data.get("hdplay") or data.get("play")
                if not video_url:
                    raise PublicBotError("API не вернул ссылку на видео")

                ext = ".mp4"
                dest = output_dir / f"{media_id}{ext}"
                await download_file(session, video_url, dest)

                if dest.stat().st_size > settings.max_video_size_bytes:
                    if not settings.enable_cache:
                        shutil.rmtree(output_dir, ignore_errors=True)
                    raise PublicBotError(
                        t.video_too_large.format(
                            size_mb=dest.stat().st_size // 1024 // 1024,
                            limit_mb=settings.max_video_size_bytes // 1024 // 1024,
                        )
                    )

                result = DownloadResult(
                    media_id=media_id,
                    kind="video",
                    source_url=effective_url,
                    title=title,
                    assets=[MediaAsset(path=dest, media_type="video")],
                    temp_dir=None if settings.enable_cache else output_dir,
                )

                await put_cache(result, original_url)
                if effective_url != original_url:
                    await put_cache(result, effective_url)
                return result


async def get_or_download_video(url: str) -> DownloadResult:
    result = await get_or_download_media(url)
    if result.kind != "video":
        raise PublicBotError(t.expected_video_got_album)
    return result

def cleanup_temp_dir(result: DownloadResult) -> None:
    if result.temp_dir and result.temp_dir.exists():
        # Удаляем только если это наша временная папка, а не кэш
        if result.temp_dir.name == ".temp_downloads":
            # Удаляем только файлы из этого запуска, а не всю папку
            for asset in result.assets:
                try:
                    if asset.path.exists():
                        asset.path.unlink()
                except Exception:
                    pass
        else:
            shutil.rmtree(result.temp_dir, ignore_errors=True)